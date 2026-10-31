"""有来源、可复算的量化；模型不负责生成能量数字。"""
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import re

MET_SOURCE = 'https://pacompendium.com/'
SLEEP_SOURCE = 'https://www.cdc.gov/sleep/about/index.html'


def sanitize_model_energy(value):
    """角色自由文本不承担能量计算，防止把模型自报数字当成量化结果。"""
    if isinstance(value, str):
        return re.sub(r'\d+(?:\.\d+)?(?:\s*[~～–—-]\s*\d+(?:\.\d+)?)?\s*(?:kcal|千卡|大卡|卡路里)',
                      '〔能量值见程序量化卡〕', value, flags=re.I)
    if isinstance(value, list):
        return [sanitize_model_energy(item) for item in value]
    if isinstance(value, dict):
        return {key:sanitize_model_energy(item) for key,item in value.items()}
    return value
# 保留原始条目数值；区间表示不同活动情景，绝不是统计置信区间。
ACTIVITIES = {
    'walk': [(2.8, '17152', '平地慢走，约3.2–3.9 km/h'),
             (3.8, '17190', '平地中速步行，约4.5–5.5 km/h')],
    'indoor': [(2.3, '17151', '很慢的步行，低于3.2 km/h'),
               (2.8, '17152', '慢走，约3.2–3.9 km/h')],
    'jog': [(6.0, '12010', '走跑交替，其中慢跑少于10分钟'),
            (7.5, '12020', '自选配速慢跑')],
}


def weight_basis(profile, records, as_of):
    candidates = [(r['record_date'], r['weight_kg'], '自报体重记录') for r in records
                  if r.get('weight_kg') and r['record_date'] <= as_of]
    if profile.get('baseline_weight_kg') and profile.get('baseline_date', '') <= as_of:
        candidates.append((profile['baseline_date'], profile['baseline_weight_kg'], '档案自报体重'))
    if not candidates:
        return None
    day, kg, source = max(candidates, key=lambda row: row[0])
    return {'kg': kg, 'date': day, 'source': source,
            'age_days': (date.fromisoformat(as_of) - date.fromisoformat(day)).days}


def energy_estimate(mode, minutes, basis, age=None):
    if not basis or not minutes:
        return {'status': 'missing', 'note': '需要有效体重和活动时长；未记录不等于零消耗。'}
    if age is None or not 19 <= age <= 59:
        return {'status': 'unsupported', 'note': '当前MET条目用于19–59岁成人；未套用其他年龄人群。'}
    scenarios = ACTIVITIES.get(mode)
    if not scenarios:
        return {'status': 'missing', 'note': '活动类型或强度不明确，暂不估算。'}
    kg = basis['kg']
    gross = [m * kg * minutes / 60 for m, _, _ in scenarios]
    net = [max(0, m - 1) * kg * minutes / 60 for m, _, _ in scenarios]
    return {'status': 'estimated', 'unit': 'kcal', 'minutes': minutes, 'weight': basis,
            'gross_kcal': [round(min(gross)), round(max(gross))],
            'net_kcal': [round(min(net)), round(max(net))],
            'formula': '总量≈MET×kg×分钟/60；相对静息额外量≈(MET−1)×kg×分钟/60',
            'scenarios': [{'met': m, 'code': code, 'assumption': text} for m, code, text in scenarios],
            'source': MET_SOURCE, 'source_version': '2024 Adult Compendium',
            'note': '不同强度情景的粗估范围，不是实测或误差上下界；受速度、地形、个体差异影响。'
                    + ('体重记录较旧，请更新。' if basis['age_days'] > 30 else '')}


def nutrition_total(items):
    if not items:
        return {'status': 'missing', 'note': '尚无食物分量和可靠营养密度，不能仅按店名或菜名计算热量。', 'items': []}
    rows = []
    for item in items:
        row = dict(item)
        row['kcal'] = round(item['grams'] * item['kcal_per_100g'] / 100, 1)
        for key in ('protein_g', 'fat_g', 'carbs_g'):
            row[key] = round(item['grams'] * item[key + '_per_100g'] / 100, 1) if item.get(key + '_per_100g') is not None else None
        rows.append(row)
    return {'status': 'calculated_from_input', 'items': rows,
            'kcal': round(sum(row['kcal'] for row in rows)),
            'grams': round(sum(row['grams'] for row in rows), 1),
            **{key: round(sum(row[key] for row in rows), 1) if all(row[key] is not None for row in rows) else None
               for key in ('protein_g', 'fat_g', 'carbs_g')},
            'formula': '分量(g)×标签每100g营养值/100',
            'note': '根据用户提供的分量和营养标签计算，标签/称量未由系统核验；仅覆盖这些条目，不代表全天摄入。'}


