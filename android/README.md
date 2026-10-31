# Simon Android

Java WebView容器，将网页端Simon与安卓设备能力连接。支持配对、主动单次定位、日历读取、筛选通知事件、计步采样和系统语音输入。模型Key始终留在后端。

## 构建

需要Android 8或以上。用Android Studio打开本目录，准备SDK 35、JDK 17；项目使用Gradle Wrapper 8.9、AGP 8.7.3。

```powershell
# 在android目录中运行
.\gradlew.bat assembleDebug
```

调试APK位于`app/build/outputs/apk/debug/app-debug.apk`。仓库仅提供源码，不包含本机签名密钥、证书或预构建APK。

## 连接自己的后端

1. 按[快速开始](../docs/quickstart.md)安装Python依赖，准备本机`.env`。
2. 停止普通后端，在项目根目录运行`python run_phone.py --ip <电脑局域网IPv4>`。需要OpenSSL，默认HTTPS端口8443。
3. 在手机上自行安装生成的`data/mobile-tls/Simon-dev-CA.cer`，仅用于本地开发。调试包支持用户安装的CA，release包只信任系统CA。CA私钥和服务器私钥保留在电脑，不发送到手机。
4. 运行`python phone_pair.py --scopes app location`生成一次性配对码。日历、通知、计步等按需增加`calendar`、`event_messages`、`notifications`或`health`范围；配对后仍需逐项完成OS授权。
5. 在App填写电脑的HTTPS地址和配对码，选择允许的能力。手机上的`127.0.0.1`指手机自身。

脚本不自动修改防火墙或系统信任。一次只启动一个操作相同业务数据库的后端。

## 桥接与权限

JS入口为`SimonAndroid.request(id, method, json)`，结果回调`SimonBridge.onResult(id, ok, payload)`。支持`configure / api / capabilities / permission / location / calendar / steps / speech`。

后端设备scope、手机OS权限和能力报告分别处理。定位仅主动单次采集；通知仅处理用户选择的应用与日程关键词，过滤验证码，不读取全量短信或微信历史。原始步数不直接换算全天消耗。

## 当前范围

APK曾完成编译与签名检查；真机授权、vivo后台行为尚未验证。提醒当前需App前台接收，尚无后台推送、精确闹钟或Health Connect睡眠/体重同步。
