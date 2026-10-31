"""明确记忆句式的离线解析；数据库写入与用户确认位于业务层。"""
import re
from datetime import date, timedelta
from app.multi_agent.memory_contracts import MemoryProposal

def explicit_proposals(message, as_of):
    """无需模型的有限演示句式；所有提取仍需用户单独确认，不做隐含画像推断。"""
    if not any(word in message for word in ('记住', '以后', '长期', '这周', '今天', '明天')):
        return []
    day = date.fromisoformat(as_of)
    scope, end = 'long_term', None
    if '这周' in message:
        scope, end = 'temporary', (day + timedelta(days=6 - day.weekday())).isoformat()
    elif '明天' in message:
        scope, day = 'temporary', day + timedelta(days=1)
        end = day.isoformat()
    elif '今天' in message:
        scope, end = 'temporary', as_of
    updates = []
    def add(key, value):
        updates.append(MemoryProposal(key=key, value=value, scope=scope, valid_from=day.isoformat(), valid_until=end,
                                      reason='由用户明确表达提取，确认后生效。').model_dump())
    if re.search(r'(?:优先|喜欢|安排|选择|改成).*步行', message):
        add('activity.type', 'walk')
    elif re.search(r'(?:优先|喜欢|安排|选择|改成).*室内', message):
        add('activity.type', 'indoor')
    elif re.search(r'(?:优先|喜欢|安排|选择|改成).*慢跑', message):
        add('activity.type', 'jog')
    minutes = re.search(r'(?:每次|单次|只有|运动)\D{0,5}(\d{1,3})\s*分钟', message)
    if minutes and int(minutes[1]) <= 1440:
        add('activity.minutes', int(minutes[1]))
    time = re.search(r'(?:晚上|下午)?\s*(\d{1,2})(?:点|:)(\d{2})?', message)
    sleep_context=any(w in message for w in ('睡觉','入睡','上床','起床','睡眠','休息时间'))
    if time and not sleep_context and 0 <= int(time[1]) <= 23:
        hour = int(time[1])
        if any(word in message for word in ('晚上', '下午')) and hour < 12:
            hour += 12
        add('activity.time', f'{hour:02d}:{time[2] or "00"}')
    if sleep_context:
        clock=r'((?:[01]?\d|2[0-3]))(?::|点)([0-5]\d)?'
        bed=re.search(clock+r'\s*(?:睡觉|入睡|上床|休息)',message)
        wake=re.search(clock+r'\s*起床',message)
        for key,match in [('sleep.bedtime',bed),('sleep.wake_time',wake)]:
            if match:add(key,f'{int(match[1]):02d}:{match[2] or "00"}')
        hours=re.search(r'(?:睡够|睡眠目标|睡眠)\s*(\d+(?:\.\d+)?)\s*小时',message)
        if hours and 4<=float(hours[1])<=12:add('sleep.target_hours',float(hours[1]))
    pref = re.search(r'(?:喜欢吃|喜欢|优先推荐|优先吃|偏好是)([^，。；\n]{1,50})', message)
    if pref and not any(w in pref[1] for w in ('步行', '慢跑', '运动', '室内')):
        add('diet.preferences', [pref[1].strip()])
    if '不吃辣' in message:
        add('diet.preferences', ['不吃辣'])
    merged = {}
    for item in updates:
        if item['key'] == 'diet.preferences' and item['key'] in merged:
            item['value'] = list(dict.fromkeys(merged[item['key']]['value'] + item['value']))
        merged[item['key']] = item
    return list(merged.values())[:3]
