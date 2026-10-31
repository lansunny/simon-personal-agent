"""天气适配：统一单位/日期/来源，Open-Meteo失败时回退高德，不伪造缺项。"""
import math
from datetime import datetime,timezone
from urllib.parse import urlencode
from urllib.error import HTTPError
from zoneinfo import ZoneInfo

WMO_TEXT={0:'晴',1:'大部晴',2:'局部多云',3:'阴',45:'雾',48:'雾凇',51:'小毛毛雨',53:'毛毛雨',55:'强毛毛雨',
          56:'冻毛毛雨',57:'强冻毛毛雨',61:'小雨',63:'中雨',65:'大雨',66:'冻雨',67:'强冻雨',
          71:'小雪',73:'中雪',75:'大雪',77:'米雪',80:'小阵雨',81:'阵雨',82:'强阵雨',85:'小阵雪',86:'强阵雪',95:'雷暴',96:'雷暴伴冰雹',99:'强雷暴伴冰雹'}


def number(value):
    try:
        result=float(value)
        return result if math.isfinite(result) else None
    except (ValueError,TypeError):return None


def timestamp(value,zone):
    if not value:return None
    moment=datetime.fromisoformat(value)
    return (moment if moment.tzinfo else moment.replace(tzinfo=ZoneInfo(zone))).isoformat()


def envelope(provider,data,target_date,sources):
    rows=data['forecast'];data['available_dates']=[r['time'] for r in rows]
    if target_date:data['forecast']=[r for r in rows if r['time']==target_date]
    return {'status':'ok' if data['forecast'] else 'out_of_range','provider':provider,'data':data,'sources':sources,
            'retrieved_at':datetime.now(timezone.utc).isoformat(),
            'message':'天气按预报地区和日期提供；日预报不代表某小时实况，缺失概率/湿度等指标不补零。' if data['forecast'] else '服务没有覆盖目标日期，请临近再查；未将缺失日期当晴天。'}


def open_meteo(fetch,city,target_date,latitude,longitude):
    if latitude is not None and longitude is not None:
        place={'latitude':latitude,'longitude':longitude,'name':'已授权坐标所在地区'}
    else:
        geo=fetch('https://geocoding-api.open-meteo.com/v1/search?'+urlencode({'name':city,'count':3,'language':'zh','format':'json'}))
        places=geo.get('results',[])
        if len(places)!=1:return {'status':'ambiguous_location','provider':'open_meteo','message':'城市匹配不唯一，未自动猜选。'}
        place=places[0]
    raw=fetch('https://api.open-meteo.com/v1/forecast?'+urlencode({
        'latitude':place['latitude'],'longitude':place['longitude'],'timezone':'auto','forecast_days':7,
        'wind_speed_unit':'kmh','temperature_unit':'celsius',
        'current':'temperature_2m,apparent_temperature,relative_humidity_2m,precipitation,weather_code,wind_speed_10m',
        'daily':'temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code,wind_speed_10m_max,wind_gusts_10m_max,sunrise,sunset'}))
    daily=raw['daily'];zone=raw.get('timezone','UTC')
    rows=[{k:v[i] for k,v in daily.items()} for i in range(len(daily['time']))]
    for row in rows:row['weather_text']=WMO_TEXT.get(row.get('weather_code'),'天气类型待核实')
    current=raw.get('current') or {}
    current['weather_text']=WMO_TEXT.get(current.get('weather_code'),'天气类型待核实')
    if current.get('time'):current['time']=timestamp(current['time'],zone)
    data={'location':place['name'],'timezone':zone,'forecast':rows,'units':raw.get('daily_units',{}),
          'current':current,'current_units':raw.get('current_units',{}),
          'current_basis':'Open-Meteo网格当前天气估计，不是用户设备实测。','forecast_resolution':'daily'}
    return envelope('open_meteo',data,target_date,[{'title':'Open-Meteo天气预报','url':'https://open-meteo.com/'}])


