package life.simon.app;

import android.app.Notification;
import android.service.notification.NotificationListenerService;
import android.service.notification.StatusBarNotification;
import java.time.Instant;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.Collections;
import java.util.concurrent.*;
import java.util.regex.Pattern;
import org.json.JSONObject;

/** Android通知访问，无READ_SMS；仅明确选择的应用和日程关键词。 */
public class EventNotificationListener extends NotificationListenerService {
    private final ExecutorService io=new ThreadPoolExecutor(1,1,0,TimeUnit.SECONDS,new ArrayBlockingQueue<>(20),new ThreadPoolExecutor.DiscardPolicy());
    private static final Pattern EVENT=Pattern.compile("会议|饭局|聚餐|出差|加班|改期|取消");
    private static final Pattern SECRET=Pattern.compile("验证码|校验码|动态密码|\\bOTP\\b|one.time.password",Pattern.CASE_INSENSITIVE);
    @Override public void onNotificationPosted(StatusBarNotification item) {
        BridgeStore store=new BridgeStore(this);
        if(!store.paired()||!store.hasScope("event_messages")||!store.prefs.getStringSet("event_apps",Collections.emptySet()).contains(item.getPackageName()))return;
        Notification notification=item.getNotification();if(notification==null||notification.extras==null||(notification.flags & Notification.FLAG_GROUP_SUMMARY)!=0)return;
        CharSequence title=notification.extras.getCharSequence(Notification.EXTRA_TITLE,""),text=notification.extras.getCharSequence(Notification.EXTRA_BIG_TEXT,notification.extras.getCharSequence(Notification.EXTRA_TEXT,""));
        String content=(title+"\n"+text).trim();
        if(SECRET.matcher(content).find()||!EVENT.matcher(content).find())return;if(content.length()>4000)content=content.substring(0,4000);final String value=content;
        io.execute(()->{try{
            PhoneCapabilities.require(this,store,"event_messages");PhoneCapabilities.report(this,store);
            byte[] hash=MessageDigest.getInstance("SHA-256").digest(item.getKey().getBytes(StandardCharsets.UTF_8));StringBuilder key=new StringBuilder();for(byte b:hash)key.append(String.format("%02x",b));
            // 同一通知更新保持来源ID；接收时间只是解析相对日期基准，不替代消息中缺失的起止时间。
            PhoneApi.post(store,"/api/mobile/bridge/event-message",new JSONObject().put("text",value).put("reference_at",Instant.ofEpochMilli(item.getPostTime()).toString()).put("external_id","notification:"+key));
        }catch(Exception ignored){}});
    }
    @Override public void onDestroy(){io.shutdownNow();super.onDestroy();}
}
