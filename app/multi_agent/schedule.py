"""把已校验的活动预算落到七天日程；地理细节只取工具返回或用户原话。"""
from datetime import datetime, timedelta, timezone
import math
from app.multi_agent.weather_rules import assess_forecast


def make_schedule(plan, state):
    intent = state.get('intent_plan', {})
    previous = state.get('previous_plan') or {}
    if 'activity' not in state.get('selected_workers', ['activity']) and previous.get('daily_schedule'):
        plan['daily_schedule'] = previous['daily_schedule']
        plan['planning_context'] = previous.get('planning_context', {})
        return plan
    now = datetime.fromisoformat(state['local_now']) if state.get('local_now') else datetime.now().astimezone()
    chosen_time = intent.get('preferred_time') or '19:00'
    hour, minute = map(int, chosen_time.split(':'))
    start = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if intent.get('target_date'):
        requested = datetime.fromisoformat(intent['target_date']).date()
        if requested >= now.date():
            start = start.replace(year=requested.year, month=requested.month, day=requested.day)
    if start <= now:
        start += timedelta(days=1)
    evidence = state.get('external_evidence', [])
    outing = next((e.get('data') for e in evidence if e['tool'] == 'outing' and e.get('data')), {})
    forecasts = list(outing.get('weather', {}).get('data', {}).get('forecast', [])) if outing.get('weather', {}).get('data') else []
    # 独立天气查询同样是有效证据，不仅仅读outing内嵌的天气。
    forecasts += [row for e in evidence if e.get('tool')=='weather' and e.get('status')=='ok' for row in (e.get('data') or {}).get('forecast',[])]
    weather_by_date = {r['time']: r for r in forecasts}
    candidates = {c['id']: c for c in outing.get('candidates', []) if c.get('id')}
    choices = {c['weekday']: c for c in state.get('activity', {}).get('venue_choices', [])}
    origin = intent.get('start_place') or ('本次授权定位点' if state.get('location') else '待确认的出发点')
    days, budget = plan['activity_days'], plan['minutes_per_session']
    selected = {math.floor(i * 7 / days) for i in range(days)} if days else set()
    if intent.get('available_weekdays'):
        available_dates = [i for i in range(7) if (start + timedelta(days=i)).weekday() in intent['available_weekdays']]
        selected = set(available_dates[:days])
        plan['activity_days'] = len(selected)
    sessions = []
    for i in range(7):
        when = start + timedelta(days=i)
        if i not in selected or budget == 0:
            sessions.append({'date': when.date().isoformat(), 'kind': 'rest', 'title': '休息与轻松日常活动', 'minutes': 0})
            continue
        forecast = weather_by_date.get(when.date().isoformat())
        weather_assessment=assess_forecast(forecast)
        bad_weather=weather_assessment['status']=='avoid_outdoor'
        unknown_weather=weather_assessment['status']=='unknown'
        choice = choices.get(when.weekday(), {})
        candidate = candidates.get(choice.get('candidate_id'), {})
        route = candidate.get('route', {})
        back_route = candidate.get('return_route', {})
        venue = candidate.get('name', '未选择可用场地')
        verified = route.get('status') == 'ok' and back_route.get('status') == 'ok'
        travel = math.ceil(float(route.get('duration_seconds', 0)) / 60) if verified else 0
        back = math.ceil(float(back_route.get('duration_seconds', 0)) / 60) if verified else 0
        too_far = verified and travel + back + 5 >= budget
        indoor = (intent.get('activity_type') == 'indoor' or choice.get('activity') == 'indoor'
                  or bad_weather or unknown_weather or too_far or not verified)
        warmup = min(3, budget // 4)
        cooldown = min(2, budget // 4)
        if indoor:
            travel = back = 0
        active = budget - warmup - cooldown - travel - back
        sport = '室内慢走' if indoor else '轻松走跑交替' if choice.get('activity') == 'jog' else '轻松步行'
        segments = [{'name': '轻松热身', 'minutes': warmup}]
        if travel:
            segments.append({'name': f'从{origin}步行到{venue}', 'minutes': travel})
        segments.append({'name': sport, 'minutes': active})
        if back:
            segments.append({'name': f'按地图步行返回{origin}', 'minutes': back})
        segments.append({'name': '放慢步速与放松', 'minutes': cooldown})
        sessions.append({
            'date': when.date().isoformat(), 'kind': 'exercise', 'start_at': when.isoformat(),
            'end_at': (when + timedelta(minutes=budget)).isoformat(), 'minutes': budget,
            'title': f'{sport} · {"室内" if indoor else venue}', 'start_place': '安全的室内空间' if indoor else origin,
            'destination': '原地附近' if indoor else venue,
            'route_text': '保持地面平整、空间足够。' if indoor else
                f'{origin} → {venue} → 返回起点；到场后核对开放区域，不预设未知场内路线。',
            'route_status': 'indoor' if indoor else 'map_result' if route.get('status') == 'ok' else 'needs_confirmation',
            'route_steps': route.get('instructions', []) if not indoor else [],
            'one_way_distance_m': route.get('distance_m') if not indoor else None,
            'round_trip_distance_m': (route.get('distance_m',0)+back_route.get('distance_m',0)) if not indoor else None,
            'return_route_steps': back_route.get('instructions', []) if not indoor else [],
            'candidate_id': candidate.get('id') if not indoor else None,
            'selection_reason': choice.get('reason', '尚无 Agent 选址或有效路线，采用室内备选。'),
            'segments': [s for s in segments if s['minutes'] > 0],
            'weather': forecast,
            'weather_assessment':weather_assessment,
            'weather_note': '；'.join(weather_assessment['reasons']) or '日预报未触发当前不利天气规则，出发前仍需复核天气与场地。',
            'note': '场地往返超过时间预算，暂用室内方案。' if too_far else '未取得有效的场地选择和往返路线，暂用室内方案。' if not verified and intent.get('activity_type') != 'indoor' else '场地开放、费用与预约尚未核实，出发前确认。',
            'alternative': f'天气不合适或场地不可用时，在安全室内完成 {budget} 分钟轻松活动，可随时停止。',
        })
    plan['daily_schedule'] = sessions
    plan['planning_context'] = {'preferred_time': chosen_time, 'city': intent.get('city', ''),
                                'place_name': intent.get('place_name', ''), 'start_place': intent.get('start_place', ''),
                                'activity_type': intent.get('activity_type', 'walk'),
                                'available_weekdays': intent.get('available_weekdays', []),
                                'wants_reminder': intent.get('wants_reminder', False),
                                'timezone_offset': now.strftime('%z'), 'generated_at': now.isoformat()}
    return plan
