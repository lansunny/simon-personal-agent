"""健康Personal Agent后端：持续状态 + 事件任务 + 多角色 + 可验证行动。"""
import copy
import hashlib
import json
from datetime import datetime,timedelta,timezone
from uuid import uuid4
from zoneinfo import ZoneInfo
from app.config import settings
from app.errors import BudgetExceeded
from app.personal.service import PersonalService
from app.personal.contracts import AutomationPolicy,LifeEvent,PersonalIntent,ScheduleInput,EventText
from app.personal.continuity import Continuity,dump,stamp
from app.personal.runtime import JobRuntime,utcnow
from app.personal.delivery import Delivery
from app.personal.event_store import EventStore
from app.personal.capabilities import PersonalReadAccess,SkillCatalog
from app.personal.environment import EnvironmentAccess
from app.personal.schedule_engine import normalize
from app.personal.planning import build_event_graph
from app.multi_agent.quantification import attach_quantification
from app.multi_agent.context_builder import ContextBuilder
from app.multi_agent.state import Review


class HealthPersonalAgent(PersonalService):
    def __init__(self,service,start_worker=True,environment_caller=None):
        super().__init__(service,start_worker=False)
        self.continuity=Continuity(self.store,service.memory)
        self.event_store=EventStore(self.store)
        self.environment_caller=environment_caller
        self.delivery=Delivery(self.store)
        self.runtime=JobRuntime(self.store,self.handle_job,self.maintenance,start_worker=False)
        self.thread=self.runtime.thread
        self.reconcile_schedules()
        self.sync_state()
        if start_worker:self.thread.start()

    def close(self):self.runtime.close()

    def policy(self):
        saved=super().policy();revision=saved.pop('revision')
        return {**AutomationPolicy.model_validate(saved).model_dump(),'revision':revision}

    def events(self):
        return self.event_store.list() if hasattr(self,'event_store') else super().events()

    def save_policy(self,policy):
        result=super().save_policy(AutomationPolicy.model_validate(policy).model_dump())
        if hasattr(self,'runtime'):self.reconcile_schedules()
        return result

    def _runner(self,mode):
        from app.multi_agent.runners import DeepSeekRunner,DemoRunner
        if mode=='demo':return DemoRunner()
        runner=DeepSeekRunner();request=runner.request
        def counted(messages,tools):
            policy=self.policy()
            self.runtime.consume('model',policy['model_daily_budget'],policy['timezone_name'])
            return request(messages,tools)
        runner.request=counted
        return runner

    def environment(self,policy=None):
        try:location=self.continuity.get('location','current')
        except KeyError:location=None
        args={'caller':self.environment_caller} if self.environment_caller else {}
        return EnvironmentAccess(self.continuity,self.runtime,policy or self.policy(),self.events(),location,**args)

    def capability_bindings(self,role,context):
        if role not in ('router','activity','diet','sleep','coordinator','advisor'):return None
        policy=self.policy()
        if context.get('allow_external'):policy={**policy,'allow_environment_queries':True}
        environment=self.environment(policy)
        context.setdefault('location_context',environment.location_context(context.get('as_of','')))
        bindings=environment.bindings(role)
        read_access=PersonalReadAccess(self.continuity,role=role)
        bindings.update(read_access.bindings());context['skill_catalog']=read_access.catalog.list()
        if role in ('diet','coordinator'):
            from app.multi_agent.tools import ToolBinding,NoArguments
            context['meal_templates']=self.meal_template_summaries()
            bindings['read_meal_templates']=ToolBinding(NoArguments,self.meal_template_summaries,'读取用户提供的餐食模板ID与每份量化结果；不能当成餐馆菜单。')
        return bindings

    def meal_templates(self):return self.continuity.list('meal_template',limit=20)

    def meal_template_summaries(self):
        return [{'id':t['id'],'name':t['name'],'revision':t['revision'],
                 'nutrition':{k:t['nutrition'].get(k) for k in ('kcal','grams','protein_g','fat_g','carbs_g')},
                 'source':t['source']} for t in self.meal_templates()[:8]]

    def save_meal_template(self,body):
        from app.multi_agent.quantification import nutrition_total
        return self.continuity.put('meal_template',uuid4().hex,{**body,'nutrition':nutrition_total(body['items']),'source':'用户明确提供的分量与营养资料'})

    def call_external(self,requests):
        from app.integrations.mcp_client import call_tools
        policy=self.policy();count=sum(r['name']!='ingredients' for r in requests)
        try:
            if count:self.runtime.consume('external',policy['external_daily_budget'],policy['timezone_name'],count)
        except (ValueError,BudgetExceeded):return [{'tool':r['name'],'status':'limit_reached','message':'当日外部查询预算已用完。'} for r in requests]
        return (self.environment_caller or call_tools)(requests)

    def set_location(self,body):
        now=utcnow();value=dict(body)
        value['confirmed_at']=value.get('confirmed_at') or now.isoformat()
        moment=datetime.fromisoformat(value['confirmed_at'])
        if moment.tzinfo is None or moment>now+timedelta(minutes=5):raise ValueError('位置确认时间无效。')
        if (value.get('latitude') is None)!=(value.get('longitude') is None):raise ValueError('经纬度需同时提供。')
        if value.get('latitude') is None and not (value.get('city') and value.get('start_place')):raise ValueError('请确认坐标，或城市和具体起点。')
        result=self.continuity.put('location','current',value)
        return {**result,'note':'仅使用本次确认位置，按时效失效，不后台跟踪。'}

    def current_goal(self):
        goals=self.continuity.list('goal','active')
        return goals[0]['id'] if len(goals)==1 else None

    def sync_state(self,goal_id=None):
        with self.service.lock:return self._sync_state(goal_id)

    def _sync_state(self,goal_id=None):
        active=self.store.active_plan()
        if active and self.store.profile():
            policy=self.policy();today=utcnow().astimezone(ZoneInfo(policy['timezone_name'])).date().isoformat()
            profile=self.service.memory.profile(self.store.profile(),self.service.memory.recall(today))
            body=normalize(active['body'],profile,policy)
            self.continuity.sync_plan({**active,'body':body},goal_id or self.current_goal())
        return self.update_live()

    def sync_reminders(self):
        with self.service.lock:return self._sync_reminders()

    def _sync_reminders(self):
        if not hasattr(self,'continuity'):return
        policy=self.policy();desired={};tasks=self.continuity.list('task',limit=10000)
        active=self.store.active_plan()
        if policy['reminders_enabled']:
            for task in tasks:
                if task['status']!='scheduled' or not task.get('start_at'):continue
                start=datetime.fromisoformat(task['start_at'])
                if start+timedelta(minutes=15)<=utcnow():continue
                key=f"task:{task['id']}:{task['revision']}"
                due=start-timedelta(minutes=policy['reminder_before_minutes'] if task['kind']=='activity' else 0)
                desired[key]=(due.isoformat(),{'task_id':task['id'],'task_revision':task['revision'],
                    'title':task['title'],'start_at':start.isoformat(),'quantities':task.get('quantities'),
                    'energy':task.get('quantities',{}).get('energy'),'minutes':task.get('quantities',{}).get('minutes'),
                    'expires_at':(start+timedelta(minutes=15)).isoformat(),
                    'plan_version':task.get('plan_version'),'reminder_key':key,
                    'message':task.get('notes') or '这是已安排的健康事项；计划值不代表实际完成。'})
        with self.store.connection() as db:
            for row in db.execute("SELECT key FROM health_reminders WHERE status='pending'").fetchall():
                if row['key'] not in desired:db.execute("UPDATE health_reminders SET status='cancelled' WHERE key=?",(row['key'],))
            for key,(due,body) in desired.items():db.execute('INSERT OR IGNORE INTO health_reminders VALUES (?,?,?,?)',(key,due,'pending',dump(body)))
            for row in db.execute("SELECT id,body FROM personal_outbox WHERE kind='reminder' AND status='unread'").fetchall():
                data=json.loads(row['body'])
                if data.get('reminder_key') not in desired or data.get('plan_version') is not None and (not active or data['plan_version']!=active['version']):
                    db.execute("UPDATE personal_outbox SET status='superseded' WHERE id=?",(row['id'],))

    def update_live(self,session_id=None,goal_id=None):
        context=self.continuity.working_context(session_id,goal_id)
        context['pending_actions']=[{'id':a['id'],'status':a['status'],'event_title':a['body'].get('event_title'),'unresolved':a['body'].get('unresolved',[])}
                                    for a in self.actions() if a['status'] in ('pending','blocked','failed')][:10]
        context['next_steps']=[{'action_id':a['id'],'title':a.get('event_title'),'status':a['status']} for a in context['pending_actions']]+context['next_steps']
        self.continuity.put('working_context',session_id or 'current',context)
        return context

    def snapshot(self):
        base=super().snapshot()
        return {**base,'jobs':self.runtime.list(20),'schedules':self.runtime.schedules(),'usage':self.runtime.usage(),
                'goals':self.continuity.list('goal'),'working_context':self.context(),
                'deliveries':self.delivery.list(),'skills':SkillCatalog().list(),'backend_version':'health-personal-7'}

    def context(self,session_id=None,goal_id=None):
        result=self.continuity.working_context(session_id,goal_id)
        try:result['live']=self.continuity.get('working_context',session_id or 'current')
        except KeyError:result['live']=None
        result['pending_actions']=[{'id':a['id'],'status':a['status'],'event_title':a['body'].get('event_title')} for a in self.actions() if a['status'] in ('pending','blocked')]
        return result

    def ingest(self,event,mode=None,process=True):
        value=LifeEvent.model_validate(event).model_dump(mode='json')
        with self.service.lock:
            if value.get('related_event_id'):self.event_store.get(value['related_event_id'])
            saved=super().ingest(value,mode,process=False)
            if saved.get('duplicate'):
                saved['event_id']=self.event_store.canonical_id(saved['event_id']);return saved
            stored=self.event_store.update(saved['event_id'],value)
            saved['event_id']=stored['id']
        self.continuity.append('event',stored['id'],{'event':stored},f"event:{stored['id']}:{stored['revision']}")
        if not process:return saved
        return self.process(stored['id'],mode)

    def ingest_message(self,body):
        """所有消息入口共享关联、澄清与提取逻辑；外部文本不能修改授权。"""
        import re
        from app.personal.ingestion import parse_text
        body=EventText.model_validate(body)
        reference=body.reference_at or utcnow()
        if reference.tzinfo is None:raise ValueError('消息参考时间需包含时区。')
        # 手机时间戳通常是UTC；“明天19点”应按用户时区解析，而非UTC的明天19点。
        reference=reference.astimezone(ZoneInfo(self.policy()['timezone_name']))
        known=self.event_store.get(body.related_event_id) if body.related_event_id else self.event_store.resolve(body.source,body.external_id) if body.external_id else None
        session=self.continuity.get('session',body.session_id) if body.session_id else None
        updating=any(w in body.text for w in ('取消','不聚了','不开了','改到','改为','改期','推迟','提前','确认参加','确认开会','照常'))
        if not known and updating:
            if session and session.get('last_event_id'):known=self.event_store.get(session['last_event_id'])
            else:
                kind='trip' if '出差' in body.text else 'dinner' if any(w in body.text for w in ('饭局','聚餐','吃饭')) else 'meeting' if '会议' in body.text else None
                candidates=[e for e in self.events() if e['status']!='cancelled' and datetime.fromisoformat(e['end_at'])>=reference and (kind is None or e['kind']==kind)]
                if len(candidates)==1:known=candidates[0]
                elif len(candidates)>1:
                    return {'needs_information':True,'message':'有多个可能关联的事件，请指定事件ID后再改期或取消。',
                            'candidate_events':[{k:e[k] for k in ('id','title','start_at','end_at')} for e in candidates[:8]]}
                elif body.mode=='demo':return {'needs_information':True,'message':'没有找到可关联的原事件，请提供事件ID或完整事件信息。'}
        # 明确取消/确认已知事项时保留原时间；没有编造新的结束时间。
        if known and updating and not re.search(r'\d{1,2}[:：]\d{2}|[0-9一二两三四五六七八九十]+点',body.text) and any(w in body.text for w in ('取消','不聚了','不开了','确认参加','确认开会','照常')):
            extracted={k:v for k,v in known.items() if k not in ('id','revision')}
            extracted.update(source=body.source,external_id=body.external_id or hashlib.sha256(body.text.encode()).hexdigest()[:32],
                             related_event_id=known['id'],source_text=body.text,
                             status='cancelled' if any(w in body.text for w in ('取消','不聚了','不开了')) else 'confirmed')
        else:
            extracted=parse_text(body.text,reference,body.external_id,body.source,self._runner('live') if body.mode=='live' else None,
                                 known_event=known,known_events=self.events())
        if extracted.get('needs_information'):
            if session:
                session['pending_event']={**body.model_dump(mode='json'),'session_id':None}
                self.continuity.put('session',session['id'],session,expected_revision=session['revision'])
            return extracted
        result=self.ingest(extracted,body.mode)
        if session:
            session=self.continuity.get('session',session['id']);session.pop('pending_event',None)
            session['last_event_id']=result['event_id']
            self.continuity.put('session',session['id'],session,expected_revision=session['revision'])
        return result

    def process(self,event_id,mode=None):
        event=next((e for e in self.events() if e['id']==event_id),None)
        if not event:raise KeyError(event_id)
        with self.store.connection() as db:feedback_revision=db.execute("SELECT COALESCE(MAX(rowid),0) FROM personal_journal WHERE kind='feedback'").fetchone()[0]
        dependency=hashlib.sha256(dump([self.store.revision(),self.policy()['revision'],feedback_revision]).encode()).hexdigest()[:16]
        job=self.runtime.enqueue('event_replan',{'event_id':event_id,'event_revision':event['revision'],'mode':mode or self.policy()['agent_mode']},
                                 f"event:{event_id}:{event['revision']}:{(self.store.active_plan() or {}).get('version',0)}:{dependency}")
        self.update_live()
        return {'event_id':event_id,'job':job,'message':'事件已存档并进入持久后台任务；可查询任务、候选和处理结果。'}

    def import_calendar(self,content):
        from app.personal.calendar_reader import read_calendar
        parsed=read_calendar(content,self.policy()['timezone_name'])
        self.continuity.put('calendar_health','current',{k:v for k,v in parsed.items() if k!='events'})
        changed=[]
        for event in parsed['events']:
            result=self.ingest(event,process=False)
            if not result.get('duplicate'):changed.append(result['event_id'])
        for event in super().events():
            if event['source']!='calendar' or event['status']=='cancelled':continue
            series=event['external_id'].split('::',1)[0]
            if series not in parsed['cancelled_series']:continue
            body={k:v for k,v in event.items() if k not in ('id','revision')}
            body.update(status='cancelled',source_sequence=parsed['cancelled_series'][series])
            result=self.ingest(body,process=False)
            if not result.get('duplicate'):changed.append(result['event_id'])
        relevant=[];active=self.store.active_plan()
        if active:
            horizon=max((d['date'] for d in active['body'].get('daily_schedule',[])),default=utcnow().date().isoformat())
            relevant=[e for e in self.events() if e['id'] in changed and e['start_at'][:10]<=horizon and datetime.fromisoformat(e['end_at'])>utcnow()]
        jobs=[]
        if relevant:
            trigger=next((e for e in relevant if e['status']=='cancelled'),relevant[0])
            jobs.append(self.process(trigger['id'])['job'])
        self.calendar_status=f"已导入{len(parsed['events'])}个日历实例，变更{len(changed)}个。"
        return {'imported':len(parsed['events']),'changed':len(changed),'jobs':jobs,'warnings':parsed['warnings'],'complete':parsed['complete'],
                'message':'按UID/重复实例更新；支持明确取消。订阅缺项不等于取消。'}

    def handle_job(self,job):
        kind=job['kind'];payload=job['payload']
        if kind=='event_replan':return self.replan(job)
        if kind=='calendar_sync':
            if not self.policy()['calendar_enabled']:return {'status':'disabled'}
            return self.sync_calendar()
        if kind=='review':return self.review(payload.get('goal_id'),job['id'])
        if kind=='consolidate':return self.consolidate(job['id'])
        if kind=='checkin':
            context=self.context(goal_id=payload.get('goal_id'))
            due=[t for t in context['tasks'] if t['status']=='scheduled' and t.get('end_at') and datetime.fromisoformat(t['end_at'])<utcnow()]
            if not due:return {'status':'quiet','reason':'没有需要询问执行结果的事项。'}
            self.notify('checkin:'+job['id'],'checkin',{'title':'执行情况待补充','message':f'有{len(due)}项安排尚无执行反馈，未将缺失视为未完成。','task_ids':[t['id'] for t in due]})
            return {'status':'asked','task_ids':[t['id'] for t in due]}
        if kind=='environment_check':return self.environment_check(job)
        raise ValueError('未注册的后台任务。')

    def replan(self,job):
        payload=job['payload']
        with self.service.lock:
            event=next((e for e in self.events() if e['id']==payload['event_id']),None)
            if not event or event['revision']!=payload['event_revision']:return {'status':'superseded'}
            active=self.store.active_plan();profile=self.store.profile();policy=self.policy()
            if not active or not profile:return {'status':'waiting_information','message':'缺少已确认健康计划或档案。'}
            if profile.get('needs_professional_support') is not False:return {'status':'outside_scope'}
            profile=self.service.memory.profile(profile,self.service.memory.recall(utcnow().astimezone(ZoneInfo(policy['timezone_name'])).date().isoformat()))
            if event['status']=='tentative':return {'status':'tentative','message':'邀请尚未确认，不自动占用日程。'}
            records=self.store.records();business_revision=self.store.revision();events=self.events()
            base=attach_quantification(normalize(active['body'],profile,policy),profile,records,utcnow().astimezone(ZoneInfo(policy['timezone_name'])).date().isoformat())
            statuses={t.get('logical_id'):t['status'] for t in self.continuity.list('task',limit=10000) if t.get('logical_id')}
            for field in ('daily_schedule','meal_schedule','sleep_schedule'):
                for entry in base.get(field,[]):entry['execution_status']=statuses.get(entry.get('logical_id'),'scheduled')
            with self.store.connection() as db:feedback_revision=db.execute("SELECT COALESCE(MAX(rowid),0) FROM personal_journal WHERE kind='feedback'").fetchone()[0]
        # 模型与网络请求在全局锁外执行，提交时重新验证全部依赖。
        with self.store.connection() as db:
            recovered=db.execute("SELECT * FROM personal_actions WHERE json_extract(body,'$.job_id')=? AND status IN ('applied','rejected','observed')",(job['id'],)).fetchone()
            old=recovered or db.execute('SELECT * FROM personal_actions WHERE event_id=? AND event_revision=? AND base_version=?',(event['id'],event['revision'],active['version'])).fetchone()
        if old and old['status'] in ('applied','rejected','observed') and (old['status']=='applied' or json.loads(old['body']).get('business_revision')==business_revision and json.loads(old['body']).get('policy_revision')==policy['revision']):
            self.continuity.append('decision',old['id'],{'action_id':old['id'],'status':old['status'],'job_id':job['id'],'recovered':True},'decision-recovered:'+job['id'])
            self.notify('job:'+job['id'],'event',{'title':event['title'],'message':'已恢复此前处理结果。','action_id':old['id'],'job_id':job['id']})
            self.sync_state(payload.get('goal_id'));return {'status':old['status'],'action_id':old['id'],'replayed':True}
        preferred={}
        if event['status']=='cancelled':
            for action in self.actions():
                if action['event_id']!=event['id'] or action['status']!='applied':continue
                current={d.get('logical_id'):d for field in ('daily_schedule','meal_schedule','sleep_schedule') for d in base.get(field,[]) if d.get('logical_id')}
                originals={d.get('logical_id'):d for field in ('daily_schedule','meal_schedule','sleep_schedule') for d in action['body']['before_plan'].get(field,[]) if d.get('logical_id')}
                for change in action['body'].get('changes',[]):
                    item=current.get(change.get('logical_id'))
                    if item and item['start_at']==change['after']:
                        preferred[change['logical_id']]=originals.get(change['logical_id'],{'start_at':change['before'],'end_at':change['before_end'],'domain':change['domain']})
                break
        environment=self.environment(policy);goal_id=payload.get('goal_id') or self.current_goal()
        read_access=PersonalReadAccess(self.continuity,goal_id=goal_id)
        graph=build_event_graph(self._runner(payload.get('mode',policy['agent_mode'])),self.service.memory,environment,read_access,self.service.saver)
        dependency=hashlib.sha256(dump([active['version'],business_revision,policy['revision'],event['revision'],feedback_revision]).encode()).hexdigest()[:16]
        graph_thread='personal-job:'+job['id']+':'+dependency
        config={'configurable':{'thread_id':graph_thread},'recursion_limit':30,'max_concurrency':3}
        with self.store.connection() as db:db.execute('UPDATE personal_job_attempts SET thread_id=? WHERE job_id=? AND attempt=?',(graph_thread,job['id'],job['attempts']))
        checkpoint=graph.get_state(config)
        if checkpoint.values and not checkpoint.next:result=dict(checkpoint.values)
        elif checkpoint.values:result=graph.invoke(None,config)
        else:result=graph.invoke({'event':event,'events':events,'base_plan':base,'profile':profile,'records':records,
                                  'policy':policy,'now':utcnow().astimezone(ZoneInfo(policy['timezone_name'])).isoformat(),
                                  'role_results':[],'traces':[],'external_evidence':[],'preferred_slots':preferred},config)
        action_id=old['id'] if old else uuid4().hex
        body={'before_plan':active['body'],'business_revision':business_revision,'policy_revision':policy['revision'],
              'mode':payload.get('mode',policy['agent_mode']),'event_title':event['title'],'job_id':job['id'],'goal_id':goal_id,
              'feedback_revision':feedback_revision,'graph_thread_id':graph_thread,
              **{k:result[k] for k in ('candidate','changes','unresolved','impact','role_results','traces','approval_requirements')}}
        body['environment_evidence']=result.get('used_environment_evidence',environment.evidence)
        try:
            health=self.continuity.get('calendar_health','current')
            if not health.get('complete',True):body['unresolved'].append('日历存在未展开或遗漏的事件，需补齐可用性资料后再提交。')
        except KeyError:pass
        status='blocked' if body['unresolved'] else 'pending' if body['changes'] else 'observed'
        with self.store.connection() as db:
            db.execute('INSERT INTO personal_actions VALUES (?,?,?,?,?,?,?) ON CONFLICT(event_id,event_revision,base_version) DO UPDATE SET status=excluded.status,body=excluded.body',
                       (action_id,event['id'],event['revision'],active['version'],status,dump(body),stamp()))
        auto=self.auto_allowed(policy,body)
        if status=='pending' and auto:
            try:self.decide(action_id,True);status='applied'
            except ValueError:status='stale';self._action_status(action_id,'stale')
        self.continuity.append('decision',action_id,{'action_id':action_id,'event_id':event['id'],'event_revision':event['revision'],
                'goal_id':goal_id,'status':status,'changes':body['changes'],'unresolved':body['unresolved'],
                'approval_requirements':body['approval_requirements'],'job_id':job['id']},'decision-created:'+action_id+':'+job['id'])
        message={'applied':'已按授权调整健康事项并更新量化结果。','pending':'健康安排有可行调整，等待确认。',
                 'blocked':'存在尚未解决的约束，原计划未被覆盖。','observed':'事件已纳入工作上下文。','stale':'输入在规划期间变化，旧候选未应用。'}[status]
        self.notify('job:'+job['id'],'event',{'title':event['title'],'message':message,'action_id':action_id,'job_id':job['id']})
        self.sync_state(goal_id)
        return {'status':status,'action_id':action_id,'auto_allowed':auto,'changes':body['changes']}

    @staticmethod
    def auto_allowed(policy,body):
        if not policy['auto_adjust']:return False
        domains={c.get('domain','activity') for c in body['changes']}
        if not domains<=set(policy['auto_adjust_domains']):return False
        checks={'cross_day':policy['allow_cross_day'],'sleep_shift':policy['allow_sleep_shift'],'wake_shift':policy['allow_wake_shift'],
                'meal_shift':policy['allow_meal_shift'],'activity_shortening':policy['allow_activity_shortening'],
                'activity_environment_fallback':policy['auto_allow_environment_fallback'],'confirm_trip_location':False}
        return all(checks.get(flag,False) for flag in body.get('approval_requirements',[]))

    def _action_status(self,action_id,status):
        with self.store.connection() as db:db.execute('UPDATE personal_actions SET status=? WHERE id=?',(status,action_id))

    def decide(self,action_id,approved):
        with self.service.lock:return self._decide(action_id,approved)

    def _decide(self,action_id,approved):
        if approved:
            action=next((a for a in self.actions() if a['id']==action_id),None)
            if action and action['status']=='pending':
                with self.store.connection() as db:current_feedback=db.execute("SELECT COALESCE(MAX(rowid),0) FROM personal_journal WHERE kind='feedback'").fetchone()[0]
                if action['body'].get('feedback_revision',current_feedback)!=current_feedback:raise ValueError('执行反馈已变化，请根据最新状态重新规划。')
                for evidence in action['body'].get('environment_evidence',[]):
                    if evidence.get('status')=='ok' and evidence.get('fetched_at') and utcnow()-datetime.fromisoformat(evidence['fetched_at'])>timedelta(minutes=self.policy()['environment_ttl_minutes']):raise ValueError('候选环境证据已过期，请重新查询。')
                    if evidence.get('status')=='ok' and evidence.get('location_basis') and self.environment().resolve(evidence.get('target_date',''))!=evidence['location_basis']:raise ValueError('候选使用的位置已变化或过期，请重新规划。')
        action=super().decide(action_id,approved)
        self.continuity.append('decision',action_id,{'action_id':action_id,'status':action['status'],'changes':action['body'].get('changes',[])},'decision-'+action['status']+':'+action_id+':'+action['body'].get('job_id','legacy'))
        self.sync_state(action['body'].get('goal_id'))
        return action

    def undo(self,action_id):
        result=super().undo(action_id)
        self.continuity.append('decision',action_id,{'action_id':action_id,'status':'undone'},'decision-undone:'+action_id)
        self.sync_state();return result

    def feedback(self,task_id,body):
        with self.service.lock:return self._feedback(task_id,body)

    def _feedback(self,task_id,body):
        result=self.continuity.feedback(task_id,body)
        if result.get('duplicate'):return result
        source=result.get('source_message_id');note=body.get('note','')
        from app.multi_agent.memory_parsing import explicit_proposals
        if source:
            for proposal in explicit_proposals(note,utcnow().date().isoformat()):self.service.memory.propose(proposal,source)
        self.sync_reminders();self.update_live(goal_id=result['task'].get('goal_id'))
        result['review_job']=self.runtime.enqueue('review',{'goal_id':result['task'].get('goal_id')},'feedback-review:'+result['feedback_id'])
        return result

    def review(self,goal_id=None,job_id=None):
        tasks=[t for t in self.continuity.list('task',limit=500) if not goal_id or t.get('goal_id')==goal_id]
        sources=[{'task_id':t['id'],'outcome':t['status'],'feedback':t.get('last_feedback'),'source_message_id':t.get('feedback_source_id')} for t in tasks if t.get('last_feedback')]
        records=self.store.records();policy=self.policy();today=utcnow().astimezone(ZoneInfo(policy['timezone_name'])).date().isoformat()
        recent=[r for r in records if (datetime.fromisoformat(today)-timedelta(days=6)).date().isoformat()<=r['record_date']<=today]
        fingerprint=hashlib.sha256(dump({'goal_id':goal_id,'sources':sources,'records':recent,'as_of':today}).encode()).hexdigest()
        try:return {**self.continuity.get('review',fingerprint),'cached':True}
        except KeyError:pass
        from app.multi_agent.tools import summarize_records
        summary=summarize_records(records,today)
        if not sources and not summary['recorded_days']:return {'status':'quiet','message':'没有新增反馈或记录，未推断执行情况。'}
        from app.multi_agent.context_builder import bound_tool_result
        context={'records':recent,'as_of':today,'previous_plan':(self.store.active_plan() or {}).get('body'),
                 'task_feedback':bound_tool_result({'feedback':sources[:20]},5000),
                 'skill':SkillCatalog().read('evening-review')['content']}
        context,_=ContextBuilder().build('review',context)
        trace=[];result=self._runner(policy['agent_mode']).run('review',context,Review,trace=trace,
                    retrieval=self.service.memory.agent_access('review',today,context),extra_bindings=PersonalReadAccess(self.continuity,goal_id=goal_id).bindings())
        observation=self.continuity.put('observation',fingerprint[:32],{'goal_id':goal_id,'kind':'hypothesis','statements':result['observations'],
                  'recommendations':result['recommendations'],'summary':summary,'evidence':sources[:20],
                  'note':'描述性复盘和待验证观察，不自动改变长期偏好。'},'pending_review')
        review=self.continuity.put('review',fingerprint,{'goal_id':goal_id,'observation_id':observation['id'],'result':result,'summary':summary,
                                                      'source_count':len(sources),'trace':trace})
        if job_id:self.notify('review:'+job_id,'review',{'title':'健康执行复盘','message':'已有基于记录的复盘与待验证观察，可查看依据。','review_id':fingerprint})
        self.update_live(goal_id=goal_id);return review

    def consolidate(self,job_id):
        try:cursor=self.continuity.get('cursor','memory')['rowid']
        except KeyError:cursor=0
        with self.store.connection() as db:rows=db.execute("SELECT rowid AS sequence,id,role,content,created_at FROM messages WHERE rowid>? ORDER BY rowid LIMIT 40",(cursor,)).fetchall()
        if not rows:return {'status':'quiet','reason':'没有尚未整理的消息。'}
        end=rows[-1]['sequence'];sources=[{'id':r['id'],'role':r['role'],'excerpt':r['content'][:300],'created_at':r['created_at']} for r in rows]
        # 第一版只生成可追溯情节索引，不把助手回答提炼为个人事实。
        episode=self.continuity.put('episode',f'{cursor}:{end}',{'source_start':cursor,'source_end':end,'sources':sources,
                    'open_work':self.context()['next_steps'],'facts_promoted':False,'note':'原文在消息仓储，截断片段不是完整原话。'})
        self.continuity.put('cursor','memory',{'rowid':end})
        return {'status':'indexed','episode_id':episode['id'],'source_count':len(rows)}

    def environment_check(self,job):
        active=self.store.active_plan();policy=self.policy()
        if not active or not policy['allow_environment_queries']:return {'status':'quiet'}
        upcoming=[d for d in active['body'].get('daily_schedule',[]) if d.get('kind')=='exercise' and utcnow()<datetime.fromisoformat(d['start_at'])<utcnow()+timedelta(hours=3)]
        if not upcoming:return {'status':'quiet','reason':'临近没有已安排活动。'}
        env=self.environment(policy);evidence=env.query('outing',upcoming[0]['date'])
        data=evidence.get('data') or {};weather=(data.get('weather') or {}).get('data') or {}
        material={'status':evidence.get('status'),'forecast':[f for f in weather.get('forecast',[]) if f.get('time')==upcoming[0]['date']],
                  'venues':[(c['id'],c.get('route',{}).get('status'),c.get('return_route',{}).get('status')) for c in data.get('candidates',[])]}
        fingerprint=hashlib.sha256(dump(material).encode()).hexdigest();key='environment:'+upcoming[0]['date']
        try:
            old=self.continuity.get('environment_marker',key)
            if old['fingerprint']==fingerprint:return {'status':'quiet','reason':'环境条件没有可处理变化。'}
        except KeyError:pass
        self.continuity.put('environment_marker',key,{'fingerprint':fingerprint,'evidence_id':evidence.get('id')})
        if evidence.get('status')!='ok' or (data.get('weather') or {}).get('status')!='ok' or not material['forecast']:
            self.notify('environment:'+fingerprint,'environment',{'title':'活动前环境待核实','message':'环境查询缺失，不把它当晴天或路线可用。','evidence':evidence})
            return {'status':'weather_unverified' if evidence.get('status')=='ok' else evidence.get('status')}
        event=LifeEvent(source='environment',external_id=key,title='活动前环境变化',kind='other',status='confirmed',
                        start_at=upcoming[0]['start_at'],end_at=upcoming[0]['end_at'],occupies_time=False,trigger='environment')
        # 指纹进入事件来源，条件变更才形成新版本；同一条件不会持续改期。
        body=event.model_dump(mode='json');body['source_text']='环境证据 '+fingerprint
        return self.ingest(body)

    def reconcile_schedules(self):
        policy=self.policy()
        definitions=[('system-calendar','日历增量同步','calendar_sync','interval',1,policy['calendar_enabled'] and bool(settings.calendar_ics_url)),
                     ('system-review','每日健康复盘','review','daily',60,policy['reviews_enabled']),
                     ('system-environment','临近活动环境检查','environment_check','interval',30,policy['allow_environment_queries'])]
        existing={s['id']:s for s in self.runtime.schedules()}
        for key,title,kind,frequency,interval,enabled in definitions:
            body=ScheduleInput(title=title,kind=kind,frequency=frequency,time=policy['review_time'],interval_minutes=interval,timezone_name=policy['timezone_name'],enabled=enabled).model_dump()
            if key not in existing or existing[key]['body']!=body:self.runtime.add_schedule(body,key)

    def maintenance(self):
        super().tick()
        self.deliver()

    def deliver(self):
        if self.policy()['delivery_enabled']:self.delivery.webhook()
