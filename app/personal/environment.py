"""参数化实时环境工具：可信位置、时效、缓存、额度与只读MCP适配。"""
import hashlib
import threading
from datetime import date,datetime,timedelta,timezone
from pydantic import Field
from app.schemas import InputModel
from app.errors import BudgetExceeded
from app.multi_agent.tools import ToolBinding
from app.integrations.mcp_client import call_tools
from app.personal.continuity import dump


class EnvironmentQuery(InputModel):
    purpose: str = Field(pattern='^(weather|outing|restaurants)$')
    target_date: str = Field(default='',max_length=10)
    radius_m: int = Field(default=2000,ge=300,le=5000)
    categories: list[str] = Field(default_factory=list,max_length=3)


class LocationInput(InputModel):
    city: str = Field(default='',max_length=100)
    start_place: str = Field(default='',max_length=200)
    latitude: float | None = Field(default=None,ge=-90,le=90)
    longitude: float | None = Field(default=None,ge=-180,le=180)
    confirmed_at: datetime | None = None
    accuracy_m: float | None = Field(default=None,ge=0,le=100000)
    coordinate_system: str = Field(default='wgs84',pattern='^wgs84$')


class EnvironmentAccess:
    def __init__(self,continuity,runtime,policy,events,location=None,caller=call_tools,max_calls=4):
        self.continuity,self.runtime,self.policy=continuity,runtime,policy
        self.events,self.location,self.caller=events,location,caller
        self.calls=0;self.max_calls=max_calls;self.lock=threading.Lock();self.evidence=[]

    def resolve(self,target):
        now=datetime.now(timezone.utc)
        trips=[e for e in self.events if e['kind']=='trip' and e['status']=='confirmed' and e['start_at'][:10]<=target<=e['end_at'][:10]]
        if trips:
            e=trips[0]
            if e.get('location_confirmed') and e.get('city') and e.get('start_place'):
                return {'city':e['city'],'start_place':e['start_place'],'source':'confirmed_trip','event_id':e['id'],'revision':e['revision']}
            return None
        value=self.location
        if not value or value.get('status','active')!='active':return None
        updated=datetime.fromisoformat(value['confirmed_at'])
        if updated.tzinfo is None or updated>now+timedelta(minutes=5) or now-updated>timedelta(minutes=self.policy['location_ttl_minutes']):return None
        return {k:v for k,v in value.items() if k in ('city','start_place','latitude','longitude','confirmed_at','accuracy_m')}

    def location_context(self,target_date=''):
        """角色共用的位置事实；无需把坐标当成用户对话文本，也不额外联网。"""
        from zoneinfo import ZoneInfo
        target=target_date or datetime.now(ZoneInfo(self.policy['timezone_name'])).date().isoformat()
        resolved=self.resolve(target)
        if resolved:
            coordinates=resolved.get('latitude') is not None and resolved.get('longitude') is not None
            accuracy=resolved.get('accuracy_m')
            return {'status':'available','has_coordinates':coordinates,
                    'city':resolved.get('city',''),'start_place':resolved.get('start_place',''),
                    'accuracy_m':accuracy,'confirmed_at':resolved.get('confirmed_at'),
                    'can_search_nearby':not coordinates or accuracy is None or accuracy<=1000,
                    'environment_queries_allowed':bool(self.policy.get('allow_environment_queries')),
                    'note':'已取得用户授权位置；有坐标即可查附近场地、餐馆和天气，不要求另填城市。位置精度是起点的不确定范围，不是门牌级精确定位。'}
        raw=self.location or {}
        travelling=any(e['kind']=='trip' and e['status']=='confirmed' and e['start_at'][:10]<=target<=e['end_at'][:10] for e in self.events)
        status='trip_unconfirmed' if travelling else 'revoked' if raw.get('status')=='revoked' else 'expired' if raw.get('confirmed_at') else 'missing'
        return {'status':status,'has_coordinates':False,'can_search_nearby':False,
                'environment_queries_allowed':bool(self.policy.get('allow_environment_queries')),
                'note':{'trip_unconfirmed':'当天有出差，请确认目的城市和当地起点。','revoked':'用户已撤回位置。','expired':'之前保存的位置已过期或确认时间无效，请重新点击定位。','missing':'尚未保存已授权位置，可点击定位或填写城市和起点。'}[status]}

    def query(self,purpose,target_date='',radius_m=2000,categories=None):
        with self.lock:
            if self.calls>=self.max_calls:return {'status':'limit_reached','message':'本次环境查询额度已用完。'}
            self.calls+=1
        if not self.policy.get('allow_environment_queries'):return {'status':'disabled','message':'未获准查询外部环境；不会把查询失败当晴天或可用场地。'}
        from zoneinfo import ZoneInfo
        today=datetime.now(ZoneInfo(self.policy['timezone_name'])).date()
        target=target_date or today.isoformat()
        try:day=date.fromisoformat(target)
        except ValueError:return {'status':'invalid_query','message':'目标日期需为ISO日期。'}
        if not today<=day<=today+timedelta(days=7):return {'status':'unavailable','message':'环境工具仅查询当前至未来7天，远期计划需临近再核实。'}
        location=self.resolve(target)
        if not location:return {'status':'missing_input','message':'缺少适用的已确认起点或位置已过期；请确认当前位置或出差城市和具体起点。'}
        if purpose in ('outing','restaurants') and (location.get('accuracy_m') or 0)>1000:return {'status':'missing_input','message':'当前位置精度不足以规划具体往返路线，请重新定位或确认城市和具体起点。'}
        allowed={'outing':{'park','trail','sports','fitness'},'restaurants':{'chinese','light','noodles','vegetarian','restaurant'},'weather':set()}[purpose]
        if any(c not in allowed for c in categories or []):return {'status':'invalid_query','message':'类别不在工具白名单。'}
        if purpose=='weather':
            args={'city':location.get('city',''),'target_date':target}
            args.update({k:location[k] for k in ('latitude','longitude') if location.get(k) is not None})
        else:
            args={k:v for k,v in location.items() if k in ('city','start_place','latitude','longitude')}
            args.update(radius_m=radius_m,categories=categories or (['park','trail'] if purpose=='outing' else ['chinese','light']))
        key=hashlib.sha256(dump({'tool':purpose,'args':args,'location':location,'target':target,
                                'adapter_revision':'weather-v2' if purpose in ('weather','outing') else 'v1'}).encode()).hexdigest()
        try:
            cached=self.continuity.get('environment',key)
            evidence=cached['evidence'];ttl=self.policy['environment_ttl_minutes']*60
            if purpose in ('weather','outing'):ttl=min(ttl,600)
            nested_weather=(evidence.get('data') or {}).get('weather',{}) if purpose=='outing' else None
            if evidence.get('status') not in ('ok','empty','partial') or nested_weather is not None and nested_weather.get('status')!='ok':ttl=min(ttl,30)
            if datetime.now(timezone.utc)-datetime.fromisoformat(cached['fetched_at'])<timedelta(seconds=ttl):
                result={**cached['evidence'],'cached':True};self.evidence.append(result);return result
        except KeyError:pass
        try:self.runtime.consume('external',self.policy['external_daily_budget'],self.policy['timezone_name'])
        except (ValueError,BudgetExceeded):return {'status':'limit_reached','message':'当日外部查询预算已用完。'}
        try:result=self.caller([{'name':purpose,'arguments':args}])[0]
        except Exception:
            result={'tool':purpose,'status':'unavailable','message':'环境服务暂不可用；未据此假设天气或路线。'}
        result.update(id='ENV_'+key[:20],target_date=target,location_basis=location,
                      fetched_at=datetime.now(timezone.utc).isoformat(),cached=False)
        if location.get('accuracy_m') is not None and location['accuracy_m']>100:
            result['precision_note']=f"起点精度约{location['accuracy_m']:g}米，距离和路线基于该近似起点，出发前确认实际起点。"
        self.continuity.put('environment',key,{'evidence':result,'fetched_at':result['fetched_at']})
        with self.lock:self.evidence.append(result)
        return result

    def bindings(self,role):
        allowed={'activity':{'weather','outing'},'diet':{'restaurants'},'sleep':{'weather'},'coordinator':{'weather','outing','restaurants'},'advisor':{'weather','outing','restaurants'}}.get(role,set())
        def query(**args):
            if args['purpose'] not in allowed:return {'status':'denied','message':'该角色不能请求此类环境能力。'}
            return self.query(**args)
        from app.multi_agent.tools import NoArguments
        bindings={'read_location':ToolBinding(NoArguments,self.location_context,'读取已授权位置是否有效、精度及联网范围；有坐标无需重复询问城市，不会触发新的系统授权。')}
        if allowed:bindings['query_environment']=ToolBinding(EnvironmentQuery,query,'按当前授权起点查询天气、附近场地或餐馆。仅返回工具证据；位置过期需补充。')
        return bindings