def sleep_windows(profile, start_date, days=7):
    zone=ZoneInfo(profile.get("timezone_name","Asia/Shanghai"))
    bedtime = profile.get('sleep_bedtime', '23:00')
    wake = profile.get('sleep_wake_time', '07:00')
    target = profile.get('sleep_target_hours', 8)
    result = []
    for n in range(days):
        day = date.fromisoformat(start_date) + timedelta(days=n)
        bed = datetime.fromisoformat(f'{day}T{bedtime}').replace(tzinfo=zone)
        end = datetime.fromisoformat(f'{day}T{wake}').replace(tzinfo=zone)
        if end <= bed:
            end += timedelta(days=1)
        hours = (end.astimezone(timezone.utc) - bed.astimezone(timezone.utc)).total_seconds() / 3600
        age = profile.get('age', 25)
        reference = '18–60岁成人通常至少7小时' if age <= 60 else '61–64岁通常7–9小时' if age <= 64 else '65岁及以上通常7–8小时'
        result.append({'date': day.isoformat(), 'bedtime': bedtime, 'wake_time': wake,
                       'start_at':bed.isoformat(),'end_at':end.isoformat(),'logical_id':f'sleep:{day.isoformat()}',
                       'wake_date': end.date().isoformat(), 'crosses_midnight': end.date() != day,
                       'opportunity_hours': round(hours, 2), 'target_hours': target,
                       'target_gap_hours': round(max(0, target - hours), 2),
                       'reference': reference, 'source': SLEEP_SOURCE,
                       'note': '这是用户设置的卧床休息窗口，不等于实际睡眠时长或睡眠质量。'})
    return result


