"""标准 iCalendar 导出。用户导入日历后由其设备处理 VALARM。"""
from datetime import datetime, timezone


def escape(value):
    return str(value).replace('\\', '\\\\').replace('\n', '\\n').replace('\r', '').replace(',', '\\,').replace(';', '\\;')


def calendar_text(plan):
    lines = ['BEGIN:VCALENDAR', 'VERSION:2.0', 'PRODID:-//Hengban//Workout Plan//ZH', 'CALSCALE:GREGORIAN']
    for index, item in enumerate(plan['body'].get('daily_schedule', [])):
        if item.get('kind') != 'exercise':
            continue
        start = datetime.fromisoformat(item['start_at']).astimezone(timezone.utc)
        end = datetime.fromisoformat(item['end_at']).astimezone(timezone.utc)
        energy = item.get('energy') or {}
        quantity = f"计划{item['minutes']}分钟。"
        if energy.get('status') == 'estimated':
            low,high=energy['gross_kcal']
            quantity += f"其中{energy['covered_minutes']}分钟情景估算总量约{low}–{high} kcal，另{energy['excluded_minutes']}分钟未计入。非实测。"
        else:
            quantity += '缺少适用的体重或强度数据，未估算能量。'
        lines += ['BEGIN:VEVENT', f'UID:hengban-{item["date"]}-exercise@local',
                  'SEQUENCE:' + str(plan['version']),
                  'DTSTAMP:' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'),
                  'DTSTART:' + start.strftime('%Y%m%dT%H%M%SZ'), 'DTEND:' + end.strftime('%Y%m%dT%H%M%SZ'),
                  'SUMMARY:' + escape(item['title']), 'LOCATION:' + escape(item.get('destination', '')),
                  'DESCRIPTION:' + escape(quantity + '\n' + item.get('route_text', '') + '\n' + item.get('alternative', '')),
                  'BEGIN:VALARM', 'TRIGGER:-PT5M', 'ACTION:DISPLAY', 'DESCRIPTION:运动安排即将开始', 'END:VALARM', 'END:VEVENT']
    lines.append('END:VCALENDAR')
    # 按 UTF-8 字节折行，兼容含中文内容的日历客户端。
    folded = []
    for line in lines:
        chunk = ''
        for char in line:
            if len((chunk + char).encode('utf-8')) > 73:
                folded.append(chunk)
                chunk = ' '
            chunk += char
        folded.append(chunk)
    return '\r\n'.join(folded) + '\r\n'