def amap_weather(fetch,key,city,target_date,latitude,longitude):
    def get(path,params):
        raw=fetch('https://restapi.amap.com/v3/'+path+'?'+urlencode({'key':key,**params}))
        if raw.get('status')!='1':
            code=str(raw.get('infocode',''))
            raise ValueError(code if code.isdigit() else 'unknown')
        return raw
    if latitude is not None and longitude is not None:
        point=get('assistant/coordinate/convert',{'locations':f'{longitude},{latitude}','coordsys':'gps'}).get('locations')
        code=get('geocode/regeo',{'location':point,'extensions':'base'}).get('regeocode',{}).get('addressComponent',{}).get('adcode')
    elif city.isdigit() and len(city)==6:code=city
    else:
        places=get('config/district',{'keywords':city,'subdistrict':0,'extensions':'base'}).get('districts',[])
        if len(places)!=1:return {'status':'ambiguous_location','provider':'amap','message':'高德行政区匹配不唯一，需确认城市。'}
        code=places[0].get('adcode')
    if not code:return {'status':'missing_input','provider':'amap','message':'无法确定天气行政区。'}
    forecast=get('weather/weatherInfo',{'city':code,'extensions':'all'}).get('forecasts',[])
    if not forecast:return {'status':'empty','provider':'amap','message':'该地区暂无高德预报。'}
    forecast=forecast[0];rows=[]
    for cast in forecast.get('casts',[]):
        temperatures=[v for v in (number(cast.get('daytemp')),number(cast.get('nighttemp'))) if v is not None]
        rows.append({'time':cast['date'],'temperature_2m_max':max(temperatures) if temperatures else None,
                     'temperature_2m_min':min(temperatures) if temperatures else None,
                     'weather_text_day':cast.get('dayweather'),'weather_text_night':cast.get('nightweather'),
                     'wind_force_day':cast.get('daypower'),'wind_force_night':cast.get('nightpower'),
                     'wind_direction_day':cast.get('daywind'),'wind_direction_night':cast.get('nightwind'),
                     'precipitation_probability_max':None,'weather_code':None})
    # 实况接口失败不丢弃已得到的有效预报。
    live_error=False
    try:live=get('weather/weatherInfo',{'city':code,'extensions':'base'}).get('lives',[])
    except Exception:live=[];live_error=True
    live=live[0] if live else {}
    current={'time':timestamp(live.get('reporttime'),'Asia/Shanghai'),'temperature_2m':number(live.get('temperature')),
             'relative_humidity_2m':number(live.get('humidity')),'weather_text':live.get('weather'),
             'wind_force':live.get('windpower'),'wind_direction':live.get('winddirection')}
    data={'location':forecast.get('city'),'region_code':code,'province':forecast.get('province'),
          'timezone':'Asia/Shanghai','reported_at':timestamp(forecast.get('reporttime'),'Asia/Shanghai'),
          'forecast':rows,'forecast_resolution':'daily','current':current,'current_status':'unavailable' if live_error or not live else 'ok',
          'current_basis':'高德提供的地区实况，报告时间不等于本次查询时间。',
          'units':{'temperature_2m_max':'°C','temperature_2m_min':'°C','wind_force_day':'级','wind_force_night':'级'},
          'current_units':{'temperature_2m':'°C','relative_humidity_2m':'%','wind_force':'级'}}
    return envelope('amap',data,target_date,[{'title':'高德天气查询','url':'https://lbs.amap.com/api/webservice/guide/api/weatherinfo'}])


def fetch_weather(fetch,key,city='',target_date='',latitude=None,longitude=None):
    if (latitude is None)!=(longitude is None):return {'status':'missing_input','message':'需成对提供经纬度。'}
    if latitude is not None and not (-90<=latitude<=90 and -180<=longitude<=180):return {'status':'invalid_query','message':'坐标范围无效。'}
    if latitude is None and not city.strip():return {'status':'missing_input','message':'需要已授权坐标或明确城市。'}
    # 固定两个服务；不读取外部文本中的URL，不传用户档案/体重。
    providers=[('open_meteo',lambda:open_meteo(fetch,city,target_date,latitude,longitude))]
    if key and (latitude is None or 18<=latitude<=54 and 73<=longitude<=135):
        providers.append(('amap',lambda:amap_weather(fetch,key,city,target_date,latitude,longitude)))
    attempts=[];last=None
    for name,call in providers:
        try:
            response=call();attempts.append({'provider':name,'status':response['status']});last=response
            if response['status']=='ok':
                response['attempts']=attempts;response['fallback_used']=len(attempts)>1
                return response
        except HTTPError as error:attempts.append({'provider':name,'status':'unavailable','http_status':error.code})
        except ValueError as error:
            code=str(error)
            attempts.append({'provider':name,'status':'provider_error',**({'code':code} if code.isdigit() else {})})
        except Exception:attempts.append({'provider':name,'status':'unavailable'})
    if last is not None:return {**last,'attempts':attempts}
    return {'status':'unavailable','attempts':attempts,'data':{'forecast':[],'current':{}},'sources':[],
            'retrieved_at':datetime.now(timezone.utc).isoformat(),
            'message':'天气服务本次均未返回有效结果；不会把查询失败当晴天，可先选室内备选并稍后重查。'}
