"""授权消息提取与日历兼容入口；缺失信息保留为缺项。"""
import re
import hashlib
from datetime import datetime,timedelta
from app.personal.contracts import LifeEvent,EventExtraction


def parse_text(text,reference_at,external_id='',source='manual',runner=None,known_event=None,known_events=None):
    if reference_at.tzinfo is None:raise ValueError('参考时间需包含时区。')
    if runner and runner.mode=='live':
        result=runner.run('event_parser',{'source_text':text,'reference_at':reference_at.isoformat(),
                    'known_event':known_event,'known_events':[{k:e.get(k) for k in ('id','title','kind','start_at','end_at','status')} for e in (known_events or [])[:12]]},EventExtraction)
        related=result.get('related_event_id')
        if related and related not in {e['id'] for e in (known_events or [])+([known_event] if known_event else [])}:raise ValueError('引用了不存在的事件。')
    else:
        from app.multi_agent.routing import number
        normalized=re.sub(r'([零一二两三四五六七八九十]+)点',lambda m:str(number(m[1]))+'点' if number(m[1]) is not None else m[0],text)
        dates=re.findall(r'\d{4}-\d{2}-\d{2}',normalized)
        relative=any(w in text for w in ('今天','今晚','明天','明晚','后天','周','星期'))
        day=datetime.fromisoformat(dates[0]).date() if dates else (reference_at+timedelta(days=2 if '后天' in text else 1 if any(w in text for w in ('明天','明晚')) else 0)).date()
        if known_event and not dates and not relative:day=datetime.fromisoformat(known_event['start_at']).astimezone(reference_at.tzinfo).date()
        weekday=re.search(r'(下周|本周|周|星期)([一二三四五六日天])',text)
        if weekday and not dates:
            target='一二三四五六日'.index(weekday[2].replace('天','日'));delta=target-reference_at.weekday()
            if weekday[1]=='下周':delta+=7
            elif delta<0:delta+=7
            day=reference_at.date()+timedelta(days=delta)
        normalized=re.sub(r'\d{4}-\d{2}-\d{2}',' ',normalized)
        clocks=list(re.finditer(r'(?<!\d)([01]?\d|2[0-3])(?:[:：]([0-5]\d)|点(?:(半)|([0-5]?\d)分?)?)',normalized))
        times=[]
        for n,match in enumerate(clocks[:2]):
            hour=int(match[1]);minute=int(match[2] or ('30' if match[3] else match[4] or '0'))
            prefix=normalized[:match.start()]
            parts=list(re.finditer(r'上午|下午|晚上|早上|今晚|明晚|凌晨|中午',prefix))
            part=parts[-1][0] if parts else ''
            if part in ('下午','晚上','今晚','明晚') and hour<12:hour+=12
            current_day=datetime.fromisoformat(dates[n]).date() if n<len(dates) else day
            if n and any(w in normalized[clocks[0].end():match.start()] for w in ('次日','第二天','明天')):current_day+=timedelta(days=1)
            times.append(datetime.combine(current_day,datetime.min.time(),reference_at.tzinfo).replace(hour=hour,minute=minute))
        basis='explicit'
        if len(times)==1 and known_event:
            times.append(times[0]+(datetime.fromisoformat(known_event['end_at'])-datetime.fromisoformat(known_event['start_at'])))
            basis='沿用关联事件已知时长，未声称获得新的结束时间'
        kind='trip' if '出差' in text else 'dinner' if any(w in text for w in ('聚餐','饭局','吃饭')) else known_event['kind'] if known_event else 'meeting'
        all_day=False
        if not times and kind=='trip':
            duration=re.search(r'出差\s*([0-9一二两三四五六七八九十]+)天',text)
            if duration and number(duration[1]):
                start=datetime.combine(day,datetime.min.time(),reference_at.tzinfo)
                times=[start,start+timedelta(days=number(duration[1]))];all_day=True
        if len(times)<2:return {'needs_information':True,'message':'请补充明确起止时间；已关联的改期可沿用原时长，出差可说明明确天数。'}
        if times[1]<=times[0]:return {'needs_information':True,'message':'结束不晚于开始，请明确是否跨天。'}
        result={'title':known_event['title'] if known_event else text[:100],'kind':kind,
                'status':'cancelled' if any(w in text for w in ('取消','不聚了','不开了')) else 'tentative' if any(w in text for w in ('有空','可能','？','?','待定','邀请','能否','能不能')) else 'confirmed',
                'start_at':times[0].isoformat(),'end_at':times[1].isoformat(),'location':'','missing_fields':[],
                'all_day':all_day,'duration_basis':basis}
    if result.get('missing_fields') or not result.get('start_at') or not result.get('end_at'):
        return {'needs_information':True,'message':'事件仍缺少：'+'、'.join(result.get('missing_fields') or ['明确起止时间'])}
    body={k:known_event[k] for k in ('city','start_place','location','location_confirmed','all_day','occupies_time','timezone_name') if known_event and k in known_event}
    body.update({k:result[k] for k in ('title','kind','status','start_at','end_at','location','city','start_place','all_day','duration_basis') if k in result and (result[k] or not known_event)})
    body.update(source=source,external_id=external_id or hashlib.sha256((source+str(reference_at.date())+text).encode()).hexdigest()[:32],source_text=text)
    if known_event:body['related_event_id']=known_event['id']
    elif result.get('related_event_id'):body['related_event_id']=result['related_event_id']
    return LifeEvent.model_validate(body).model_dump(mode='json')


def parse_calendar(content):
    from app.personal.calendar_reader import read_calendar
    parsed=read_calendar(content)
    return parsed['events'],parsed['warnings']
