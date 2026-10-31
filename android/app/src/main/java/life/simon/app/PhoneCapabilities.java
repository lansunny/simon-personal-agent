package life.simon.app;

import android.Manifest;
import android.app.NotificationManager;
import android.content.Context;
import android.content.ComponentName;
import android.content.pm.PackageManager;
import android.hardware.Sensor;
import android.hardware.SensorManager;
import android.os.Build;
import android.provider.Settings;
import org.json.JSONObject;

final class PhoneCapabilities {
    static boolean allowed(Context c,String p) {return c.checkSelfPermission(p)==PackageManager.PERMISSION_GRANTED;}
    static JSONObject read(Context c,BridgeStore store) throws Exception {
        String listeners=Settings.Secure.getString(c.getContentResolver(),"enabled_notification_listeners");
        String component=new ComponentName(c,EventNotificationListener.class).flattenToString();
        boolean listening=false;
        if(listeners!=null)for(String name:listeners.split(":"))if(component.equals(name))listening=true;
        SensorManager sensors=(SensorManager)c.getSystemService(Context.SENSOR_SERVICE);
        boolean steps=sensors.getDefaultSensor(Sensor.TYPE_STEP_COUNTER)!=null;
        boolean notification=((NotificationManager)c.getSystemService(Context.NOTIFICATION_SERVICE)).areNotificationsEnabled() && (Build.VERSION.SDK_INT<33 || allowed(c,Manifest.permission.POST_NOTIFICATIONS));
        return new JSONObject().put("native",true).put("paired",store.paired()).put("secure",true)
            .put("location",allowed(c,Manifest.permission.ACCESS_COARSE_LOCATION)||allowed(c,Manifest.permission.ACCESS_FINE_LOCATION)?"granted":"denied")
            .put("calendar",allowed(c,Manifest.permission.READ_CALENDAR)?"granted":"denied")
            .put("notifications",notification?"granted":"denied")
            .put("health",!steps?"unsupported":Build.VERSION.SDK_INT<29||allowed(c,Manifest.permission.ACTIVITY_RECOGNITION)?"granted":"denied")
            .put("event_messages",listening&&!store.prefs.getStringSet("event_apps",java.util.Collections.emptySet()).isEmpty()?"granted":"denied")
            .put("microphone","system_recognizer_on_click").put("steps_scope","本次采集会话计数，非全天步数");
    }
    static void report(Context c,BridgeStore store) throws Exception {
        if(!store.paired())return;JSONObject current=read(c,store),states=new JSONObject();
        for(String scope:new String[]{"location","calendar","notifications","health","event_messages"})if(store.hasScope(scope))states.put(scope,current.get(scope));
        PhoneApi.post(store,"/api/mobile/bridge/permissions",new JSONObject().put("states",states));
    }
    static void require(Context c,BridgeStore store,String scope) throws Exception {
        if(!store.paired() || !store.hasScope(scope))throw new Exception("请先在电脑上为设备配对并选择此数据范围。");
        if(!"granted".equals(read(c,store).optString(scope)))throw new Exception("请先在手机系统中允许此项权限。");
    }
}
