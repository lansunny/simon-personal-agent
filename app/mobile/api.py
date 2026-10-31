"""移动端聚合读取和Android/WebView能力桥接。"""
from datetime import datetime,timezone,timedelta
from typing import Literal
from zoneinfo import ZoneInfo
from uuid import uuid4
from fastapi import APIRouter,Request,Header,HTTPException
from pydantic import Field
from app.schemas import InputModel,DailyRecordInput
from app.config import settings
from app.personal.contracts import CalendarImport,EventText
from app.personal.environment import LocationInput
from app.personal.api import quantities
from app.mobile.devices import SCOPES

router=APIRouter(prefix='/api/mobile',tags=['Simon移动端与手机权限'])


class SessionClient(InputModel):
    client_id:str=Field(pattern=r'^[a-zA-Z0-9_-]{16,80}$')
    timezone_name:str='Asia/Shanghai'

class PairingInput(InputModel):
    scopes:list[Literal['app','location','calendar','notifications','health','event_messages']]=Field(min_length=1,max_length=6)

class PairClaim(InputModel):
    code:str=Field(min_length=10,max_length=10)
    name:str=Field(default='我的安卓手机',min_length=1,max_length=80)
    platform:Literal['android','ios','web']='android'

class PermissionReport(InputModel):
    states:dict[str,Literal['granted','denied','prompt','unsupported','revoked']]=Field(max_length=6)

class DeviceRecord(InputModel):
    request_id:str=Field(min_length=1,max_length=100)
    record:DailyRecordInput

class StepObservation(InputModel):
    request_id:str=Field(min_length=1,max_length=100)
    observed_at:datetime
    cumulative_since_boot:int=Field(ge=0,strict=True)
    session_steps:int=Field(ge=0,strict=True)
    session_started_at:datetime

class PhoneReceipt(InputModel):
    claim_token:str=Field(min_length=1,max_length=100)
    status:Literal['accepted','displayed','failed','unknown']
    provider_id:str=Field(default='',max_length=200)

class InboxReply(InputModel):
    message:str=Field(min_length=1,max_length=1500)


def owner(request):
    if request.client and request.client.host in ('127.0.0.1','::1','testclient'):return
    raise HTTPException(403,'请在本机管理页面创建配对或撤销设备。')

def device(request,authorization,scope):
    value=request.app.state.devices.authenticate(authorization,scope)
    request.app.state.devices.require_granted(value,scope)
    return value


@router.post('/session')
def session(body:SessionClient,request:Request):
    ZoneInfo(body.timezone_name)
    agent=request.app.state.personal
    with agent.service.lock:
        try:
            link=agent.continuity.get('client_session',body.client_id)
            return agent.continuity.get('session',link['session_id'])
        except KeyError:
            try:
                demo=agent.continuity.get('demo_session','current')
                value=agent.continuity.get('session',demo['session_id'])
            except KeyError:value=agent.continuity.session('与Simon的日常',body.timezone_name)
            agent.continuity.put('client_session',body.client_id,{'session_id':value['id']})
            return value


@router.get('/state')
def state(request:Request,session_id:str|None=None):
    agent=request.app.state.personal;svc=agent.service
    with svc.lock:
        profile=svc.store.profile();active=svc.store.active_plan()
        session=agent.continuity.get('session',session_id) if session_id else None
        run_ids=(session or {}).get('run_ids',[])[-5:] if session else svc.store.runs()[:5]
        runs=[svc.get_run(key) for key in run_ids if not key.startswith('personal-job:')]
        try:location=agent.continuity.get('location','current')
        except KeyError:location=None
        return {'app':'Simon','server_time':datetime.now(timezone.utc).isoformat(),
                'config':{'model':settings.deepseek_model,'live_configured':bool(settings.deepseek_api_key),'maps_configured':bool(settings.amap_api_key),
                          'native_protocol':'simon-bridge-v1','phone_requires_https':True},
                'profile':profile,'active_plan':active,'runs':runs,'records':svc.store.records(),
                'quantities':quantities(request),'tasks':agent.continuity.list('task',limit=2000),
                'goals':agent.continuity.list('goal'),'actions':agent.actions(),'jobs':agent.runtime.list(20),
                'outbox':agent.outbox(),'reviews':agent.continuity.list('review',limit=10),
                'memories':svc.memory.entries(datetime.now(ZoneInfo(agent.policy()['timezone_name'])).date().isoformat()),
                'messages':agent.continuity.messages(session_id,60) if session_id else [],'policy':agent.policy(),
                'budgets':agent.runtime.budgets(agent.policy()),
                'phone_inbox':agent.continuity.list('phone_inbox',limit=30),
                'location':location,'location_context':agent.environment().location_context(),
                'devices':request.app.state.devices.list() if request.client.host in ('127.0.0.1','::1','testclient') else [],
                'context':agent.context(session_id),'meal_templates':agent.meal_templates()}


