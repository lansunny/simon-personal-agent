"""第一阶段：结构化意图、临时约束与白名单工具计划。"""
import re
from datetime import date, timedelta
from app.multi_agent.state import IntentPlan
from app.multi_agent.memory_contracts import HistoryQuery
from app.multi_agent.memory_parsing import explicit_proposals


def number(text):
    if text.isdigit():
        return int(text)
    digits = {'零': 0, '一': 1, '二': 2, '两': 2, '三': 3, '四': 4, '五': 5, '六': 6, '七': 7, '八': 8, '九': 9}
    if text in digits:
        return digits[text]
    if '十' in text:
        left, right = text.split('十', 1)
        return digits.get(left, 1) * 10 + digits.get(right, 0)
    return None


def demo_history(message, as_of):
    """支持明确日期、上个月、计划版本和带引号关键词的离线查询。"""
    if not any(word in message for word in ('查询', '查看', '查一下', '查找', '找回', '复盘', '以前', '上次')):
        return None
    if '记忆' in message or '偏好' in message and '查看' in message:
        return HistoryQuery(kind='memories').model_dump()
    version = re.search(r'(?:第\s*([0-9]+)\s*版|[Vv]([0-9]+))', message)
    if version:
        return HistoryQuery(kind='plans', version=int(version[1] or version[2])).model_dump()
    days = re.findall(r'\d{4}-\d{2}-\d{2}', message)
    if days:
        return HistoryQuery(kind='records', start_date=days[0], end_date=days[-1]).model_dump()
    if '上个月' in message:
        end = date.fromisoformat(as_of).replace(day=1) - timedelta(days=1)
        return HistoryQuery(kind='records', start_date=end.replace(day=1).isoformat(), end_date=end.isoformat()).model_dump()
    quoted = re.search(r'[“"「](.+?)[”"」]', message)
    if quoted:
        return HistoryQuery(kind='messages', query=quoted[1][:100]).model_dump()
    return None


