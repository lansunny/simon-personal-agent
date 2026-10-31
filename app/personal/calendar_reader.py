"""有界展开真实日历：时区、重复实例、例外、全天和明确取消。"""
from datetime import datetime,date,timedelta,time,timezone
from zoneinfo import ZoneInfo
from icalendar import Calendar
import recurring_ical_events
from app.personal.contracts import LifeEvent


def read_calendar(content,timezone_name='Asia/Shanghai',now=None):
    now=now or datetime.now(timezone.utc);zone=ZoneInfo(timezone_name)
    try:calendar=Calendar.from_ical(content)
    except Exception:raise ValueError('ICS日历无法解析。') from None
    warnings=[];complete=True;cancelled_series={}
    for component in list(calendar.walk('VEVENT')):
        uid=str(component.get('UID',''))
        if uid.startswith('hengban-'):
            calendar.subcomponents.remove(component) if component in calendar.subcomponents else None
            continue
        if not component.get('DTSTART') and str(component.get('STATUS','')).upper()=='CANCELLED' and not component.get('RECURRENCE-ID'):
            cancelled_series[uid]=int(component.get('SEQUENCE',0))
            if component in calendar.subcomponents:calendar.subcomponents.remove(component)
            continue
        rules=component.get('RRULE')
        if rules:
            freq=str(rules.get('FREQ',[''])[0])
            if freq not in ('DAILY','WEEKLY','MONTHLY','YEARLY'):
                if component in calendar.subcomponents:calendar.subcomponents.remove(component)
                warnings.append('过高频率或无法识别的重复规则未展开；该日历不是完整可用性证据。');complete=False
        if str(component.get('STATUS','')).upper()=='CANCELLED' and not component.get('RECURRENCE-ID'):
            cancelled_series[uid]=int(component.get('SEQUENCE',0))
    default_timezone=str(calendar.get('X-WR-TIMEZONE') or timezone_name)
    try:zone=ZoneInfo(default_timezone)
    except Exception:warnings.append('日历默认时区未识别，使用用户配置时区。')
    try:instances=recurring_ical_events.of(calendar).between(now-timedelta(days=7),now+timedelta(days=90))
    except Exception:raise ValueError('日历重复规则无法展开，未据此假定空闲。') from None
    events=[]
    if len(instances)>2000:warnings.append('事件超过单批2000项；需要缩小日历范围。');complete=False
    for item in instances[:2000]:
        uid=str(item.get('UID',''))
        if not uid or uid.startswith('hengban-'):continue
        try:
            start=item.decoded('DTSTART');end=item.decoded('DTEND',None)
            if end is None:
                duration=item.decoded('DURATION',None)
                if duration is not None:end=start+duration
                elif isinstance(start,date) and not isinstance(start,datetime):end=start+timedelta(days=1)
                else:raise ValueError('缺少结束时间。')
            all_day=not isinstance(start,datetime)
            def aware(value):
                if not isinstance(value,datetime):return datetime.combine(value,time.min,zone)
                return value.replace(tzinfo=zone) if value.tzinfo is None else value
            start,end=aware(start),aware(end)
            recurrence=item.decoded('RECURRENCE-ID',None)
            instance_uid=uid+'::'+aware(recurrence).astimezone(timezone.utc).isoformat() if recurrence is not None else uid
            title=str(item.get('SUMMARY','日历事件'))
            kind='trip' if any(w in title for w in ('出差','差旅')) else 'dinner' if any(w in title for w in ('聚餐','饭局','晚餐','午餐')) else 'meeting'
            status=str(item.get('STATUS','CONFIRMED')).upper()
            event=LifeEvent(source='calendar',external_id=instance_uid,title=title,kind=kind,
                status='cancelled' if status=='CANCELLED' or uid in cancelled_series else 'tentative' if status=='TENTATIVE' else 'confirmed',
                start_at=start,end_at=end,location=str(item.get('LOCATION','')),timezone_name=timezone_name,all_day=all_day,
                source_sequence=int(item.get('SEQUENCE',0)),occupies_time=False if kind=='trip' else True)
            events.append(event.model_dump(mode='json'))
        except Exception:warnings.append('某个实例缺少有效UID/起止时间，未将其视为空闲。');complete=False
    return {'events':events,'warnings':list(dict.fromkeys(warnings)),'complete':complete,'cancelled_series':cancelled_series,
            'window_start':(now-timedelta(days=7)).isoformat(),'window_end':(now+timedelta(days=90)).isoformat()}
