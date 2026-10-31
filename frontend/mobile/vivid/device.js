/* Simon桥接v1：所有权限只由明确的用户操作触发，不在加载时申请。 */
window.SimonDevice=(()=>{
  const waiting=new Map();let counter=0;
  const native=()=>!!window.SimonAndroid?.request;
  function request(method,args={}){return new Promise((resolve,reject)=>{
    if(!native())return reject(new Error('当前不在Simon安卓容器中。'));
    const id='simon-'+Date.now()+'-'+(++counter);
    const timer=setTimeout(()=>{waiting.delete(id);reject(new Error('设备操作等待超时，请检查手机授权。'));},method==='api'?210000:60000);
    waiting.set(id,{resolve,reject,timer});window.SimonAndroid.request(id,method,JSON.stringify(args));
  });}
  window.SimonBridge={onResult(id,ok,payload){const pending=waiting.get(id);if(!pending)return;waiting.delete(id);clearTimeout(pending.timer);ok?pending.resolve(payload):pending.reject(new Error(payload?.message||'设备请求未完成。'));}};
  async function location(){
    if(native())return request('location');
    if(!window.isSecureContext)throw new Error('手机定位需要HTTPS页面；电脑本机可使用localhost。');
    if(!navigator.geolocation)throw new Error('当前浏览器没有定位接口。');
    return new Promise((resolve,reject)=>navigator.geolocation.getCurrentPosition(p=>resolve({latitude:p.coords.latitude,longitude:p.coords.longitude,accuracy_m:p.coords.accuracy,coordinate_system:'wgs84',confirmed_at:new Date(p.timestamp).toISOString()}),e=>reject(new Error(({1:'定位未获授权。可以手动填写城市和起点，或在系统设置里允许定位。',2:'暂时无法取得位置，请检查手机定位服务。',3:'定位超时，请在开阔位置重试。'})[e.code]||'定位失败。')),{enableHighAccuracy:true,timeout:20000,maximumAge:60000}));
  }
  async function status(){
    if(native()){try{return await request('capabilities')}catch(e){return {native:true,error:e.message}}}
    let geolocation='prompt';try{geolocation=(await navigator.permissions.query({name:'geolocation'})).state}catch{}
    return {native:false,secure:window.isSecureContext,location:geolocation,notifications:'Notification' in window?Notification.permission:'unsupported',calendar:'file_import',event_messages:'native_required',health:'native_required',microphone:('SpeechRecognition' in window||'webkitSpeechRecognition' in window)?'available_on_click':'unsupported'};
  }
  return {native,request,location,status};
})();