def demo_intent(context):
    """离线关键词解析，仅覆盖演示句式；真实模式由模型结构化提取。"""
    text = '\n'.join(item['content'] for item in context.get('dialogue', []) if item.get('role') == 'user') or context.get('user_message', '')
    query = demo_history(context.get('user_message', ''), context['as_of'])
    if query:
        return IntentPlan(intent='lookup', history_query=query, reason='离线历史查询，来自数据库，不修改计划。').model_dump()
    previous = context.get('previous_plan')
    memory = (previous or {}).get('planning_context', {})
    sections = []
    if any(w in text for w in ('吃', '饮食', '晚餐', '食堂', '食材', '餐食', '餐馆', '饭店', '餐厅')):
        sections.append('diet')
    if any(w in text for w in ('运动', '活动', '跑步', '步行', '健身', '场地', '球场', '天气', '两天', '分钟')):
        sections.append('activity')
    if any(w in text for w in ('睡眠', '作息', '睡觉', '休息时间')):
        sections.append('sleep')
    review = any(w in text for w in ('复盘', '执行得', '回顾', '体重变化'))
    tools = []
    for name, words in {'weather': ('天气', '下雨'), 'venues': ('场地', '场馆', '预约', '球场'),
                        'search': ('搜索', '联网', '查资料', '为什么'), 'ingredients': ('食材', '采购')}.items():
        if any(w in text for w in words):
            tools.append(name)
    wants_plan = any(w in text for w in ('计划', '安排', '调整', '修改', '换掉', '替换', '减少'))
    temporary = {}
    match = re.search(r'([0-7一二两三四五六七])\s*天', text)
    if match:
        temporary['available_activity_days'] = number(match[1])
        if 'activity' not in sections:
            sections.append('activity')
    matches = re.findall(r'([0-9零一二两三四五六七八九十]+)\s*分钟', text)
    if matches and number(matches[-1]) is not None and number(matches[-1]) <= 1440:
        temporary['minutes_per_session'] = number(matches[-1])
    if '食堂' in text:
        temporary['food_preferences'] = ['食堂餐食']
    if '室内' in text:
        temporary['activity_constraints'] = ['本次优先室内活动']
    city = context.get('task_city', '')
    city_matches = re.findall(r'(?:我在|城市是|地点是)([^，。！？\s]{2,20})', text)
    if city_matches:
        city = city_matches[-1]
    target = (date.fromisoformat(context['as_of']) + timedelta(days=1)).isoformat() if '明天' in text else ''
    if review and not wants_plan:
        intent, sections = 'review', ['review']
    elif tools and not wants_plan:
        intent = 'lookup'
    elif not text.strip() or any(w in text for w in ('完整计划', '制定计划', '生成计划', '第一份')):
        intent, sections = 'adjust' if previous else 'plan', ['diet', 'activity', 'sleep', 'review']
    elif sections:
        intent = 'adjust' if previous else 'plan'
    else:
        intent = 'clarify'
    query = '运动场馆' if 'venues' in tools else '体重日常波动 原因' if '为什么' in text else '成年人日常活动与饮食建议'
    weekdays = ['一二三四五六日'.index(w.replace('天', '日')) for w in re.findall(r'(?:周|星期)([一二三四五六日天])', text)]
    if not weekdays: weekdays = memory.get('available_weekdays', [])
    preferences = {m['key']: m['value'] for m in context.get('memory_context', [])}
    preferred_time = preferences.get('activity.time', memory.get('preferred_time', ''))
    times = re.findall(r'(?:([上下]午|晚上|早上|傍晚)\s*)?([0-9一二两三四五六七八九十]{1,3})(?:[:：]([0-5][0-9])|点(半)?)', text)
    if times:
        part, hour, minute, half = times[-1]
        hour = number(hour)
        if hour is not None and 0 <= hour < 24:
            if part in ('下午', '晚上', '傍晚') and hour < 12:
                hour += 12
            preferred_time = f'{hour:02d}:{minute or ("30" if half else "00")}'
    place_name = memory.get('place_name', '')
    matches = re.findall(r'(?:场地是|去|附近有个|附近有|旁边有个|旁边有)([^，。\n]{1,30}(?:生态园|公园)(?:[东南西北]门)?)', text)
    if matches:
        place_name = matches[-1]
    elif '生态园' in text:
        place_name = '用户提到的生态园'
    elif '公园' in text:
        place_name = '用户提到的公园'
    starts = re.findall(r'从([^，。\n]{1,30}?)(?:出发|开始)', text)
    start_place = starts[-1] if starts else memory.get('start_place', '')
    if '北门' in text and not start_place:
        start_place = '用户确认的北门'
    latest = next((item['content'] for item in reversed(context.get('dialogue', [])) if item.get('role') == 'user'), context.get('user_message', ''))
    activity_text = latest if any(w in latest for w in ('室内','慢跑','步行')) else context.get('user_message', '')
    activity_type = 'indoor' if '室内' in activity_text else 'jog' if '慢跑' in activity_text else 'walk' if '步行' in activity_text else preferences.get('activity.type', memory.get('activity_type', 'walk'))
    if '分钟' not in text and '半小时' in text:
        temporary['minutes_per_session'] = 30
    if any(w in text for w in ('永久修改' , '修改长期档案', '以后都改成')):
        return IntentPlan(intent='clarify', question='长期档案修改请在档案表单保存；本对话可以处理本次临时调整，请说明本次需求。', reason='不通过任务对话隐式修改长期档案。').model_dump()
    dining_mode = 'eat_out' if any(w in text for w in ('餐馆', '饭店', '餐厅', '外食', '外出吃')) else 'home' if '自己做饭' in text else 'unspecified'
    budget = re.findall(r'(?:预算|人均)[^0-9]{0,3}([0-9]+)', text)
    return IntentPlan(memory_updates=explicit_proposals(context.get('latest_user_message', context.get('user_message', '')), context['as_of']), dining_mode=dining_mode, meal_budget_yuan=int(budget[-1]) if budget and 1 <= int(budget[-1]) <= 10000 else None, intent=intent, preferred_time=preferred_time, available_weekdays=weekdays, place_name=place_name, start_place=start_place, activity_type=activity_type, wants_reminder=('提醒' in text or '闹钟' in text), sections=sections, temporary=temporary, city=city,
                      target_date=target, external_tools=tools, public_query=query,
                      question='你想调整饮食、活动，还是只查看执行复盘？请说明本次需求。' if intent == 'clarify' else '',
                      reason='离线规则识别；只作为本次任务约束，不修改长期档案。').model_dump()


