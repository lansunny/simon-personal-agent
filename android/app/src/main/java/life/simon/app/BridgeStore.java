package life.simon.app;

import android.content.Context;
import android.content.SharedPreferences;
import android.security.keystore.KeyGenParameterSpec;
import android.security.keystore.KeyProperties;
import android.util.Base64;
import org.json.JSONArray;
import org.json.JSONObject;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.security.KeyStore;
import javax.crypto.Cipher;
import javax.crypto.KeyGenerator;
import javax.crypto.SecretKey;
import javax.crypto.spec.GCMParameterSpec;

/** 凭据只保存在Android Keystore加密后的私有Preferences，关闭备份。 */
final class BridgeStore {
    final SharedPreferences prefs;
    BridgeStore(Context context) { prefs=context.getSharedPreferences("simon_device",Context.MODE_PRIVATE); }
    static String origin(String input) throws Exception {
        URI uri=new URI(input.trim());
        if (!"https".equalsIgnoreCase(uri.getScheme()) || uri.getHost()==null || uri.getUserInfo()!=null || uri.getQuery()!=null || uri.getFragment()!=null || !(uri.getPath().isEmpty() || "/".equals(uri.getPath())))
            throw new Exception("请输入受信HTTPS服务根地址，例如 https://电脑域名:8000");
        if(uri.getPort()==0||uri.getPort()>65535)throw new Exception("服务端口无效。");
        String host=uri.getHost().toLowerCase(java.util.Locale.ROOT);
        if(host.contains(":" )&&!host.startsWith("["))host="["+host+"]";
        return "https://"+host+(uri.getPort()==-1||uri.getPort()==443?"":":"+uri.getPort());
    }
    String base() { return prefs.getString("base",""); }
    boolean hasScope(String scope) {
        try { JSONArray a=new JSONArray(prefs.getString("scopes","[]"));for(int i=0;i<a.length();i++)if(scope.equals(a.getString(i)))return true; }catch(Exception ignored){}
        return false;
    }
    boolean paired() { return prefs.contains("secret"); }
    private SecretKey key() throws Exception {
        KeyStore ks=KeyStore.getInstance("AndroidKeyStore");ks.load(null);
        if (!ks.containsAlias("simon_device_v1")) {
            KeyGenerator gen=KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES,"AndroidKeyStore");
            gen.init(new KeyGenParameterSpec.Builder("simon_device_v1",KeyProperties.PURPOSE_ENCRYPT|KeyProperties.PURPOSE_DECRYPT).setBlockModes(KeyProperties.BLOCK_MODE_GCM).setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE).build());gen.generateKey();
        }
        return ((KeyStore.SecretKeyEntry)ks.getEntry("simon_device_v1",null)).getSecretKey();
    }
    synchronized void configure(JSONObject value,String trustedOrigin) throws Exception {
        if(!origin(value.getString("base_url")).equals(trustedOrigin))throw new Exception("配对服务来源不一致。");
        String token=value.getString("token");if(token.length()<30)throw new Exception("设备凭据格式无效。");
        Cipher cipher=Cipher.getInstance("AES/GCM/NoPadding");cipher.init(Cipher.ENCRYPT_MODE,key());
        String encrypted=Base64.encodeToString(cipher.doFinal(token.getBytes(StandardCharsets.UTF_8)),Base64.NO_WRAP);
        prefs.edit().putString("base",trustedOrigin).putString("secret",encrypted).putString("iv",Base64.encodeToString(cipher.getIV(),Base64.NO_WRAP)).putString("device",value.getString("device_id")).putString("scopes",value.getJSONArray("scopes").toString()).apply();
    }
    synchronized String token() throws Exception {
        if(!paired())return "";
        Cipher cipher=Cipher.getInstance("AES/GCM/NoPadding");cipher.init(Cipher.DECRYPT_MODE,key(),new GCMParameterSpec(128,Base64.decode(prefs.getString("iv",""),Base64.NO_WRAP)));
        return new String(cipher.doFinal(Base64.decode(prefs.getString("secret",""),Base64.NO_WRAP)),StandardCharsets.UTF_8);
    }
    void reset() { prefs.edit().clear().apply(); }
}
