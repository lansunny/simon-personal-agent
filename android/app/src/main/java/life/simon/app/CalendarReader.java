package life.simon.app;

import android.content.Context;
import android.content.ContentUris;
import android.database.Cursor;
import android.net.Uri;
import android.provider.CalendarContract;
import java.time.Instant;
import java.time.ZoneOffset;
import java.time.format.DateTimeFormatter;

final class CalendarReader {
    private static final DateTimeFormatter UTC=DateTimeFormatter.ofPattern("yyyyMMdd'T'HHmmss'Z'").withZone(ZoneOffset.UTC);
    private static String escape(String value) {return value==null?"":value.replace("\\","\\\\").replace("\r","").replace("\n","\\n").replace(";","\\;").replace(",","\\,");}
    static String read(Context context,BridgeStore store) throws Exception {
        PhoneCapabilities.require(context,store,"calendar");
        long now=System.currentTimeMillis();Uri.Builder uri=CalendarContract.Instances.CONTENT_URI.buildUpon();
        ContentUris.appendId(uri,now-86400000L);ContentUris.appendId(uri,now+30L*86400000L);
        String[] cols={CalendarContract.Instances.EVENT_ID,CalendarContract.Instances.BEGIN,CalendarContract.Instances.END,CalendarContract.Events.TITLE,CalendarContract.Events.EVENT_LOCATION,CalendarContract.Events.ALL_DAY,CalendarContract.Events.STATUS,CalendarContract.Events.RRULE,CalendarContract.Events.ORIGINAL_INSTANCE_TIME,CalendarContract.Events.UID_2445};
        StringBuilder ics=new StringBuilder("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Simon//Android Calendar Bridge//ZH\r\n");
        try(Cursor c=context.getContentResolver().query(uri.build(),cols,null,null,CalendarContract.Instances.BEGIN+" ASC")) {
            if(c==null)throw new Exception("日历提供者没有返回内容。");int count=0;
            while(c.moveToNext()) {
                if(++count>1000)throw new Exception("未来30天日历实例超过1000条，请缩小同步范围。");
                String sourceUid=c.getString(9);if(sourceUid!=null&&(sourceUid.startsWith("hengban-")||sourceUid.startsWith("simon-")))continue;
                long start=c.getLong(1),end=c.getLong(2);if(end<=start)continue;
                String title=c.getString(3);if(title==null)title="日历事项";
                // 普通事件UID随改期保持稳定；重复事件使用原始实例时间。缺失实例不视作已取消。
                boolean repeated=!c.isNull(7)&&!c.getString(7).isEmpty();
                String suffix=!c.isNull(8)?"-"+c.getLong(8):repeated?"-"+start:"";
                String uid="android-"+store.prefs.getString("device","")+"-"+c.getLong(0)+suffix;
                ics.append("BEGIN:VEVENT\r\nUID:").append(uid).append("\r\nSEQUENCE:").append(now/1000).append("\r\nDTSTAMP:").append(UTC.format(Instant.ofEpochMilli(now))).append("\r\n");
                if(c.getInt(5)==1) {
                    DateTimeFormatter day=DateTimeFormatter.ofPattern("yyyyMMdd").withZone(ZoneOffset.UTC);
                    ics.append("DTSTART;VALUE=DATE:").append(day.format(Instant.ofEpochMilli(start))).append("\r\nDTEND;VALUE=DATE:").append(day.format(Instant.ofEpochMilli(end))).append("\r\n");
                }else ics.append("DTSTART:").append(UTC.format(Instant.ofEpochMilli(start))).append("\r\nDTEND:").append(UTC.format(Instant.ofEpochMilli(end))).append("\r\n");
                ics.append("SUMMARY:").append(escape(title)).append("\r\nLOCATION:").append(escape(c.getString(4))).append("\r\nSTATUS:").append(c.getInt(6)==CalendarContract.Events.STATUS_CANCELED?"CANCELLED":c.getInt(6)==CalendarContract.Events.STATUS_TENTATIVE?"TENTATIVE":"CONFIRMED").append("\r\nEND:VEVENT\r\n");
                if(ics.length()>850000)throw new Exception("日历内容过多，请缩小同步范围。");
            }
        }
        return ics.append("END:VCALENDAR\r\n").toString();
    }
}
