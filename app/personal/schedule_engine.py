"""健康日程约束求解与差异；在有限候选中取舍，不声称全局最优。"""
import copy
import math
from datetime import datetime,timedelta,timezone
from zoneinfo import ZoneInfo
from app.multi_agent.quantification import sleep_windows,attach_quantification,nutrition_total
from app.multi_agent.weather_rules import assess_forecast


def dt(value):return datetime.fromisoformat(value)
def overlap(a,b,c,d):return a<d and c<b
def minutes(a,b):return (b.astimezone(timezone.utc)-a.astimezone(timezone.utc)).total_seconds()/60


def event_blocks(events,zone,padding=0):
    return [(dt(e['start_at']).astimezone(zone)-timedelta(minutes=padding),dt(e['end_at']).astimezone(zone)+timedelta(minutes=padding))
            for e in events if e['status']=='confirmed' and (e.get('occupies_time') if e.get('occupies_time') is not None else e['kind']!='trip')]


def normalize(plan,profile,policy):
    plan=copy.deepcopy(plan);zone=ZoneInfo(policy['timezone_name'])
    for item in plan.get('daily_schedule',[]):
        if item.get('kind')=='exercise':
            item.setdefault('logical_id',f"activity:{item.get('origin_date',item['date'])}")
            item.setdefault('origin_date',item['date'])
    first=next((i['date'] for i in plan.get('daily_schedule',[])),datetime.now(zone).date().isoformat())
    if not plan.get('sleep_schedule'):plan['sleep_schedule']=sleep_windows({**profile,'timezone_name':policy['timezone_name']},first)
    for entry in plan['sleep_schedule']:
        if not entry.get('start_at'):
            start=dt(entry['date']+'T'+entry['bedtime']).replace(tzinfo=zone)
            end=dt(entry.get('wake_date',entry['date'])+'T'+entry['wake_time']).replace(tzinfo=zone)
            if end<=start:end+=timedelta(days=1)
            entry.update(start_at=start.isoformat(),end_at=end.isoformat())
        entry.setdefault('logical_id','sleep:'+entry['date'])
    if not plan.get('meal_schedule'):
        plan['meal_schedule']=[]
        for n in range(7):
            day=(dt(first)+timedelta(days=n)).date().isoformat()
            for meal,label in [('breakfast','早餐'),('lunch','午餐'),('dinner','晚餐')]:
                start=dt(day+'T'+policy[meal+'_time']).replace(tzinfo=zone)
                plan['meal_schedule'].append({'logical_id':f'meal:{day}:{meal}','date':day,'meal':meal,'title':label+'时间窗口',
                    'start_at':start.isoformat(),'end_at':(start+timedelta(minutes=policy['meal_duration_minutes'])).isoformat(),
                    'nutrition':nutrition_total([]),'assumed':True,'basis':'个人可修改的默认用餐时段，需随新候选确认，不是实际摄入记录。'})
    return plan


def detect_impact(plan,events,event,profile,policy):
    plan=normalize(plan,profile,policy);zone=ZoneInfo(policy['timezone_name']);blocks=event_blocks(events,zone,policy['event_padding_minutes'])
    result={'activity_conflicts':[],'sleep_conflicts':[],'meal_conflicts':[],
            'location_change':event['kind']=='trip' and event['status']=='confirmed',
            'dinner_event':event['kind']=='dinner' and event['status']=='confirmed','released_event':event['status']=='cancelled'}
    for field,key in [('daily_schedule','activity_conflicts'),('sleep_schedule','sleep_conflicts'),('meal_schedule','meal_conflicts')]:
        for item in plan.get(field,[]):
            if item.get('execution_status') in ('completed','partial','skipped'):continue
            if field=='daily_schedule' and item.get('kind')!='exercise':continue
            start,end=dt(item['start_at']),dt(item['end_at'])
            hits=[e for e in events if e['status']=='confirmed' and (e.get('occupies_time') if e.get('occupies_time') is not None else e['kind']!='trip') and overlap(start,end,dt(e['start_at'])-timedelta(minutes=policy['event_padding_minutes']),dt(e['end_at'])+timedelta(minutes=policy['event_padding_minutes']))]
            if hits:result[key].append({'logical_id':item['logical_id'],'date':item['date'],'event_ids':[e['id'] for e in hits],
                                      'start_at':item['start_at'],'end_at':item['end_at']})
    return result


