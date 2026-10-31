"""一般生活规划的保守天气分流规则；不是个体医学安全判定。"""
import re


def assess_forecast(forecast):
    if not forecast:return {'status':'unknown','reasons':['缺少目标日期有效预报，先用室内备选，临近再核实。'],'requires_recheck':True}
    reasons=[];cautions=[]
    code=forecast.get('weather_code')
    text=' '.join(str(forecast.get(k) or '') for k in ('weather_text','weather_text_day','weather_text_night'))
    if code in (51,53,55,56,57,61,63,65,66,67,71,73,75,77,80,81,82,85,86,95,96,99) or re.search(r'雨|雪|雷|冰雹|冻',text):
        reasons.append('日预报有降水、冰冻或雷暴，改用室内备选。')
    if code in (45,48) or re.search(r'雾|霾|沙尘|浮尘|扬沙',text):reasons.append('日预报存在能见度或沙尘等不利条件，优先室内。')
    probability=forecast.get('precipitation_probability_max')
    if probability is not None and probability>=60:reasons.append(f'日降水概率最高{probability:g}%，优先室内备选。')
    elif probability is not None and probability>=30:cautions.append(f'日降水概率最高{probability:g}%，出发前复查并保留室内备选。')
    high,low=forecast.get('temperature_2m_max'),forecast.get('temperature_2m_min')
    if high is not None and high>=35:reasons.append(f'日最高温{high:g}°C，不安排本方案的户外跑步。')
    elif high is not None and high>=30:cautions.append(f'日最高温{high:g}°C，优先避开高温时段并降低负担；具体时段需临近核实。')
    if low is not None and low<=0:reasons.append(f'日最低温{low:g}°C，考虑低温/结冰，优先室内。')
    wind,gust=forecast.get('wind_speed_10m_max'),forecast.get('wind_gusts_10m_max')
    forces=[int(n) for k in ('wind_force_day','wind_force_night') for n in re.findall(r'\d+',str(forecast.get(k) or ''))]
    if (wind is not None and wind>=39) or (gust is not None and gust>=50) or any(v>=6 for v in forces):reasons.append('日预报风力较强，优先室内并避开临水/高处路线。')
    known=code is not None or bool(text.replace('天气类型待核实','').strip()) or high is not None or low is not None or probability is not None
    if not known:return {'status':'unknown','reasons':['目标日期天气字段缺失，先保留室内备选。'],'requires_recheck':True}
    return {'status':'avoid_outdoor' if reasons else 'caution' if cautions else 'no_alert',
            'reasons':reasons+cautions,'requires_recheck':True,
            'note':'依据日预报和保守行程规则，不代表某小时实况或个体运动安全保证。'}


def annotate_weather(evidence):
    if evidence.get('status')=='ok':
        for row in (evidence.get('data') or {}).get('forecast',[]):row['activity_assessment']=assess_forecast(row)
    return evidence
