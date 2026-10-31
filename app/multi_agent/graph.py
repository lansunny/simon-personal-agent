"""The main lesson: explicit branches, parallel join, bounded repair and interrupt."""
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from datetime import date
import copy

from app.multi_agent.state import ActivityAdvice, Analysis, DietAdvice, GraphState, Plan, Review, IntentPlan, AdviceAnswer, SleepAdvice
from app.multi_agent.routing import normalize_intent
from app.multi_agent.schedule import make_schedule
from app.multi_agent.dining import attach_dining
from app.multi_agent.context_builder import ContextBuilder
from app.multi_agent.quantification import attach_quantification
from app.personal.planning import adapt_schedule
from app.personal.schedule_engine import normalize
from app.personal.nutrition import bind_meals
from app.personal.contracts import AutomationPolicy
from datetime import datetime
from app.config import settings
from app.integrations.mcp_client import call_tools
from app.multi_agent.tools import recent_records, summarize_records
from app.multi_agent.rules import validate_plan, diff_plans


def build_graph(runner, checkpointer, memory=None, capability_factory=None, external_caller=None, meal_templates_provider=None):
    context_builder = ContextBuilder(settings.context_budget_units)
    def run_role(role, context, output_type, source_message_id=None):
        trace = []
        context = {**context, 'source_message_id': source_message_id}
        extras=capability_factory(role,context) if capability_factory else None
        context, report = context_builder.build(role, context)
        trace.append(f"{role}: context_units={report['after_units']}/{report['budget_units']}; reduced={','.join(report['reduced_fields']) or 'none'}")
        retrieval = memory.agent_access(role, context.get('as_of') or date.today().isoformat(), context) if memory else None
        result = runner.run(role,context,output_type,trace=trace,retrieval=retrieval,extra_bindings=extras)
        return result, trace, retrieval.evidence if retrieval else []

    def intake(state):
        context = {'profile': state['profile'], 'previous_plan': state.get('previous_plan'),
                   'user_message': state.get('user_message', ''), 'dialogue': state.get('dialogue', []),
                   'latest_user_message': next((t['content'] for t in reversed(state.get('dialogue', [])) if t.get('role') == 'user'), state.get('user_message', '')),
                   'memory_context': state.get('memory_context', []),
                   'location_available': bool(state.get('location')), 'local_now': state.get('local_now'), 'task_city': state.get('task_city', ''), 'as_of': state.get('as_of') or date.today().isoformat()}
        result, trace, supplements = run_role('router', context, IntentPlan, state.get('source_message_id'))
        task_memories = memory.recall(result.get('target_date') or state.get('as_of')) if memory else state.get('memory_context', [])
        result = normalize_intent(result, {**state, 'memory_context': task_memories})
        return {'retrieved_evidence': supplements, 'memory_context': task_memories, 'intent_plan': result, 'clarification_question': result['question'],
                'events': trace + ['intake: ' + result['intent'] + ' / ' + ','.join(result['sections'])]}

    def after_intake(state):
        if state['intent_plan']['intent'] != 'clarify':
            return 'prepare'
        return 'clarify_task' if state.get('clarification_round', 0) < 4 else 'clarification_limit'

    def clarify_task(state):
        answer = interrupt({'kind': 'clarification', 'question': state['clarification_question']})
        history = list(state.get('dialogue', []))
        history += [{'role': 'assistant', 'content': state['clarification_question']},
                    {'role': 'user', 'content': answer['message']}]
        return {'dialogue': history, 'location': answer.get('location') or state.get('location'), 'task_city': answer.get('city') or state.get('task_city', ''),
                'clarification_round': state.get('clarification_round', 0) + 1,
                'events': ['clarify_task: 已收到补充信息，重新识别意图']}

    def clarification_limit(state):
        return {'status': 'needs_clarification', 'events': ['clarify: 四轮后仍不明确，请重新描述任务']}

    def retrieve_memory(state):
        if memory is None:
            return {'history_evidence': [], 'events': ['retrieve_memory: 未配置仓储']}
        plan = state['intent_plan']
        query = plan.get('history_query', {})
        evidence = [memory.history(query, state['as_of'])] if query.get('kind', 'none') != 'none' else []
        topics = set(plan['sections']) & {'diet', 'activity'}
        if plan['intent'] in ('review', 'lookup'):
            topics = None
        update = {'memory_context': memory.recall(plan.get('target_date') or state['as_of'], topics or None), 'history_evidence': evidence,
                  'events': ['retrieve_memory: 读取有效记忆与指定范围历史，不加载全部存档']}
        if evidence and query.get('kind') == 'records' and plan['intent'] == 'review':
            update['summary'] = evidence[0]['data']['summary']
        return update

    def external(state):
        plan = state['intent_plan']
        requests, evidence = [], list(state.get('history_evidence', []))
        for name in plan['external_tools']:
            if name != 'ingredients' and (state.get('mode', runner.mode) != 'live' or not state.get('use_external')):
                evidence.append({'tool': name, 'status': 'disabled', 'message': '离线模式或未开启联网，本次未查询。'})
                continue
            if name == 'weather':
                args = {'city': plan['city'], 'target_date': plan['target_date']}
                args.update({k:v for k,v in (state.get('location') or {}).items() if k in ('latitude','longitude')})
            elif name == 'venues':
                if state.get('location'):
                    # GPS起点已有坐标，场地搜索无需先由用户补城市名。
                    requests.append({'name':'outing','arguments':{'city':plan['city'],
                        'start_place':plan['start_place'],
                        **{k:v for k,v in state['location'].items() if k in ('latitude','longitude')},
                        'categories':plan.get('geo_categories'),'radius_m':plan.get('search_radius_m',2000)}})
                    continue
                args = {'city': plan['city'], 'query': plan['public_query'] or '运动场馆'}
            else:
                args = {'query': plan['public_query']}
            requests.append({'name': name, 'arguments': args})
        if plan['intent'] in ('plan', 'adjust') and 'activity' in state.get('selected_workers', []) and plan['activity_type'] != 'indoor':
            # 规划自动考虑环境，无需用户再逐项要求天气/场地工具。
            location = state.get('location') or {}
            cached = state.get('prefetched_environment')
            if state.get('mode', runner.mode) == 'live' and state.get('use_external'):
                requests = [r for r in requests if r['name'] not in ('weather', 'venues','outing')]
                requests.insert(0, {'name': 'outing', 'arguments': {'city': plan['city'], 'place_name': plan['place_name'],
                    'start_place': plan['start_place'], 'latitude': location.get('latitude'), 'longitude': location.get('longitude'),
                    'categories': plan.get('geo_categories'), 'radius_m': plan.get('search_radius_m', 2000)}})
            elif cached and not plan['place_name']:
                evidence.append(dict(cached['evidence']))
            else:
                evidence.append({'tool': 'outing', 'status': 'disabled', 'message': '本次未联网，天气与路线尚未核实。'})
        if plan.get('dining_mode') == 'eat_out' and plan['intent'] in ('plan', 'adjust', 'lookup'):
            if state.get('mode', runner.mode) == 'live' and state.get('use_external'):
                location = state.get('location') or {}
                requests.insert(0, {'name': 'restaurants', 'arguments': {'city': plan['city'],
                    'start_place': plan['start_place'], 'latitude': location.get('latitude'), 'longitude': location.get('longitude'),
                    'categories': plan.get('restaurant_categories'), 'radius_m': plan.get('search_radius_m', 2000)}})
            else:
                evidence.append({'tool': 'restaurants', 'status': 'disabled', 'message': '离线模式或未允许联网，未查询餐馆。'})
        if requests:
            location=state.get('location') or {}
            if location.get('accuracy_m') is not None and location['accuracy_m']>1000:
                blocked=[r for r in requests if r['name'] in ('outing','restaurants')]
                evidence += [{'tool':r['name'],'status':'missing_input','message':'当前坐标精度不足，请重新定位或确认具体起点后查询路线。'} for r in blocked]
                requests=[r for r in requests if r['name'] not in ('outing','restaurants')]
            if requests:evidence += (external_caller or call_tools)(requests[:4])
            evidence += [{'tool': r['name'], 'status': 'disabled', 'message': '本轮最多四项外部查询，优先地图规划。'} for r in requests[4:]]
        accuracy=(state.get('location') or {}).get('accuracy_m')
        if accuracy is not None and accuracy>100:
            for item in evidence:
                if item.get('tool') in ('outing','restaurants'):item['precision_note']=f'起点精度约{accuracy:g}米，距离和路线需按近似起点理解。'
        for index, item in enumerate(evidence):
            item['id'] = f'E{index + 1}'
        return {'external_evidence': evidence,
                'events': ['MCP ' + item['tool'] + ': ' + item['status'] for item in evidence]}

    def dispatch(state):
        if state['intent_plan']['intent'] == 'lookup':
            return ['answer_query']
        return state['selected_workers']

    def answer_query(state):
        context = {'user_message': state.get('user_message', ''), 'dialogue': state.get('dialogue', []),
                   'external_evidence': state.get('external_evidence', []), 'memory_context': state.get('memory_context', []),
                   'as_of': state.get('as_of'), 'retrieved_evidence': state.get('retrieved_evidence', []), 'life_events': state.get('life_events', [])[:12], 'additional_event_count': max(0, len(state.get('life_events', []))-12)}
        result, trace, supplements = run_role('advisor', context, AdviceAnswer, state.get('source_message_id'))
        known = {item['id'] for item in state.get('external_evidence', []) + state.get('retrieved_evidence', []) + supplements if item.get('status') == 'ok'}
        result['evidence_ids'] = [eid for eid in result['evidence_ids'] if eid in known]
        return {'retrieved_evidence': supplements, 'answer': result, 'status': 'answered', 'events': trace + ['answer: 信息查询结束，不修改计划']}

    def gather(state):
        return {'events': ['gather: 本次所选分支全部完成']}

    def finish_review(state):
        return {'status': 'reviewed', 'answer': {'text': '；'.join(state['review']['observations'] + state['review']['recommendations']), 'evidence_ids': []},
                'events': ['review: 仅输出复盘，不创建候选计划']}

    def analyze(state):
        trace, supplements = [], []
        # Do not allow model output to bypass the confirmed scope field.
        support = state["profile"].get("needs_professional_support")
        if support is None:
            result = {"route": "clarify", "reason": "请先确认是否需要专业支持。"}
        elif support is True:
            result = {"route": "professional_support", "reason": "此演示不生成需要专业支持的个性化方案。"}
        else:
            context = {'user_message': state.get('user_message', ''), 'dialogue': state.get('dialogue', []), "profile": state["profile"], "records": recent_records(state['records'], state.get('as_of')), 'memory_context': state.get('memory_context', [])}
            result, trace, supplements = run_role('analysis', context, Analysis, state.get('source_message_id'))
        return {"retrieved_evidence": supplements, "analysis": result, "events": trace + ["analysis: " + result["route"]]}

    def clarify(state):
        return {"status": "needs_clarification", "events": ["clarify: 请补充档案后开启新任务"]}

    def refer(state):
        return {"status": "professional_support", "events": ["refer: 转专业支持，不生成计划"]}

    def prepare(state):
        as_of = state.get('as_of') or date.today().isoformat()
        intent = state['intent_plan']
        effective = dict(state.get('base_profile', state['profile']))
        if memory:
            effective = memory.profile(effective, state.get('memory_context', []))
        for key, value in intent['temporary'].items():
            if value is not None:
                effective[key] = list(dict.fromkeys(effective.get(key, []) + value)) if key == 'activity_constraints' else value
        previous = state.get('previous_plan')
        selected = intent['sections'] if intent['intent'] != 'lookup' else []
        return {'status': 'planning', 'as_of': as_of, 'effective_profile': effective,
                'selected_workers': selected,
                'recent_records': recent_records(state['records'], as_of),
                'summary': summarize_records(state['records'], as_of),
                'task_kind': intent['intent'],
                'sleep': {'suggestions': previous.get('sleep_suggestions', []) if previous else [], 'rationale': '保留旧作息'},
                'diet': {'suggestions': previous['diet_suggestions'] if previous else [], 'rationale': '保留旧计划未请求修改部分'},
                'activity': {'days': previous['activity_days'] if previous else 0,
                             'minutes': previous['minutes_per_session'] if previous else 0,
                             'suggestions': previous['activity_suggestions'] if previous else []},
                'review': {'evidence_status': 'limited', 'observations': [], 'adjustment': 'collect_more', 'recommendations': []},
                'events': ['prepare: 临时约束仅作用于本次任务；所选角色=' + ','.join(selected)]}

    def worker_context(state):
        return {'profile': state.get('effective_profile', state['profile']), 'records': state['recent_records'],
                'previous_plan': state.get('previous_plan'), 'as_of': state['as_of'],
                'user_message': state.get('user_message', ''), 'dialogue': state.get('dialogue', []),
                'external_evidence': state.get('external_evidence', []), 'intent_plan': state.get('intent_plan', {}),
                'allow_external':state.get('use_external',False),'local_now': state.get('local_now'), 'memory_context': state.get('memory_context', []),
                'retrieved_evidence': state.get('retrieved_evidence', []), 'life_events': state.get('life_events', [])[:12], 'additional_event_count': max(0, len(state.get('life_events', []))-12)}

    def diet(state):
        result, trace, supplements = run_role('diet', worker_context(state), DietAdvice, state.get('source_message_id'))
        return {'retrieved_evidence': supplements, 'diet': result, 'events': trace + ['diet: 饮食草稿完成']}

    def activity(state):
        result, trace, supplements = run_role('activity', worker_context(state), ActivityAdvice, state.get('source_message_id'))
        return {'retrieved_evidence': supplements, 'activity': result, 'events': trace + ['activity: 活动草稿完成']}

    def sleep(state):
        result, trace, supplements = run_role('sleep', worker_context(state), SleepAdvice, state.get('source_message_id'))
        return {'sleep': result, 'retrieved_evidence': supplements, 'events': trace + ['sleep: 作息建议完成']}

    def review(state):
        context = {'records': state['recent_records'], 'previous_plan': state.get('previous_plan'), 'as_of': state['as_of'],
                   'memory_context': state.get('memory_context', []), 'retrieved_evidence': state.get('retrieved_evidence', []), 'history_summary': state['summary'] if state.get('history_evidence') and state['intent_plan'].get('history_query', {}).get('kind') == 'records' else None}
        result, trace, supplements = run_role('review', context, Review, state.get('source_message_id'))
        return {'retrieved_evidence': supplements, 'review': result, 'events': trace + ['review: 描述性复盘完成']}

    def coordinate(state):
        context = {key: state[key] for key in ("profile", "diet", "activity", "review")}
        context['life_events'] = state.get('life_events', [])[:12]
        context['additional_event_count'] = max(0, len(state.get('life_events', []))-12)
        context['sleep'] = state.get('sleep', {'suggestions': []})
        context['profile'] = state.get('effective_profile', state['profile'])
        context['requested_sections'] = state.get('selected_workers', ['diet', 'activity', 'review'])
        context['user_message'] = state.get('user_message', '')
        context['dialogue'] = state.get('dialogue', [])
        context['external_evidence'] = state.get('external_evidence', [])
        context["validation_errors"] = state.get("errors", [])
        context["previous_proposal"] = state.get("proposal")
        context['previous_plan'] = state.get('previous_plan')
        context['as_of'] = state['as_of']
        context['intent_plan'] = state.get('intent_plan', {})
        context['allow_external']=state.get('use_external',False)
        context['summary'] = state['summary']
        context['memory_context'] = state.get('memory_context', [])
        context['retrieved_evidence'] = state.get('retrieved_evidence', [])
        number = state.get("coordination_round", 0) + 1
        result, trace, supplements = run_role('coordinator', context, Plan, state.get('source_message_id'))
        # 第一阶段仅做可验证的局部保护；第二阶段再做角色定向返工。
        previous = state.get('previous_plan')
        if previous:
            if 'diet' not in context['requested_sections']:
                result['diet_suggestions'] = previous['diet_suggestions']
            if 'activity' not in context['requested_sections']:
                for key in ('activity_days', 'minutes_per_session', 'activity_suggestions'):
                    result[key] = previous[key]
        result = make_schedule(result, state)
        if previous and 'sleep' not in context['requested_sections']:
            result['sleep_suggestions'] = previous.get('sleep_suggestions', [])
        result = attach_dining(result, state)
        policy = AutomationPolicy().model_dump() | {k:v for k,v in state.get('automation_policy', {}).items() if k in AutomationPolicy.model_fields}
        result['sleep_schedule']=copy.deepcopy(previous.get('sleep_schedule',[])) if previous and 'sleep' not in context['requested_sections'] else []
        result['meal_schedule']=copy.deepcopy(previous.get('meal_schedule',[])) if previous and 'diet' not in context['requested_sections'] else []
        result=normalize(result,context['profile'],policy)
        # 这些默认用餐窗口将与整个草稿一起展示和确认，旧生效计划的派生窗口则仍标为假设。
        if 'diet' in context['requested_sections']:
            for meal in result.get('meal_schedule',[]):meal['assumed']=False
        if meal_templates_provider and 'diet' in context['requested_sections'] and state.get('intent_plan',{}).get('dining_mode')!='eat_out':
            result=bind_meals(result,state['diet'].get('meal_choices',[]),meal_templates_provider())
        if state.get('life_events') and state.get('local_now'):
            policy = AutomationPolicy().model_dump() | {k:v for k,v in state.get('automation_policy', {}).items() if k in AutomationPolicy.model_fields}
            result, shifts, remaining = adapt_schedule(result, state['life_events'], policy, context['profile'], datetime.fromisoformat(state['local_now']))
            result['schedule_conflicts'] = remaining
            result['changes'] += [f"{c['date']}为避开日程占用，开始时间调整为{c['after'][11:16]}。" for c in shifts]
        result = attach_quantification(result, context['profile'], state.get('quantitative_records',state['records']), state['as_of'])
        return {"retrieved_evidence": supplements, "proposal": result, 'plan_diff': diff_plans(state.get('previous_plan'), result),
                "coordination_round": number, "events": trace + [f"coordinate: 第 {number} 次协调"]}

    def validate(state):
        proposal, profile = state["proposal"], state.get("effective_profile", state["profile"])
        errors = validate_plan(proposal, profile, state['review'], state.get('previous_plan'), state.get('selected_workers'))
        return {"errors": errors, "events": ["validate: " + ("；".join(errors) if errors else "业务约束通过")]}

    def after_validation(state):
        if not state["errors"]:
            return "confirm"
        return "coordinate" if state["coordination_round"] < 2 else "blocked"

    def blocked(state):
        return {"status": "needs_manual_revision", "events": ["blocked: 修改一次仍有冲突，停止"]}

    def confirm(state):
        decision = interrupt({"message": "请确认这份候选周计划", "proposal": state["proposal"],
                              'changes': state.get('plan_diff', []), "mode": state.get('mode', runner.mode)})
        # Node restarts on resume: no write side effects before interrupt.
        accepted = isinstance(decision, dict) and decision.get("approved") is True
        update = {"status": "approved" if accepted else "rejected", "events": ["confirm: " + ("用户确认" if accepted else "用户拒绝")]}
        if accepted:
            update["active_plan"] = state["proposal"]
        return update

    graph = StateGraph(GraphState)
    for name, node in {"analyze": analyze, "clarify": clarify, "refer": refer,
                       "prepare": prepare, "diet": diet, "activity": activity,
                       "sleep": sleep, "review": review, "coordinate": coordinate, "validate": validate,
                       "blocked": blocked, "confirm": confirm}.items():
        graph.add_node(name, node)
    for name, node in {'intake': intake, 'clarify_task': clarify_task, 'clarification_limit': clarification_limit,
                       'retrieve_memory': retrieve_memory, 'external': external, 'answer_query': answer_query, 'gather': gather, 'finish_review': finish_review}.items():
        graph.add_node(name, node)
    graph.add_edge(START, "analyze")
    graph.add_conditional_edges("analyze", lambda s: s["analysis"]["route"],
                                {"plan": "intake", "clarify": "clarify", "professional_support": "refer"})
    graph.add_conditional_edges('intake', after_intake, {'prepare': 'prepare', 'clarify_task': 'clarify_task', 'clarification_limit': 'clarification_limit'})
    graph.add_edge('clarify_task', 'intake')
    graph.add_edge('prepare', 'retrieve_memory')
    graph.add_edge('retrieve_memory', 'external')
    graph.add_conditional_edges('external', dispatch, ['diet', 'activity', 'sleep', 'review', 'answer_query'])
    # 所选分支均为单节点，处于同一 superstep；gather 在本轮所有分支完成后执行一次。
    for worker in ('diet', 'activity', 'sleep', 'review'):
        graph.add_edge(worker, 'gather')
    graph.add_conditional_edges('gather', lambda s: 'finish_review' if s['intent_plan']['intent'] == 'review' else 'coordinate',
                                {'finish_review': 'finish_review', 'coordinate': 'coordinate'})
    for node in ('answer_query', 'finish_review', 'clarification_limit'):
        graph.add_edge(node, END)
    graph.add_edge("coordinate", "validate")
    graph.add_conditional_edges("validate", after_validation,
                                {"confirm": "confirm", "coordinate": "coordinate", "blocked": "blocked"})
    for final_node in ("clarify", "refer", "blocked", "confirm"):
        graph.add_edge(final_node, END)
    return graph.compile(checkpointer=checkpointer)