def nearest_slot(original,duration,blocks,earliest,latest,max_shift,now,extra=None):
    choices=[]
    for offset in range(0,1440,5):
        start=original.replace(hour=0,minute=0,second=0,microsecond=0)+timedelta(minutes=offset)
        end=start+timedelta(minutes=duration)
        if start<=now or start<earliest or end>latest:continue
        if abs(minutes(original,start))>max_shift:continue
        if any(overlap(start,end,a,b) for a,b in blocks):continue
        if extra and not extra(start,end):continue
        choices.append(start)
    return min(choices,key=lambda t:(abs(minutes(original,t)),t)) if choices else None


def active_trip(events,day):
    return next((e for e in events if e['status']=='confirmed' and e['kind']=='trip' and e['start_at'][:10]<=day<=e['end_at'][:10]),None)


def environment_for(evidence,purpose,day):
    return next((e for e in reversed(evidence) if e.get('tool')==purpose and e.get('target_date')==day and e.get('status')=='ok'),None)


def apply_environment(item,trip,evidence,role_results,policy,force_indoor=False):
    original={k:copy.deepcopy(item.get(k)) for k in ('title','destination','candidate_id','route_status','round_trip_distance_m')}
    original['segments']=[(s['name'],s['minutes']) for s in item.get('segments',[])]
    day=item['date'];outing=environment_for(evidence,'outing',day)
    if not trip and not force_indoor and not outing:return False,[]
    flags=[];candidate=None
    if outing and not force_indoor:
        candidates=(outing.get('data') or {}).get('candidates',[])
        choices=role_results.get('activity',{}).get('venue_choices',[])
        weekday=dt(day).weekday()
        selected=next((c.get('candidate_id') for c in choices if c['weekday']==weekday),None)
        viable=[c for c in candidates if c.get('route',{}).get('status')=='ok' and c.get('return_route',{}).get('status')=='ok'
                and math.ceil(c['route']['duration_seconds']/60)+math.ceil(c['return_route']['duration_seconds']/60)+5<item['minutes']]
        candidate=next((c for c in viable if c['id']==selected),None)
        if not selected and viable:candidate=min(viable,key=lambda c:c['route']['duration_seconds']+c['return_route']['duration_seconds'])
    if candidate:
        go=math.ceil(candidate['route']['duration_seconds']/60);back=math.ceil(candidate['return_route']['duration_seconds']/60)
        warm=min(3,item['minutes']//4);cool=min(2,item['minutes']//4)
        item.update(title='轻松步行 · '+candidate['name'],destination=candidate['name'],candidate_id=candidate['id'],
                    route_status='map_result',route_steps=candidate['route'].get('instructions',[]),return_route_steps=candidate['return_route'].get('instructions',[]),
                    round_trip_distance_m=candidate['route'].get('distance_m',0)+candidate['return_route'].get('distance_m',0),
                    one_way_distance_m=candidate['route'].get('distance_m'),route_text='按本次查询路线往返；场内未知路线不编造。',
                    segments=[{'name':'轻松热身','minutes':warm},{'name':'从确认起点步行到场地','minutes':go},
                              {'name':'轻松步行','minutes':item['minutes']-warm-cool-go-back},
                              {'name':'按地图返回起点','minutes':back},{'name':'放松','minutes':cool}],environment_id=outing['id'])
    else:
        warm=min(3,item['minutes']//4);cool=min(2,item['minutes']//4)
        item.update(title='室内慢走备选',destination='用户需确认的室内空间',candidate_id=None,route_status='indoor',route_steps=[],return_route_steps=[],
                    round_trip_distance_m=None,one_way_distance_m=None,route_text='不复用旧地点路线；确认室内空间后执行。',
                    segments=[{'name':'轻松热身','minutes':warm},{'name':'室内慢走','minutes':item['minutes']-warm-cool},{'name':'放松','minutes':cool}])
        flags.append('activity_environment_fallback')
        if trip and not trip.get('location_confirmed'):flags.append('confirm_trip_location')
    current={k:copy.deepcopy(item.get(k)) for k in ('title','destination','candidate_id','route_status','round_trip_distance_m')}
    current['segments']=[(s['name'],s['minutes']) for s in item.get('segments',[])]
    return original!=current,flags


def reconcile(plan,events,event,profile,policy,now,evidence=None,role_results=None,preferred_slots=None):
    candidate=normalize(plan,profile,policy);changes=[];unresolved=[];requirements=[]
    zone=ZoneInfo(policy['timezone_name']);now=now.astimezone(zone);evidence=evidence or [];role_results=role_results or {}
    blocks=event_blocks(events,zone,policy['event_padding_minutes'])
    def change(domain,item,before,reason):
        changes.append({'domain':domain,'logical_id':item['logical_id'],'date':item['date'],'before':before['start_at'],'after':item['start_at'],
                        'before_end':before['end_at'],'after_end':item['end_at'],'minutes':round(minutes(dt(item['start_at']),dt(item['end_at']))),
                        'duration_delta_minutes':round(minutes(dt(item['start_at']),dt(item['end_at']))-minutes(dt(before['start_at']),dt(before['end_at']))),
                        'reason':reason})
    def restore(item,domain):
        original=(preferred_slots or {}).get(item.get('logical_id'))
        if not isinstance(original,dict):return False
        a,b=dt(original['start_at']),dt(original['end_at'])
        occupied=blocks+[(dt(x['start_at']),dt(x['end_at'])) for field in ('daily_schedule','meal_schedule','sleep_schedule')
                         for x in candidate.get(field,[]) if x is not item and x.get('start_at') and x.get('end_at')]
        if a<=now or any(overlap(a,b,c,d) for c,d in occupied):return False
        before=copy.deepcopy(item);status=item.get('execution_status');item.update(original)
        if status:item['execution_status']=status
        item.pop('event_id',None)
        change(domain,item,before,'事件已取消，原安排仍然可用，恢复原时间和已知量化依据。')
        return True
    # 先协调休息和餐食，再为活动分配剩余窗口。
    for item in candidate['sleep_schedule']:
        if item.get('execution_status') in ('completed','partial','skipped'):continue
        if restore(item,'sleep'):continue
        start,end=dt(item['start_at']),dt(item['end_at']);before=copy.deepcopy(item)
        if end<=now or not any(overlap(start,end,a,b) for a,b in blocks):continue
        duration=minutes(start,end);found=None
        for delta in sorted(range(-policy['max_sleep_shift_minutes'],policy['max_sleep_shift_minutes']+1,15),key=lambda d:(abs(d),d)):
            a,b=start+timedelta(minutes=delta),end+timedelta(minutes=delta)
            if a<=now or duration/60<max(policy['min_sleep_opportunity_hours'],profile.get('sleep_target_hours',8)):continue
            if not policy['allow_wake_shift'] and b>end:continue
            if any(overlap(a,b,c,d) for c,d in blocks):continue
            found=(a,b);break
        if not found:unresolved.append(f"{item['date']}没有满足休息目标与起床约束的窗口，请选择新的可用时段。");continue
        item.update(start_at=found[0].isoformat(),end_at=found[1].isoformat())
        change('sleep',item,before,'避开确定事件，保留原休息时长与目标。')
        if not policy['allow_sleep_shift']:requirements.append('sleep_shift')
        if found[1]>end and not policy['allow_wake_shift']:requirements.append('wake_shift')
    for item in candidate['meal_schedule']:
        if item.get('execution_status') in ('completed','partial','skipped'):continue
        if restore(item,'diet'):continue
        start,end=dt(item['start_at']),dt(item['end_at']);before=copy.deepcopy(item)
        if end<=now:continue
        dinners=[e for e in events if e['status']=='confirmed' and e['kind']=='dinner' and dt(e['start_at']).astimezone(zone).date().isoformat()==item['date']]
        dinner=next((e for e in dinners if ('breakfast' if dt(e['start_at']).astimezone(zone).hour<10 else 'lunch' if dt(e['start_at']).astimezone(zone).hour<16 else 'dinner')==item['meal']),None)
        if dinner:
            item.update(start_at=dinner['start_at'],end_at=dinner['end_at'],title='已确认饭局时间窗口',event_id=dinner['id'],
                        basis='外部饭局占用时段，不代表实际进餐持续时间。',nutrition=nutrition_total([]))
            if item['start_at']!=before['start_at'] or item['end_at']!=before['end_at']:change('diet',item,before,'按已确认饭局更新用餐窗口，营养仍待菜单和分量。')
            if not policy['allow_meal_shift']:requirements.append('meal_shift')
            continue
        if not any(overlap(start,end,a,b) for a,b in blocks):continue
        day=start.replace(hour=0,minute=0,second=0,microsecond=0)
        sleep_blocks=[(dt(s['start_at']),dt(s['end_at'])) for s in candidate['sleep_schedule']]
        other_meals=[(dt(m['start_at']),dt(m['end_at'])) for m in candidate['meal_schedule'] if m is not item]
        found=nearest_slot(start,minutes(start,end),blocks+sleep_blocks+other_meals,day.replace(hour=6),day.replace(hour=23),policy['max_meal_shift_minutes'],now)
        if not found:unresolved.append(f"{item['date']}的{item['meal']}缺少可用用餐窗口。");continue
        item.update(start_at=found.isoformat(),end_at=(found+(end-start)).isoformat())
        change('diet',item,before,'避开工作事件和休息窗口，保留用户设置的用餐时段长度。')
        if not policy['allow_meal_shift']:requirements.append('meal_shift')
    activities=[d for d in candidate.get('daily_schedule',[]) if d.get('kind')=='exercise']
    for item in activities:
        if item.get('execution_status') in ('completed','partial','skipped'):continue
        start,end=dt(item['start_at']),dt(item['end_at']);before=copy.deepcopy(item)
        if start<=now:continue
        trip=active_trip(events,item['date'])
        env=environment_for(evidence,'outing',item['date'])
        forecasts=((env or {}).get('data') or {}).get('weather',{}).get('data') or {}
        forecast=next((f for f in forecasts.get('forecast',[]) if f.get('time')==item['date']),None)
        assessment=assess_forecast(forecast)
        bad_weather=assessment['status']=='avoid_outdoor'
        if forecast:
            item.update(weather=forecast,weather_assessment=assessment,
                        weather_note='；'.join(assessment['reasons']) or '依据地区日预报，出发前再核实。')
        altered,flags=apply_environment(item,trip,evidence,role_results,policy,bad_weather) if trip or bad_weather else (False,[])
        requirements+=flags
        if altered:change('activity',item,before,'按当前位置/出差环境重建候选；未知路线不复用。')
        preferred=(preferred_slots or {}).get(item['logical_id'])
        if isinstance(preferred,dict):preferred=preferred['start_at']
        if preferred:
            proposed=dt(preferred);proposed_end=proposed+timedelta(minutes=item['minutes'])
            occupied=blocks+[(dt(s['start_at'])-timedelta(minutes=policy['bedtime_buffer_minutes']),dt(s['end_at'])) for s in candidate['sleep_schedule']]+[(dt(m['start_at']),dt(m['end_at'])) for m in candidate['meal_schedule']]+[(dt(d['start_at']),dt(d['end_at'])) for d in activities if d is not item]
            if proposed>now and proposed.date()==start.date() and not any(overlap(proposed,proposed_end,a,b) for a,b in occupied):
                item.update(start_at=proposed.isoformat(),end_at=proposed_end.isoformat());change('activity',item,before,'原事件已取消，恢复仍然可用的原活动时段。');continue
        if not any(overlap(start,end,a,b) for a,b in blocks):continue
        found=None;chosen_duration=item['minutes'];chosen_day=0
        for day_offset in range(policy['max_reschedule_days']+1):
            base=start+timedelta(days=day_offset);day=base.replace(hour=0,minute=0,second=0,microsecond=0)
            if day_offset and any(d is not item and dt(d['start_at']).date()==day.date() for d in activities):continue
            weekdays=candidate.get('planning_context',{}).get('available_weekdays',[])
            if weekdays and day.weekday() not in weekdays:continue
            h,m=map(int,policy['earliest_time'].split(':'));earliest=day.replace(hour=h,minute=m)
            h,m=map(int,policy['latest_time'].split(':'));latest=day.replace(hour=h,minute=m)
            # 跨出原七日时也必须先计算前夜起床和当晚就寝约束，再分配候选。
            context_windows=list(candidate['sleep_schedule'])
            for sleep_day in ((day-timedelta(days=1)).date().isoformat(),day.date().isoformat()):
                if not any(s['date']==sleep_day for s in context_windows):context_windows+=sleep_windows({**profile,'timezone_name':policy['timezone_name']},sleep_day,1)
            extra=[(dt(s['start_at'])-timedelta(minutes=policy['bedtime_buffer_minutes']),dt(s['end_at'])) for s in context_windows]
            extra += [(dt(m['start_at']),dt(m['end_at'])) for m in candidate['meal_schedule']]
            extra += [(dt(d['start_at']),dt(d['end_at'])) for d in activities if d is not item]
            durations=[item['minutes']]
            if policy['allow_activity_shortening']:durations += list(range(item['minutes']-5,policy['min_activity_minutes']-1,-5))
            for duration in durations:
                possible=nearest_slot(base,duration,blocks+extra,earliest,latest,policy['max_shift_minutes'],now)
                if possible:found=possible;chosen_duration=duration;chosen_day=day_offset;break
            if found:break
        if not found:unresolved.append(f"{item['date']}在已知约束内无可用活动窗口，未强制挤占休息或追加活动。");continue
        item.update(start_at=found.isoformat(),end_at=(found+timedelta(minutes=chosen_duration)).isoformat(),date=found.date().isoformat(),minutes=chosen_duration)
        if chosen_day:
            if not policy['allow_cross_day']:requirements.append('cross_day')
            candidate['daily_schedule']=[d for d in candidate['daily_schedule'] if d is item or not (d.get('kind')!='exercise' and d['date']==item['date'])]
            if not any(d['date']==before['date'] for d in candidate['daily_schedule']):candidate['daily_schedule'].append({'date':before['date'],'kind':'rest','title':'活动已改期','minutes':0})
            if not any(s['date']==item['date'] for s in candidate['sleep_schedule']):
                candidate['sleep_schedule']+=sleep_windows({**profile,'timezone_name':policy['timezone_name']},item['date'],1)
            moved_trip=active_trip(events,item['date'])
            if moved_trip or trip:
                _,flags=apply_environment(item,moved_trip,evidence,role_results,policy,not moved_trip)
                requirements+=flags
            item['weather_note']='活动日期已改变，按目标日期证据核实环境；缺失时不视为晴天。'
        if chosen_duration!=before['minutes']:
            # 缩短活动后清除不再适用的路线，按明确的室内备选重新组装。
            apply_environment(item,trip,[],role_results,policy,True);requirements.append('activity_shortening')
        change('activity',item,before,'避开事件、餐食与休息安排，在有限可用时段中选择接近原安排的窗口。')
    candidate['daily_schedule']=sorted(candidate.get('daily_schedule',[]),key=lambda i:i['date'])
    candidate['activity_days']=sum(d.get('kind')=='exercise' for d in candidate['daily_schedule'])
    candidate['variable_session_minutes']=len({d['minutes'] for d in candidate['daily_schedule'] if d.get('kind')=='exercise'})>1
    candidate=attach_quantification(candidate,{**profile,'timezone_name':policy['timezone_name']},[],now.date().isoformat())
    candidate['event_adjustments']=changes;candidate['schedule_conflicts']=unresolved
    return candidate,changes,list(dict.fromkeys(unresolved)),list(dict.fromkeys(requirements))


def validate_candidate(plan,events,profile,policy,changes,now):
    errors=[];changed={c['logical_id'] for c in changes};zone=ZoneInfo(policy['timezone_name'])
    blocks=event_blocks(events,zone,policy['event_padding_minutes'])
    activities=[i for i in plan.get('daily_schedule',[]) if i.get('kind')=='exercise']
    for item in activities:
        if item['logical_id'] not in changed:continue
        start,end=dt(item['start_at']),dt(item['end_at'])
        if start<=now:errors.append('不能提交已经过去的活动时间。')
        if abs(minutes(start,end)-item['minutes'])>0.01:errors.append('活动总分钟与起止时间不一致。')
        if item['minutes']>profile['minutes_per_session']:errors.append('活动分钟超过用户预算。')
        if sum(s['minutes'] for s in item.get('segments',[]))!=item['minutes']:errors.append('活动分段之和与总分钟不一致。')
        if any(overlap(start,end,a,b) for a,b in blocks):errors.append('活动仍与确定事件冲突。')
        for sleep in plan.get('sleep_schedule',[]):
            if overlap(start,end,dt(sleep['start_at'])-timedelta(minutes=policy['bedtime_buffer_minutes']),dt(sleep['end_at'])):errors.append('活动挤占休息窗口。')
        for meal in plan.get('meal_schedule',[]):
            if overlap(start,end,dt(meal['start_at']),dt(meal['end_at'])):errors.append('活动与用餐时段冲突。')
    for field in ('meal_schedule','sleep_schedule'):
        for item in plan.get(field,[]):
            if item['logical_id'] not in changed:continue
            start,end=dt(item['start_at']),dt(item['end_at'])
            relevant=[e for e in events if e.get('id')!=item.get('event_id')]
            if any(overlap(start,end,a,b) for a,b in event_blocks(relevant,zone,policy['event_padding_minutes'])):errors.append('用餐/休息窗口仍与其他事件冲突。')
            if field=='sleep_schedule' and minutes(start,end)/60<max(policy['min_sleep_opportunity_hours'],profile.get('sleep_target_hours',8)):errors.append('调整后的休息机会不足用户设置目标。')
    return list(dict.fromkeys(errors))