def attach_quantification(plan, profile, records, as_of):
    basis = weight_basis(profile, records, as_of)
    total_low = total_high = estimated_minutes = unknown_minutes = 0
    for day in plan.get('daily_schedule', []):
        if day.get('kind') != 'exercise':
            continue
        low = high = net_low = net_high = measured = 0
        segments = day.get('segments', [])
        for segment in segments:
            name = segment['name']
            mode = 'walk' if name.startswith(('从', '按地图')) or name == '轻松步行' else 'indoor' if name == '室内慢走' else 'jog' if name == '轻松走跑交替' else None
            estimate = energy_estimate(mode, segment['minutes'], basis, profile.get('age'))
            segment['energy'] = estimate
            if estimate['status'] == 'estimated':
                low += estimate['gross_kcal'][0]; high += estimate['gross_kcal'][1]
                net_low += estimate['net_kcal'][0]; net_high += estimate['net_kcal'][1]
                measured += segment['minutes']
        excluded = day['minutes'] - measured
        day['energy'] = {'status': 'estimated' if measured else 'missing', 'gross_kcal': [low, high] if measured else None,
                         'net_kcal': [net_low, net_high] if measured else None, 'covered_minutes': measured, 'excluded_minutes': excluded,
                         'weight': basis, 'source': MET_SOURCE,
                         'note': f'仅估算其中{measured}分钟；其余{excluded}分钟动作/强度不明确，未计入。范围是情景估算，不是实测。'}
        total_low += low; total_high += high; estimated_minutes += measured; unknown_minutes += excluded
    plan['quantification'] = {'planned_minutes': sum(d.get('minutes', 0) for d in plan.get('daily_schedule', [])),
                             'estimated_minutes': estimated_minutes, 'unestimated_minutes': unknown_minutes,
                             'gross_kcal': [total_low, total_high] if estimated_minutes else None,
                             'weight': basis, 'unit': 'kcal',
                             'note': '仅覆盖本计划列出的可估算活动，不是全天总消耗、热量缺口或减重预测。',
                             'nutrition': nutrition_total([])}
    quantified=[d['energy'] for d in plan.get('daily_schedule',[]) if d.get('kind')=='exercise' and d.get('energy',{}).get('status')=='estimated']
    plan['quantification']['net_kcal']=[sum(e['net_kcal'][n] for e in quantified) for n in (0,1)] if quantified else None
    plan['quantification'].update(calculation_version='health-quantities-2',basis_type='planned',
                                  interval_type='activity_scenarios_not_confidence_interval')
    known=[m for m in plan.get('meal_schedule',[]) if m.get('nutrition',{}).get('status')=='calculated_from_input']
    if known:
        plan['quantification']['nutrition']=nutrition_total([i for m in known for i in m['nutrition']['items']])
        plan['quantification']['nutrition'].update(planned_meals=len(known),unknown_meals=len(plan['meal_schedule'])-len(known),
                                                 note='仅覆盖已选模板的计划分量；未选餐食与外食营养未知，不代表实际或全天摄入。')
    first = next((d['date'] for d in plan.get('daily_schedule', [])), as_of)
    if not plan.get('sleep_schedule'):
        plan['sleep_schedule'] = sleep_windows(profile, first)
    for window in plan['sleep_schedule']:
        if window.get('start_at') and window.get('end_at'):
            start=datetime.fromisoformat(window['start_at']);end=datetime.fromisoformat(window['end_at'])
            window['opportunity_hours']=round((end.astimezone(timezone.utc)-start.astimezone(timezone.utc)).total_seconds()/3600,2)
            window['target_gap_hours']=round(max(0,window.get('target_hours',8)-window['opportunity_hours']),2)
            window.update(bedtime=start.strftime('%H:%M'),wake_time=end.strftime('%H:%M'),wake_date=end.date().isoformat(),crosses_midnight=end.date()!=start.date())
    days=sorted({i['date'] for field in ('daily_schedule','meal_schedule','sleep_schedule') for i in plan.get(field,[])})
    daily=[]
    for day in days:
        activities=[i for i in plan.get('daily_schedule',[]) if i.get('kind')=='exercise' and i['date']==day]
        meals=[i for i in plan.get('meal_schedule',[]) if i['date']==day]
        nutrition=nutrition_total([i for m in meals if m.get('nutrition',{}).get('status')=='calculated_from_input' for i in m['nutrition']['items']])
        known_meals=sum(m.get('nutrition',{}).get('status')=='calculated_from_input' for m in meals)
        nutrition.update(planned_meals=len(meals),quantified_meals=known_meals,unknown_meals=len(meals)-known_meals)
        covered=[a['energy'] for a in activities if a['energy']['status']=='estimated']
        daily.append({'date':day,'planned_activity_minutes':sum(a['minutes'] for a in activities),
                      'estimated_minutes':sum(a['energy']['covered_minutes'] for a in activities),
                      'unestimated_minutes':sum(a['energy']['excluded_minutes'] for a in activities),
                      'gross_kcal':[sum(e['gross_kcal'][n] for e in covered) for n in (0,1)] if covered else None,
                      'net_kcal':[sum(e['net_kcal'][n] for e in covered) for n in (0,1)] if covered else None,
                      'planned_nutrition':nutrition,'sleep_opportunity_hours':[s['opportunity_hours'] for s in plan['sleep_schedule'] if s['date']==day],
                      'note':'计划分量和活动情景估算；缺项不计为零，休息机会不是实际睡眠。'})
    plan['quantification']['daily']=daily
    advice=[]
    for item in plan.get('daily_schedule',[]):
        if item.get('kind')!='exercise':continue
        energy=item['energy'];interval=energy.get('gross_kcal')
        text=f"计划活动{item['minutes']}分钟；可估算{energy['covered_minutes']}分钟，未估算{energy['excluded_minutes']}分钟。"
        if interval:text+=f"所覆盖部分的总能量情景估算约{interval[0]}–{interval[1]} kcal。"
        advice.append({'domain':'activity','date':item['date'],'logical_id':item.get('logical_id'),'text':text,'basis_type':'planned_estimate','source':MET_SOURCE})
    for item in plan.get('meal_schedule',[]):
        nutrition=item.get('nutrition',{});text=f"{item['start_at'][11:16]}–{item['end_at'][11:16]}用餐窗口。"
        if nutrition.get('status')=='calculated_from_input':text+=f"计划分量{nutrition['grams']}g，按所填标签计算约{nutrition['kcal']} kcal；实际摄入需另行反馈。"
        else:text+='缺少菜单、分量或营养密度，待补充后计算，当前不提供热量数值。'
        advice.append({'domain':'diet','date':item['date'],'logical_id':item.get('logical_id'),'text':text,'basis_type':'planned_from_input' if nutrition.get('status')=='calculated_from_input' else 'missing','assumed':item.get('assumed',False)})
    for item in plan.get('sleep_schedule',[]):
        text=f"计划预留{item['opportunity_hours']}小时卧床机会，个人目标{item['target_hours']}小时，窗口差额{item['target_gap_hours']}小时；实际睡眠另记。"
        advice.append({'domain':'sleep','date':item['date'],'logical_id':item.get('logical_id'),'text':text,'basis_type':'planned_opportunity','source':SLEEP_SOURCE})
    plan['quantified_advice']=advice
    return plan


def quantity_changes(before,after,changes):
    """改期说明也提供前后量；不同情景区间不相减生成伪精确收益。"""
    fields={'activity':'daily_schedule','diet':'meal_schedule','sleep':'sleep_schedule'}
    def view(item,domain):
        if not item:return None
        if domain=='activity':return {k:item.get(k) for k in ('minutes','energy','round_trip_distance_m')}
        if domain=='diet':return {'nutrition':item.get('nutrition'),'servings':item.get('servings'),'basis':item.get('basis')}
        return {k:item.get(k) for k in ('opportunity_hours','target_hours','target_gap_hours')}
    for change in changes:
        domain=change['domain'];field=fields[domain];key=change['logical_id']
        old=next((i for i in before.get(field,[]) if i.get('logical_id')==key),None)
        new=next((i for i in after.get(field,[]) if i.get('logical_id')==key),None)
        change['quantities_before']=view(old,domain);change['quantities_after']=view(new,domain)
        change['quantity_note']='量化值为计划值；单纯挪动时间不宣称额外消耗或减重收益。'
    return changes