@router.delete('/location')
def revoke_location(request:Request):
    agent=request.app.state.personal
    try:old=agent.continuity.get('location','current')
    except KeyError:return {'revoked':True}
    agent.continuity.put('location','current',old,'revoked',old['revision'])
    return {'revoked':True,'note':'停止把此位置用于新查询；历史计划证据继续保留来源。'}


@router.post('/pairings')
def create_pairing(body:PairingInput,request:Request):
    owner(request);return request.app.state.devices.pairing(body.scopes)


@router.post('/pair/claim')
def claim_pairing(body:PairClaim,request:Request):
    return request.app.state.devices.claim(body.code,body.name,body.platform,request.client.host)


@router.get('/devices')
def devices(request:Request):
    owner(request);return request.app.state.devices.list()


@router.post('/devices/{device_id}/revoke')
def revoke_device(device_id:str,request:Request):
    owner(request)
    result=request.app.state.devices.revoke(device_id)
    try:
        old=request.app.state.personal.continuity.get('location','current')
        if old.get('device_id')==device_id:request.app.state.personal.continuity.put('location','current',old,'revoked',old['revision'])
    except KeyError:pass
    return result


@router.post('/bridge/permissions')
def report_permissions(body:PermissionReport,request:Request,authorization:str=Header(default='')):
    current=request.app.state.devices.authenticate(authorization)
    result=request.app.state.devices.report(current,body.states)
    if body.states.get('location') in ('denied','revoked'):
        try:
            old=request.app.state.personal.continuity.get('location','current')
            if old.get('device_id')==current['id']:revoke_location(request)
        except KeyError:pass
    return result


@router.post('/bridge/location')
def device_location(body:LocationInput,request:Request,authorization:str=Header(default='')):
    current=device(request,authorization,'location')
    return request.app.state.personal.set_location({**body.model_dump(mode='json'),'device_id':current['id']})


@router.post('/bridge/calendar')
def device_calendar(body:CalendarImport,request:Request,authorization:str=Header(default='')):
    device(request,authorization,'calendar');return request.app.state.personal.import_calendar(body.content)


@router.post('/bridge/event-message')
def device_event(body:EventText,request:Request,authorization:str=Header(default='')):
    current=device(request,authorization,'event_messages')
    if not any(word in body.text for word in ('会议','饭局','聚餐','出差','加班','改期','取消')):return {'status':'ignored','message':'非健康日程相关消息，未交给模型。'}
    import re
    if re.search(r'验证码|校验码|动态密码|\bOTP\b|one.time.password',body.text,re.I):return {'status':'ignored','message':'敏感验证消息不会进入Agent。'}
    from hashlib import sha256
    external_id=body.external_id or sha256((str(body.reference_at)+body.text).encode()).hexdigest()[:32]
    agent=request.app.state.personal
    event=body.model_copy(update={'source':'message','mode':'live','session_id':None,'external_id':current['id']+':'+external_id[:120]})
    key=sha256(event.external_id.encode()).hexdigest()[:32]
    try:
        old=agent.continuity.get('phone_inbox',key)
        if old.get('event',{}).get('text')==body.text:return {'duplicate':True,'inbox_id':key,'message':old.get('message','已接收此通知。')}
    except KeyError:pass
    # 先存外部来源，再提取；缺项和失败都能在界面找回，不当作用户直接指令。
    entry={'device_id':current['id'],'event':event.model_dump(mode='json'),'message':'手机通知已接收，正在提取日程。'}
    agent.continuity.put('phone_inbox',key,entry,'processing')
    return process_phone_event(agent,key,entry,event)


