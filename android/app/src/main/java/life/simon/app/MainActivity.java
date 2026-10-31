package life.simon.app;

import android.Manifest;
import android.app.*;
import android.content.*;
import android.content.pm.*;
import android.hardware.*;
import android.location.*;
import android.net.Uri;
import android.os.*;
import android.provider.Settings;
import android.speech.RecognizerIntent;
import android.view.*;
import android.webkit.*;
import android.widget.*;
import org.json.*;
import java.time.Instant;
import java.util.*;
import java.util.concurrent.*;

/** 无第三方定位SDK：只有显式点击才采集一次。系统权限不等于后端授权。 */
public class MainActivity extends Activity {
    private WebView web;
    private BridgeStore store;
    private String trusted="";
    private final Handler ui=new Handler(Looper.getMainLooper());
    private final ExecutorService io=Executors.newFixedThreadPool(4);
    private Runnable permissionContinuation;
    private String permissionId,speechId;
    private boolean foreground=false,polling=false;
    private final Runnable poll=new Runnable(){public void run(){if(!foreground)return;pollNotifications();ui.postDelayed(this,30000);}};
    interface Work {Object run() throws Exception;}

    @Override public void onCreate(Bundle saved) {
        super.onCreate(saved);store=new BridgeStore(this);
        web=new WebView(this);setContentView(web);
        WebSettings options=web.getSettings();options.setJavaScriptEnabled(true);options.setDomStorageEnabled(true);
        options.setAllowFileAccess(false);options.setAllowContentAccess(false);options.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);options.setJavaScriptCanOpenWindowsAutomatically(false);options.setSupportMultipleWindows(false);options.setSafeBrowsingEnabled(true);
        CookieManager.getInstance().setAcceptThirdPartyCookies(web,false);
        web.addJavascriptInterface(new Bridge(),"SimonAndroid");
        web.setWebViewClient(new WebViewClient(){
            @Override public boolean shouldOverrideUrlLoading(WebView view,WebResourceRequest request) {
                if(sameOrigin(request.getUrl().toString()))return false;
                Toast.makeText(MainActivity.this,"为保护设备凭据，容器仅打开你配置的Simon服务。",Toast.LENGTH_LONG).show();return true;
            }
            @Override public WebResourceResponse shouldInterceptRequest(WebView v,WebResourceRequest r) {
                if(sameOrigin(r.getUrl().toString()))return null;
                return new WebResourceResponse("text/plain","UTF-8",new java.io.ByteArrayInputStream(new byte[0]));
            }
            @Override public void onReceivedSslError(WebView v,android.webkit.SslErrorHandler h,android.net.http.SslError e) {h.cancel();runOnUiThread(()->Toast.makeText(MainActivity.this,"HTTPS证书不受信，请配置有效证书或在debug手机安装开发CA。",Toast.LENGTH_LONG).show());}
        });
        web.setWebChromeClient(new WebChromeClient(){
            @Override public void onPermissionRequest(PermissionRequest r) {r.deny();}
        });
        trusted=store.base();if(trusted.isEmpty())configureServer();else load();
    }
    private boolean sameOrigin(String url) {
        try{Uri value=Uri.parse(url),base=Uri.parse(trusted);return "https".equals(value.getScheme())&&Objects.equals(value.getHost(),base.getHost())&&(value.getPort()==-1?443:value.getPort())==(base.getPort()==-1?443:base.getPort())&&value.getUserInfo()==null;}catch(Exception e){return false;}
    }
    private void load(){web.loadUrl(trusted+"/static/mobile/vivid/live.html");}
    private void configureServer() {
        EditText field=new EditText(this);field.setSingleLine(true);field.setHint("https://电脑域名:8000");field.setText(trusted);
        new AlertDialog.Builder(this).setTitle("连接你的Simon服务").setMessage("手机与电脑需可互访。请输入受信HTTPS根地址；127.0.0.1是手机自身。换服务会清除此机配对。").setView(field).setNegativeButton("取消",null).setPositiveButton("连接",(d,w)->{
            try{String next=BridgeStore.origin(field.getText().toString());if(!next.equals(trusted))store.reset();trusted=next;store.prefs.edit().putString("base",next).apply();load();}catch(Exception e){Toast.makeText(this,e.getMessage(),Toast.LENGTH_LONG).show();configureServer();}
        }).show();
    }
    @Override public boolean onCreateOptionsMenu(Menu menu){menu.add("服务地址").setOnMenuItemClickListener(i->{configureServer();return true;});return true;}
    @Override public void onBackPressed(){if(web.canGoBack())web.goBack();else super.onBackPressed();}
    @Override protected void onResume(){super.onResume();foreground=true;web.onResume();ui.removeCallbacks(poll);ui.post(poll);if(store.paired())io.execute(()->{try{PhoneCapabilities.report(this,store);}catch(Exception ignored){}});}
    @Override protected void onPause(){foreground=false;ui.removeCallbacks(poll);web.onPause();super.onPause();}
    @Override protected void onDestroy(){ui.removeCallbacksAndMessages(null);io.shutdownNow();web.removeJavascriptInterface("SimonAndroid");web.destroy();super.onDestroy();}

    private void result(String id,boolean ok,Object value) {
        ui.post(()->{if(isFinishing()||isDestroyed()||!sameOrigin(web.getUrl()))return;String payload=value instanceof JSONObject||value instanceof JSONArray?value.toString():JSONObject.quote(String.valueOf(value));web.evaluateJavascript("window.SimonBridge&&window.SimonBridge.onResult("+JSONObject.quote(id)+","+ok+","+payload+")",null);});
    }
    private void fail(String id,String message){try{result(id,false,new JSONObject().put("message",message));}catch(Exception ignored){}}
    private void background(String id,Work work){io.execute(()->{try{result(id,true,work.run());}catch(Exception e){fail(id,e instanceof java.io.IOException?"网络连接失败或证书不受信，请检查服务地址。":e.getMessage()==null?"设备操作未完成。":e.getMessage());}});}
    public final class Bridge {
        @JavascriptInterface public void request(String id,String method,String json) {
            if(id==null||id.length()>100||json==null||json.length()>1_100_000)return;
            ui.post(()->{if(!sameOrigin(web.getUrl()))return;try{dispatch(id,method,new JSONObject(json));}catch(Exception e){fail(id,e.getMessage());}});
        }
    }
    private void dispatch(String id,String method,JSONObject args) throws Exception {
        switch(method) {
            case "capabilities":result(id,true,PhoneCapabilities.read(this,store));break;
            case "configure":background(id,()->{store.configure(args,trusted);PhoneCapabilities.report(this,store);return new JSONObject().put("paired",true);});break;
            case "api":background(id,()->PhoneApi.call(store,trusted,args.getString("path"),args.optString("method","GET"),args.opt("body")));break;
            case "permission":permission(id,args.getString("scope"),()->background(id,()->{PhoneCapabilities.report(this,store);return new JSONObject().put("message","已读取系统授权结果，可在系统设置中随时撤回。").put("capabilities",PhoneCapabilities.read(this,store));}));break;
            case "location":permission(id,"location",()->locate(id));break;
            case "calendar":background(id,()->{PhoneCapabilities.report(this,store);return PhoneApi.post(store,"/api/mobile/bridge/calendar",new JSONObject().put("content",CalendarReader.read(this,store)));});break;
            case "steps":readSteps(id);break;
            case "speech":speech(id);break;
            default:throw new Exception("设备方法不在白名单。");
        }
    }
    private void permission(String id,String scope,Runnable next) throws Exception {
        if(!store.paired()||!store.hasScope(scope))throw new Exception("请先在电脑上配对并选择此项数据范围。");
        if("event_messages".equals(scope)){chooseEventApps(id);return;}
        if("granted".equals(PhoneCapabilities.read(this,store).optString(scope))){next.run();return;}
        String[] permissions;
        switch(scope){
            case "location":permissions=new String[]{Manifest.permission.ACCESS_FINE_LOCATION,Manifest.permission.ACCESS_COARSE_LOCATION};break;
            case "calendar":permissions=new String[]{Manifest.permission.READ_CALENDAR};break;
            case "notifications":if(Build.VERSION.SDK_INT<33){startActivity(new Intent(Settings.ACTION_APP_NOTIFICATION_SETTINGS).putExtra(Settings.EXTRA_APP_PACKAGE,getPackageName()));result(id,true,new JSONObject().put("message","请在系统通知设置中允许Simon提醒。"));return;}permissions=new String[]{Manifest.permission.POST_NOTIFICATIONS};break;
            case "health":if(Build.VERSION.SDK_INT<29)throw new Exception("手机未提供可用计步传感器。");permissions=new String[]{Manifest.permission.ACTIVITY_RECOGNITION};break;
            default:throw new Exception("权限名称不在白名单。");
        }
        if(permissionContinuation!=null)throw new Exception("请先完成当前系统授权。");permissionId=id;permissionContinuation=next;requestPermissions(permissions,501);
    }
    @Override public void onRequestPermissionsResult(int code,String[] permissions,int[] results) {
        super.onRequestPermissionsResult(code,permissions,results);if(code!=501)return;Runnable next=permissionContinuation;String id=permissionId;permissionContinuation=null;permissionId=null;
        boolean allowed=false;for(int state:results)if(state==PackageManager.PERMISSION_GRANTED)allowed=true;
        if(allowed&&next!=null)next.run();else {if(id!=null)fail(id,"权限未获授权，可在系统设置中重新允许。");io.execute(()->{try{PhoneCapabilities.report(this,store);}catch(Exception ignored){}});}
    }
    private void locate(String id) {
        try {
            PhoneCapabilities.require(this,store,"location");LocationManager manager=(LocationManager)getSystemService(LOCATION_SERVICE);
            String provider=manager.isProviderEnabled(LocationManager.GPS_PROVIDER)?LocationManager.GPS_PROVIDER:LocationManager.NETWORK_PROVIDER;
            if(!PhoneCapabilities.allowed(this,Manifest.permission.ACCESS_FINE_LOCATION))provider=LocationManager.NETWORK_PROVIDER;
            if(!manager.isProviderEnabled(provider))throw new Exception("请开启手机定位服务。仅授权粗略定位时可能无法获得精确路线。");
            java.util.concurrent.atomic.AtomicBoolean completed=new java.util.concurrent.atomic.AtomicBoolean();
            LocationListener listener=new LocationListener(){
                public void onLocationChanged(Location p){if(completed.getAndSet(true))return;manager.removeUpdates(this);background(id,()->{PhoneCapabilities.report(MainActivity.this,store);JSONObject value=new JSONObject().put("latitude",p.getLatitude()).put("longitude",p.getLongitude()).put("accuracy_m",p.hasAccuracy()?p.getAccuracy():100000).put("confirmed_at",Instant.ofEpochMilli(p.getTime()).toString()).put("coordinate_system","wgs84");return PhoneApi.post(store,"/api/mobile/bridge/location",value);});}
                public void onStatusChanged(String p,int s,Bundle b){} public void onProviderEnabled(String p){} public void onProviderDisabled(String p){}
            };
            manager.requestSingleUpdate(provider,listener,Looper.getMainLooper());ui.postDelayed(()->{if(completed.getAndSet(true))return;manager.removeUpdates(listener);fail(id,"定位等待已结束；如果尚未返回位置，请在开阔处重试。");},25000);
        }catch(Exception e){fail(id,e.getMessage());}
    }
    private void readSteps(String id) throws Exception {
        PhoneCapabilities.require(this,store,"health");SensorManager sensors=(SensorManager)getSystemService(SENSOR_SERVICE);Sensor sensor=sensors.getDefaultSensor(Sensor.TYPE_STEP_COUNTER);
        if(sensor==null)throw new Exception("这台手机没有计步传感器。");
        java.util.concurrent.atomic.AtomicBoolean sampled=new java.util.concurrent.atomic.AtomicBoolean();
        SensorEventListener listener=new SensorEventListener(){
            public void onAccuracyChanged(Sensor s,int a){}
            public void onSensorChanged(SensorEvent event){if(sampled.getAndSet(true))return;sensors.unregisterListener(this);long count=(long)event.values[0],elapsed=SystemClock.elapsedRealtime();String observed=Instant.now().toString();
                long baseline=store.prefs.getLong("step_baseline",-1),last=store.prefs.getLong("step_elapsed",0);String started=store.prefs.getString("step_started",observed);
                if(baseline<0||elapsed<last||count<baseline){baseline=count;started=observed;}
                store.prefs.edit().putLong("step_baseline",baseline).putLong("step_elapsed",elapsed).putString("step_started",started).apply();long delta=count-baseline;String start=started;
                background(id,()->{PhoneCapabilities.report(MainActivity.this,store);return PhoneApi.post(store,"/api/mobile/bridge/steps",new JSONObject().put("request_id",UUID.randomUUID().toString()).put("observed_at",observed).put("cumulative_since_boot",count).put("session_steps",delta).put("session_started_at",start));});
            }
        };
        if(!sensors.registerListener(listener,sensor,SensorManager.SENSOR_DELAY_NORMAL))throw new Exception("计步传感器不可用。");
        ui.postDelayed(()->{if(sampled.getAndSet(true))return;sensors.unregisterListener(listener);fail(id,"计步采样等待结束；如果没有返回，可走动几步后重试。");},20000);
    }
    private void speech(String id) {
        if(speechId!=null){fail(id,"请先完成当前语音输入。");return;}
        new AlertDialog.Builder(this).setTitle("使用手机系统语音输入").setMessage("由你选择的系统识别服务处理音频，服务可能联网。本应用只取得文字，不录音存档，也不自动发送。是否继续？").setNegativeButton("取消",(d,w)->fail(id,"已取消语音输入。")).setPositiveButton("开始",(d,w)->{
            try{Intent intent=new Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH).putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL,RecognizerIntent.LANGUAGE_MODEL_FREE_FORM).putExtra(RecognizerIntent.EXTRA_LANGUAGE,"zh-CN").putExtra(RecognizerIntent.EXTRA_PREFER_OFFLINE,true);speechId=id;startActivityForResult(intent,502);}catch(Exception e){speechId=null;fail(id,"没有可用的系统语音识别服务。");}
        }).show();
    }
    @Override protected void onActivityResult(int request,int code,Intent data) {
        super.onActivityResult(request,code,data);if(request!=502||speechId==null)return;String id=speechId;speechId=null;
        try{ArrayList<String> texts=data==null?null:data.getStringArrayListExtra(RecognizerIntent.EXTRA_RESULTS);if(code!=RESULT_OK||texts==null||texts.isEmpty())throw new Exception("语音输入未返回文字。");result(id,true,new JSONObject().put("text",texts.get(0)));}catch(Exception e){fail(id,e.getMessage());}
    }
    private void chooseEventApps(String id) {
        Intent launcher=new Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_LAUNCHER);
        List<ResolveInfo> list=getPackageManager().queryIntentActivities(launcher,0);Map<String,String> applications=new TreeMap<>();
        for(ResolveInfo app:list){String pkg=app.activityInfo.packageName;if(!getPackageName().equals(pkg))applications.put(pkg,app.loadLabel(getPackageManager())+" ("+pkg+")");}
        String[] packages=applications.keySet().toArray(new String[0]),labels=applications.values().toArray(new String[0]);Set<String> selected=new HashSet<>(store.prefs.getStringSet("event_apps",Collections.emptySet()));boolean[] checked=new boolean[packages.length];for(int i=0;i<packages.length;i++)checked[i]=selected.contains(packages[i]);
        new AlertDialog.Builder(this).setTitle("选择允许读取日程通知的应用").setMultiChoiceItems(labels,checked,(d,index,value)->{if(value)selected.add(packages[index]);else selected.remove(packages[index]);})
            .setNegativeButton("取消",(d,w)->fail(id,"未更改通知连接。"))
            .setPositiveButton("保存并打开系统授权",(d,w)->{store.prefs.edit().putStringSet("event_apps",selected).apply();startActivity(new Intent(Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS));try{result(id,true,new JSONObject().put("message","请在系统中允许Simon通知访问。仅处理所选应用未来送达的日程通知，验证码会丢弃；返回后读取真实授权结果。"));}catch(Exception ignored){}}).show();
    }
    private void pollNotifications() {
        if(polling||!store.paired()||!store.hasScope("notifications"))return;polling=true;
        io.execute(()->{try{
            PhoneCapabilities.require(this,store,"notifications");PhoneCapabilities.report(this,store);
            JSONArray items=(JSONArray)PhoneApi.post(store,"/api/mobile/bridge/notifications/claim",new JSONObject());
            NotificationManager notifications=(NotificationManager)getSystemService(NOTIFICATION_SERVICE);notifications.createNotificationChannel(new NotificationChannel("simon-routine","Simon日常提醒",NotificationManager.IMPORTANCE_DEFAULT));
            for(int i=0;i<items.length();i++){
                JSONObject item=items.getJSONObject(i),body=item.getJSONObject("body");String key=item.getString("id"),status="accepted";
                try{PhoneCapabilities.require(this,store,"notifications");if(body.has("expires_at")&&Instant.parse(body.getString("expires_at")).isBefore(Instant.now()))throw new Exception("提醒已过期");
                    PendingIntent open=PendingIntent.getActivity(this,0,new Intent(this,MainActivity.class),PendingIntent.FLAG_IMMUTABLE|PendingIntent.FLAG_UPDATE_CURRENT);
                    notifications.notify(key,0,new Notification.Builder(this,"simon-routine").setSmallIcon(android.R.drawable.ic_dialog_info).setContentTitle(body.optString("title","Simon提醒")).setContentText(body.optString("message","你有一项日常安排。")).setContentIntent(open).setAutoCancel(true).build());
                }catch(Exception e){status="failed";}
                PhoneApi.post(store,"/api/mobile/bridge/notifications/"+key+"/receipt",new JSONObject().put("claim_token",item.getString("claim_token")).put("status",status).put("provider_id",key));
            }
        }catch(Exception ignored){}finally{polling=false;}});
    }
}
