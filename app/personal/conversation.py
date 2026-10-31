"""统一健康助理入口：跨会话指代、事项操作、规划和复盘。"""
import re
from uuid import uuid4
from zoneinfo import ZoneInfo
from datetime import datetime,timedelta
from app.personal.contracts import PersonalIntent,EventText,TaskFeedback,TaskInput,GoalInput
from app.personal.capabilities import PersonalReadAccess
from app.multi_agent.context_builder import ContextBuilder
from app.personal.location_conversation import nearby_requests,asks_location_status,location_reply,collect_nearby
from app.personal.local_replies import local_reply


def chat(agent,body):
    continuity=agent.continuity
    session=continuity.session(timezone_name=body.timezone_name,session_id=body.session_id)
    session_id=session['id'];goal_id=body.goal_id
    if not goal_id and len(session.get('goal_ids',[]))==1:goal_id=session['goal_ids'][0]
    if goal_id:continuity.get('goal',goal_id)
    source=continuity.message(session_id,'user',body.message)
    memory_reply=agent.service.memory_command(body.message,datetime.now(ZoneInfo(body.timezone_name)).date().isoformat(),source)
    if memory_reply:
        continuity.message(session_id,'assistant',memory_reply['reply'])
        agent.update_live(session_id,goal_id)
        return {'session_id':session_id,'goal_id':goal_id,**memory_reply}
    fixed_reply=local_reply(body.message)
    if fixed_reply:
        continuity.message(session_id,'assistant',fixed_reply);agent.update_live(session_id,goal_id)
        return {'session_id':session_id,'goal_id':goal_id,'reply':fixed_reply,'intent':{'intent':'chat','reason':'固定身份/问候答复。'},'handled_by':'local_reply'}
    zone=ZoneInfo(body.timezone_name);today=datetime.now(zone).date().isoformat()
    policy=agent.policy()
    if body.use_external:policy={**policy,'allow_environment_queries':True}
    environment=agent.environment(policy)
    location_context=environment.location_context(today)
    resolved_location=environment.resolve(today) or {}
    position={k:resolved_location[k] for k in ('latitude','longitude') if resolved_location.get(k) is not None}
    if position:position['accuracy_m']=resolved_location.get('accuracy_m') if resolved_location.get('accuracy_m') is not None else 100000
    elif resolved_location.get('city') and resolved_location.get('start_place'):
        position={k:resolved_location[k] for k in ('city','start_place')}
    queries=nearby_requests(body.message)
    if asks_location_status(body.message):
        reply=location_reply(location_context)
        continuity.message(session_id,'assistant',reply);agent.update_live(session_id,goal_id)
        return {'session_id':session_id,'goal_id':goal_id,'reply':reply,'intent':{'intent':'chat','reason':'读取本次有效位置事实。'},'location_context':location_context}
    if session.get('pending_event') and not any(w in body.message for w in ('先不处理','取消澄清','新建目标','复盘')):
        pending=session['pending_event']
        result=agent.ingest_message(EventText.model_validate({**pending,'text':pending['text']+'；补充：'+body.message,'session_id':session_id,'mode':body.mode}))
        reply=result.get('message','事件已处理。')
        continuity.message(session_id,'assistant',reply);agent.update_live(session_id,goal_id)
        return {'session_id':session_id,'goal_id':goal_id,'reply':reply,**result}
    if session.get('pending_event'):
        session.pop('pending_event',None);continuity.put('session',session_id,session,expected_revision=session['revision'])
    latest_run=next((key for key in reversed(session.get('run_ids',[])) if not key.startswith('personal-job:')),None)
    if latest_run and not queries and not any(w in body.message for w in ('会议','开会','加班','出差','饭局','聚餐')):
        current=agent.service.get_run(latest_run)
        if current['status']=='waiting_clarification':
            run=agent.service.reply(latest_run,body.message,body.city or resolved_location.get('city',''),current['state'].get('clarification_round',0),location=position or None,use_external=bool(policy['allow_environment_queries']),source_message_id=source,refresh_location=True)
            continuity.message(session_id,'assistant',run['reply'],run['id'])
            agent.update_live(session_id,goal_id)
            return {'session_id':session_id,'goal_id':goal_id,'reply':run['reply'],'run':run}
        if current['status']=='waiting_confirmation' and re.fullmatch(r'(确认|同意|就这样|拒绝|不采用)[。！!\s]*',body.message):
            approved=not any(w in body.message for w in ('拒绝','不采用'))
            run=agent.service.decide(latest_run,approved,source)
            if approved:agent.sync_state(goal_id)
            continuity.message(session_id,'assistant',run['reply'],run['id'])
            agent.update_live(session_id,goal_id)
            return {'session_id':session_id,'goal_id':goal_id,'reply':run['reply'],'run':run}
    access=PersonalReadAccess(continuity,session_id,goal_id)
    context=access.continuity.working_context(session_id,goal_id)
    text=body.message.strip()
    if queries:
        intent={'intent':'chat','domains':[],'reason':'只读附近信息查询；先以已授权位置获取地图证据，不创建新计划。'}
    elif body.mode=='live':
        profile=agent.store.profile() or {}
        initial={'profile':profile,'user_message':text,'skill_catalog':access.catalog.list(),
                 'location_context':location_context,
                 'local_now':datetime.now(ZoneInfo(body.timezone_name)).isoformat(),
                 'recent_messages':[{'role':m['role'],'content':m['content'][:500]} for m in continuity.messages(session_id,8)]}
        initial,_=ContextBuilder().build('personal_supervisor',initial)
        intent=agent._runner('live').run('personal_supervisor',initial,PersonalIntent,extra_bindings={**access.bindings(),**environment.bindings('personal_supervisor')})
    else:
        kind='goal' if any(w in text for w in ('我的目标','目标是','建立目标')) else 'task' if any(w in text for w in ('提醒我','添加待办')) else 'review' if any(w in text for w in ('复盘','回顾','洗澡时刻')) else 'plan' if any(w in text for w in ('规划','安排计划','制定','调整运动','安排运动','安排饮食','安排睡眠')) else 'event' if any(w in text for w in ('会议','饭局','聚餐','出差','加班')) else 'feedback' if any(w in text for w in ('完成了','没完成','做完了','跳过','部分完成')) else 'status'
        intent={'intent':kind,'domains':[],'task_id':'','outcome':'skipped' if any(w in text for w in ('没完成','跳过')) else 'completed','question':'','reason':'离线规则识别，非模型理解。'}
    # 明确告知的外部事实先归档；未检索到运动事项不能阻止记录会议。这里只修正分流，不推断缺失时间。
    explicit_event=(re.search(r'会议|开会|饭局|聚餐|出差|加班',text)
                    and re.search(r'\d{1,2}[:：]\d{2}|[一二两三四五六七八九十0-9]+点',text)
                    and re.search(r'有.{0,8}(?:会议|饭局|聚餐)|(?:要|需要|临时|安排了).{0,5}(?:开会|会议|出差|加班)',text)
                    and not re.search(r'如果|假如|假设|例如|举例|可能|待定|是否|如何|怎么|吗|[？?]',text))
    if explicit_event and intent['intent'] in ('plan','clarify','status','chat'):
        intent={**intent,'intent':'event','question':'','reason':'明确日程事实优先归档，再由事件图判断是否影响计划。'}
    skill_name={'plan':'health-plan','event':'health-plan','review':'evening-review'}.get(intent['intent'],'daily-planning')
    skill=access.catalog.read(skill_name)
    continuity.append('skill_use',session_id,{'name':skill_name,'version':skill['version'],'source_message_id':source},'skill-use:'+source)
    reply='';extra={}
    if intent['intent']=='goal':
        draft=intent.get('goal_draft') or {'title':text[:100],'objective':text,'domains':intent.get('domains') or ['activity','diet','sleep']}
        goal=continuity.create_goal(GoalInput.model_validate({**draft,'session_id':session_id}).model_dump(mode='json'))
        goal_id=goal['id'];extra['goal']=goal;reply='已建立这个持续目标。后续计划、事项与反馈可以关联到它。'
    elif intent['intent']=='task':
        draft=intent.get('task_draft')
        if not draft:
            match=re.search(r'(?:([上下]午|晚上|早上))?\s*(\d{1,2})(?:[:：]([0-5]\d)|点(半)?)',text)
            if match:
                hour=int(match[2]);minute=int(match[3] or (30 if match[4] else 0))
                if match[1] in ('下午','晚上') and hour<12:hour+=12
                when=datetime.now(ZoneInfo(body.timezone_name)).replace(hour=hour,minute=minute,second=0,microsecond=0)
                if '明天' in text:when+=timedelta(days=1)
                if when>datetime.now(when.tzinfo):draft={'title':text[:120],'kind':'checkin','start_at':when.isoformat(),'notes':text}
        if not draft:reply='请补充明确提醒时间，例如“明天21:00提醒我记录睡眠”。'
        else:
            task=continuity.create_task(TaskInput.model_validate({**draft,'session_id':session_id,'goal_id':goal_id}).model_dump(mode='json'))
            agent.sync_reminders();extra['task']=task;reply='已保存这个事项。启用后端提醒规则后，会按时间进入提醒与投递流程。'
    elif intent['intent']=='plan':
        if not agent.store.profile():reply='请先建立健康档案，再生成个人计划。'
        else:
            zone=ZoneInfo(body.timezone_name);offset=-int(datetime.now(zone).utcoffset().total_seconds()/60)
            latest=next((r for r in reversed(session.get('run_ids',[])) if not r.startswith('personal-job:')),None)
            run=agent.service.generate(body.mode,text,body.city or resolved_location.get('city',''),bool(policy['allow_environment_queries']),location=position or None,timezone_offset=offset,conversation_id=latest,source_message_id=source)
            continuity.link_run(session_id,run['id'],goal_id)
            extra['run']=run;reply=run['reply']
    elif intent['intent']=='event':
        reference=datetime.now(ZoneInfo(body.timezone_name))
        extra=agent.ingest_message(EventText(text=text,reference_at=reference,external_id=source,source='manual',mode=body.mode,session_id=session_id))
        reply=extra['message']
    elif intent['intent']=='review':
        job=agent.runtime.enqueue('review',{'goal_id':goal_id},'chat-review:'+source)
        extra['job']=job;reply='已保存复盘任务；会基于记录和执行反馈生成有来源的观察，结论不会自动成为长期偏好。'
    elif intent['intent']=='feedback':
        task_id=intent.get('task_id')
        if not task_id:
            mentioned=[t for t in context['tasks'] if t['id'] in text or len(t['title'])>3 and t['title'] in text]
            if len(mentioned)==1:task_id=mentioned[0]['id']
        if not task_id:
            reply='请指出对应事项或任务ID，避免把“那个完成了”记到错误的安排。';extra['candidate_tasks']=context['next_steps']
        else:
            # 只有原消息明确表达完成状态才写入；模型不能自行推断执行事实。
            explicit='partial' if '部分完成' in text else 'skipped' if any(w in text for w in ('没完成','跳过','未完成')) else 'completed' if any(w in text for w in ('完成了','做完了','已完成')) else None
            if explicit is None:reply='请明确是完成、部分完成还是跳过，并说明对应事项。'
            else:
                match=re.search(r'(\d+(?:\.\d+)?)\s*分钟',text)
                feedback=TaskFeedback(outcome=explicit,note=text,actual_minutes=float(match[1]) if match else None,idempotency_key='chat:'+source)
                extra=agent.feedback(task_id,feedback.model_dump(mode='json'));reply='已记录这项实际反馈，并保留来源。未提供的实际分钟没有用计划值代替。'
    elif intent['intent']=='clarify':reply=intent.get('question') or '请补充你希望处理的目标或事项。'
    elif intent['intent']=='chat':
        from app.multi_agent.state import AdviceAnswer
        today=datetime.now(ZoneInfo(body.timezone_name)).date().isoformat()
        memories=agent.service.memory.recall(today)
        profile=agent.service.memory.profile(agent.store.profile() or {},memories)
        evidence=collect_nearby(environment,queries,today) if queries and body.mode=='live' else []
        initial={'profile':profile,'user_message':text,'as_of':today,'source_message_id':source,
                 'location_context':location_context,'memory_context':memories,'external_evidence':evidence,
                 'dialogue':[{'role':m['role'],'content':m['content'][:800]} for m in continuity.messages(session_id,10)],
                 'records':agent.store.records_range((datetime.fromisoformat(today)-timedelta(days=6)).date().isoformat(),today)}
        initial,_=ContextBuilder().build('advisor',initial)
        bindings={**access.bindings(),**environment.bindings('advisor')}
        result=agent._runner(body.mode).run('advisor',initial,AdviceAnswer,
                  retrieval=agent.service.memory.agent_access('advisor',today,initial),extra_bindings=bindings)
        reply=result['text'];extra['evidence_ids']=result.get('evidence_ids',[])
        extra['location_context']=location_context
        extra['environment_results']=[{k:e.get(k) for k in ('tool','status','message','id','precision_note')} for e in environment.evidence]
    else:
        live=agent.context(session_id,goal_id)
        lines=[f"当前有{len(live['goals'])}个活跃目标，{live['pending_count']}项待处理事项。"]
        lines += [f"{t['title']}：{t['status']}" for t in live['next_steps'][:5]]
        if live['pending_actions']:lines.append(f"另有{len(live['pending_actions'])}份日程调整需要查看。")
        reply='\n'.join(lines);extra['context']=live
    continuity.message(session_id,'assistant',reply,extra.get('run',{}).get('id'))
    agent.update_live(session_id,goal_id)
    return {'session_id':session_id,'goal_id':goal_id,'reply':reply,'intent':intent,'skill':{'name':skill_name,'version':skill['version']},**extra}