def process_phone_event(agent,key,entry,event):
    try:
        result=agent.ingest_message(event)
        status='needs_information' if result.get('needs_information') else 'processed'
        entry={**entry,'message':result.get('message','已处理。'),'job_id':result.get('job',{}).get('id'),'event_id':result.get('event_id')}
        agent.continuity.put('phone_inbox',key,entry,status)
        return {**result,'inbox_id':key}
    except Exception:
        agent.continuity.put('phone_inbox',key,{**entry,'message':'手机消息已保存，提取暂未完成。可补充明确日期和起止时间后重试。'},'sync_failed')
        return {'inbox_id':key,'status':'sync_failed','message':'已保存通知，等待补充或重试。'}


@router.post('/inbox/{inbox_id}/reply')
def reply_phone_event(inbox_id:str,body:InboxReply,request:Request):
    agent=request.app.state.personal;entry=agent.continuity.get('phone_inbox',inbox_id)
    if entry['status']=='processed':raise HTTPException(409,'此通知已提取；改期请在对话中引用对应事件。')
    event=EventText.model_validate(entry['event'])
    # 每次补充重新解析原文+本次澄清，不丢失原始通知，不将外部话语授予自动授权。
    agent.continuity.append('phone_event_reply',inbox_id,{'message':body.message},'phone-reply:'+uuid4().hex)
    event=event.model_copy(update={'text':event.text[:2400]+'；用户补充：'+body.message,'mode':'live'})
    return process_phone_event(agent,inbox_id,entry,event)


@router.post('/bridge/health')
def device_health(body:DeviceRecord,request:Request,authorization:str=Header(default='')):
    current=device(request,authorization,'health');agent=request.app.state.personal
    key='phone-health:'+current['id']+':'+body.request_id
    value=body.record.model_dump(mode='json')
    with agent.service.lock:
        with agent.store.connection() as db:old=db.execute('SELECT body FROM personal_journal WHERE dedupe_key=?',(key,)).fetchone()
        if old:
            import json
            if json.loads(old['body'])!=value:raise ValueError('相同来源标识不能用于不同健康记录。')
            return {'duplicate':True}
        existing=next(iter(agent.store.records_range(value['record_date'],value['record_date'])),{'record_date':value['record_date']})
        # 手机同步仅填缺失值；已由用户填写的数据仍由用户主动编辑。
        merged={**existing,**{k:v for k,v in value.items() if v is not None and existing.get(k) is None}}
        agent.store.save_record(merged);agent.continuity.append('device_health',current['id'],value,key)
    return {'saved':True,'record':merged,'source':'paired_device','note':'不以计划值填补实际值，也不覆盖手工记录。'}


@router.post('/bridge/steps')
def device_steps(body:StepObservation,request:Request,authorization:str=Header(default='')):
    current=device(request,authorization,'health');agent=request.app.state.personal
    now=datetime.now(timezone.utc)
    if body.observed_at.tzinfo is None or body.session_started_at.tzinfo is None or not body.session_started_at<=body.observed_at<=now+timedelta(minutes=5):
        raise ValueError('计步观测时间无效。')
    if body.session_steps>body.cumulative_since_boot:raise ValueError('本次会话步数不能超出传感器累计数。')
    value=body.model_dump(mode='json')
    agent.continuity.append('device_steps',current['id'],value,'steps:'+current['id']+':'+body.request_id)
    return {'saved':True,'session_steps':body.session_steps,'note':'仅为本次原生采集会话步数，不等于全天步数，不换算实际运动分钟或热量。'}


@router.post('/bridge/notifications/claim')
def claim_notifications(request:Request,authorization:str=Header(default='')):
    device(request,authorization,'notifications')
    if not request.app.state.personal.policy()['delivery_enabled']:raise HTTPException(409,'请先在Simon里启用设备提醒投递。')
    return request.app.state.personal.delivery.claim('android',5)


@router.post('/bridge/notifications/{item_id}/receipt')
def notification_receipt(item_id:str,body:PhoneReceipt,request:Request,authorization:str=Header(default='')):
    device(request,authorization,'notifications')
    return request.app.state.personal.delivery.receipt(item_id,body.claim_token,body.status,body.provider_id)