def normalize_intent(plan, state):
    plan = IntentPlan.model_validate(plan).model_dump()
    plan['sections'] = list(dict.fromkeys(plan['sections']))
    plan['external_tools'] = list(dict.fromkeys(plan['external_tools']))
    if not plan['city']:
        plan['city'] = state.get('task_city', '') or (state.get('location') or {}).get('city','')
    if not plan['start_place']:
        plan['start_place']=(state.get('location') or {}).get('start_place','')
    preferences = {m['key']: m['value'] for m in state.get('memory_context', [])}
    if not plan['preferred_time']:
        plan['preferred_time'] = preferences.get('activity.time', '')
    if not any(w in state.get('user_message', '') for w in ('步行', '慢跑', '室内', '跑步')) and preferences.get('activity.type'):
        plan['activity_type'] = preferences['activity.type']
    if plan['target_date']:
        try:
            date.fromisoformat(plan['target_date'])
        except ValueError:
            plan.update(intent='clarify', question='请给出明确的查询日期，例如 2026-10-03。')
    if any(tool in plan['external_tools'] for tool in ('weather', 'venues')) and not plan['city'] and not state.get('location'):
        plan.update(intent='clarify', question='查询天气或场地需要地点。请在城市框填写城市，或回复“我在杭州”。')
    if plan.get('dining_mode') == 'eat_out' and plan['intent'] in ('plan', 'adjust') and 'diet' not in plan['sections']:
        plan['sections'].append('diet')
    if plan['intent'] in ('plan', 'adjust'):
        if not state.get('previous_plan'):
            plan['intent'] = 'plan'
            if not plan['sections']:
                plan['sections'] = ['diet', 'activity', 'sleep', 'review']
        elif not plan['sections']:
            plan.update(intent='clarify', question='请说明需要调整饮食、活动，还是两者都调整。')
        if any(plan['temporary'].get(k) is not None for k in ('available_activity_days', 'minutes_per_session', 'activity_constraints')) and 'activity' not in plan['sections']:
            plan['sections'].append('activity')
        if plan['temporary'].get('food_preferences') is not None and 'diet' not in plan['sections']:
            plan['sections'].append('diet')
    if plan['intent'] in ('plan', 'adjust') and 'activity' in plan['sections']:
        missing = []
        if not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', plan['preferred_time']):
            missing.append('你通常几点可以开始？例如晚上七点')
        if plan['activity_type'] != 'indoor' and not state.get('location') and not (plan['start_place'] and plan['city']):
            missing.append('点一下“使用当前位置”，或告诉我城市与具体出发地点，我会寻找附近场地；也可以选择室内活动')
        if missing:
            plan.update(intent='clarify', question='；'.join(missing) + '。')
    if plan.get('dining_mode') == 'eat_out' and plan['intent'] in ('plan', 'adjust', 'lookup') and not state.get('location') and not (plan['city'] and plan['start_place']):
        plan.update(intent='clarify', question='推荐附近餐馆需要起点：请点击使用当前位置，或告诉我城市和具体出发地点。')
    if plan['intent'] == 'review':
        plan['sections'] = ['review']
    if plan['intent'] == 'clarify' and not plan['question']:
        plan['question'] = '请补充你希望完成的具体任务。'
    return plan
