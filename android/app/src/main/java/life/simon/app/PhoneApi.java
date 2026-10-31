package life.simon.app;

import org.json.JSONObject;
import org.json.JSONTokener;
import java.net.URI;
import java.net.URL;
import javax.net.ssl.HttpsURLConnection;
import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;

final class PhoneApi {
    static JSONObject call(BridgeStore store,String base,String path,String method,Object body) throws Exception {
        URI part=new URI(path);
        if(!path.startsWith("/api/") || path.startsWith("//") || part.isAbsolute() || path.contains("\\") || !part.normalize().getPath().startsWith("/api/") || path.contains(".."))throw new Exception("不允许的接口地址。");
        if(!java.util.Arrays.asList("GET","POST","PUT","DELETE").contains(method))throw new Exception("不允许的请求方法。");
        HttpsURLConnection c=(HttpsURLConnection)new URL(BridgeStore.origin(base)+path).openConnection();
        try {
            c.setRequestMethod(method);c.setInstanceFollowRedirects(false);c.setConnectTimeout(12000);c.setReadTimeout(180000);
            String token=store.token();if(!token.isEmpty())c.setRequestProperty("Authorization","Bearer "+token);
            if(body!=null && body!=JSONObject.NULL) { c.setDoOutput(true);c.setRequestProperty("Content-Type","application/json; charset=utf-8");try(java.io.OutputStream out=c.getOutputStream()){out.write(body.toString().getBytes(StandardCharsets.UTF_8));} }
            int status=c.getResponseCode();InputStream stream=status>=400?c.getErrorStream():c.getInputStream();
            ByteArrayOutputStream buffer=new ByteArrayOutputStream();
            if(stream!=null)try(InputStream in=stream){byte[] bytes=new byte[8192];int n;while((n=in.read(bytes))!=-1){if(buffer.size()+n>4_000_000)throw new Exception("服务返回内容过大。");buffer.write(bytes,0,n);}}
            Object payload;try{payload=new JSONTokener(buffer.toString("UTF-8")).nextValue();}catch(Exception e){payload=new JSONObject().put("detail","服务没有返回有效JSON。");}
            return new JSONObject().put("status",status).put("body",payload);
        }finally { c.disconnect(); }
    }
    static Object post(BridgeStore store,String path,JSONObject body) throws Exception {
        JSONObject result=call(store,store.base(),path,"POST",body);
        if(result.getInt("status")>=300)throw new Exception(result.optJSONObject("body")!=null?result.getJSONObject("body").optString("detail","手机同步未完成。"):"手机同步未完成。");
        return result.get("body");
    }
}
