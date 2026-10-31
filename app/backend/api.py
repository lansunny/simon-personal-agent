"""Minimal local-only FastAPI facade. Business actions are serialized for one user."""
import os
import hmac
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Header
from fastapi.responses import FileResponse, Response,RedirectResponse
from datetime import datetime, timezone, timedelta
import re
from app.backend.calendar_export import calendar_text
from app.integrations.mcp_client import call_tools
from fastapi.staticfiles import StaticFiles
from pydantic import Field

from app.multi_agent.tools import summarize_records
from app.config import settings
from app.schemas import DailyRecordInput, InputModel, UserProfileInput
from app.backend.service import Conflict, Service
from app.multi_agent.memory_contracts import HistoryQuery
from app.personal.contracts import LifeEvent, EventText, AutomationPolicy, CalendarImport, MealCalculation
from app.personal.agent import HealthPersonalAgent
from app.personal.api import router as personal_router
from app.multi_agent.quantification import attach_quantification, nutrition_total

ROOT = Path(__file__).resolve().parents[2]


class Position(InputModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    accuracy_m: float = Field(ge=0)


class GenerateInput(InputModel):
    mode: Literal['demo', 'live'] = 'demo'
    message: str = Field(default='', max_length=2000)
    city: str = Field(default='', max_length=80)
    use_external: bool = Field(default=False, strict=True)
    location: Position | None = None
    timezone_offset: int = Field(default=0, ge=-840, le=840)
    conversation_id: str | None = Field(default=None, max_length=64)


class ReplyInput(InputModel):
    message: str = Field(min_length=1, max_length=2000)
    city: str = Field(default='', max_length=80)
    expected_round: int = Field(ge=0, le=4, strict=True)
    location: Position | None = None


class EnvironmentInput(InputModel):
    location: Position
    place_name: str = Field(default='', max_length=100)


class DecisionInput(InputModel):
    approved: bool = Field(strict=True)


def create_app(data_dir=None):
    @asynccontextmanager
    async def lifespan(app):
        app.state.service = Service(data_dir or os.environ.get('HENGBAN_DATA_DIR', ROOT / 'data'))
        app.state.personal = HealthPersonalAgent(app.state.service)
        app.state.service.personal = app.state.personal
        from app.mobile.devices import Devices
        app.state.devices=Devices(app.state.service.store)
        try:
            yield
        finally:
            app.state.personal.close()
            app.state.service.close()

    app = FastAPI(title='Simon · 健康生活搭子', lifespan=lifespan)
    app.include_router(personal_router)
    from app.mobile.api import router as mobile_router
    app.include_router(mobile_router)
    from fastapi.responses import JSONResponse
    from app.multi_agent.runners.deepseek import ModelCallError
    from app.errors import BudgetExceeded
    @app.exception_handler(BudgetExceeded)
    async def daily_budget_exceeded(request,exc):
        return JSONResponse(status_code=429,content={'detail':str(exc),'code':'daily_budget_exceeded','budget':exc.details()})
    @app.exception_handler(ModelCallError)
    async def model_error(request,exc):
        return JSONResponse(status_code=502,content={'detail':str(exc)})
    @app.exception_handler(Conflict)
    async def conflict(request,exc):
        return JSONResponse(status_code=409,content={'detail':str(exc)})
    @app.middleware('http')
    async def mobile_access(request,call_next):
        path=request.url.path
        if path.startswith('/api/'):
            origin=request.headers.get('origin')
            if request.method not in ('GET','HEAD','OPTIONS') and origin and origin!=str(request.base_url).rstrip('/'):
                return JSONResponse(status_code=403,content={'detail':'请在同源Simon页面操作。'})
            local=request.client and request.client.host in ('127.0.0.1','::1','testclient')
            public=path in ('/api/health','/api/mobile/pair/claim')
            if not local and request.url.scheme!='https':
                return JSONResponse(status_code=403,content={'detail':'手机连接需要受信HTTPS服务。'})
            if not local and not public:
                from fastapi import HTTPException as AccessError
                try:app.state.devices.authenticate(request.headers.get('authorization',''),None if path.startswith('/api/mobile/bridge/') else 'app')
                except AccessError as exc:return JSONResponse(status_code=exc.status_code,content={'detail':exc.detail})
        return await call_next(request)
    @app.exception_handler(KeyError)
    async def missing_object(request,exc):
        return JSONResponse(status_code=404,content={'detail':'未找到对应的会话、目标、事项或任务。'})
    @app.exception_handler(ValueError)
    async def invalid_personal_request(request,exc):
        return JSONResponse(status_code=409,content={'detail':'请求未完成，请检查时间、对象版本、授权与输入；旧状态未被直接覆盖。'})
    app.mount('/static', StaticFiles(directory=ROOT / 'frontend'), name='static')

    @app.get('/api/health')
    def health():
        return {'app': 'Simon', 'version': 'simon-mobile-8', 'pid': os.getpid()}

    @app.get('/')
    def index():
        return RedirectResponse('/static/mobile/vivid/live.html')

    @app.get('/legacy')
    def legacy():
        return FileResponse(ROOT / 'frontend' / 'index.html')

    @app.get('/api/dashboard')
    def dashboard():
        svc = app.state.service
        with svc.lock:
            runs = [svc.get_run(r) for r in svc.store.runs()[:30]]
            records = svc.store.records()
            plans=svc.store.plans()
            profile=svc.store.profile()
            if profile:
                for plan in plans:
                    if plan['active'] and not plan['body'].get('quantification'):
                        attach_quantification(plan['body'],profile,records,datetime.now(timezone(timedelta(hours=8))).date().isoformat())
            return {'personal': app.state.personal.snapshot(), 'profile': profile, 'records': records, 'summary': summarize_records(records),
                    'plans': plans, 'runs': runs,
                    'memories': svc.memory.entries(datetime.now(timezone(timedelta(hours=8))).date().isoformat()),
                    'messages': svc.memory.messages(limit=30), 'live_configured': bool(settings.deepseek_api_key)}

    @app.put('/api/profile')
    def save_profile(body: UserProfileInput):
        svc = app.state.service
        with svc.lock:
            profile = body.model_dump(mode='json')
            svc.store.save_profile(profile)
            return profile

    @app.put('/api/records')
    def save_record(body: DailyRecordInput):
        svc = app.state.service
        with svc.lock:
            record = body.model_dump(mode='json')
            svc.store.save_record(record)
            return record

    @app.delete('/api/records/{day}')
    def delete_record(day: date):
        svc = app.state.service
        with svc.lock:
            svc.store.delete_record(day.isoformat())
            return {'deleted': True}

    @app.post('/api/runs')
    def generate(body: GenerateInput):
        try:
            return app.state.service.generate(body.mode, body.message, body.city, body.use_external,
                body.location.model_dump() if body.location else None, body.timezone_offset, body.conversation_id)
        except Conflict as exc:
            raise HTTPException(409, str(exc)) from None
        except ValueError:
            raise HTTPException(400, '请在本地配置有效的 DEEPSEEK_API_KEY 和 HTTPS 服务地址。') from None

    @app.get('/api/runs/{run_id}')
    def get_run(run_id: str):
        try:
            return app.state.service.get_run(run_id)
        except KeyError:
            raise HTTPException(404, '任务不存在。') from None

    @app.post('/api/runs/{run_id}/reply')
    def reply(run_id: str, body: ReplyInput):
        try:
            return app.state.service.reply(run_id, body.message, body.city, body.expected_round,
                body.location.model_dump() if body.location else None)
        except KeyError:
            raise HTTPException(404, '任务不存在。') from None
        except Conflict as exc:
            raise HTTPException(409, str(exc)) from None
        except ValueError:
            raise HTTPException(400, '请检查本地模型配置后重启。') from None

    @app.post('/api/runs/{run_id}/decision')
    def decide(run_id: str, body: DecisionInput):
        try:
            return app.state.service.decide(run_id, body.approved)
        except KeyError:
            raise HTTPException(404, '任务不存在。') from None
        except Conflict as exc:
            raise HTTPException(409, str(exc)) from None

    @app.post('/api/plans/revoke')
    def revoke():
        svc = app.state.service
        with svc.lock:
            svc.store.revoke_plan()
            return {'revoked': True}

    @app.post('/api/chat')
    def chat(body: GenerateInput):
        svc = app.state.service
        try:
            message = body.message.strip()
            if not message:
                raise HTTPException(400, '请先说说你的需求。')
            today = datetime.now(timezone(timedelta(minutes=-body.timezone_offset))).date().isoformat()
            source_message_id = svc.memory.message('user', message)
            memory_reply = svc.memory_command(message, today, source_message_id)
            if memory_reply:
                return memory_reply
            # 仅“记录”开头的明确数值写入日记，其他自然语言仍交给对话图。
            if message.startswith('记录'):
                today = datetime.now(timezone(timedelta(minutes=-body.timezone_offset))).date().isoformat()
                old = next((r for r in svc.store.records() if r['record_date'] == today), {'record_date': today})
                weight = re.search(r'体重\s*(\d+(?:\.\d+)?)\s*(?:公斤|kg|千克)?', message)
                sleep = re.search(r'睡眠\s*(\d+(?:\.\d+)?)\s*小时', message)
                activity = re.search(r'(?:步行|运动|跑步|活动)\s*(\d+)\s*分钟', message)
                if not weight and not activity and not sleep:
                    return svc.simple_reply('可以这样说：记录今天步行20分钟，体重70公斤，睡眠7.5小时。暂未保存。')
                if weight:
                    old['weight_kg'] = float(weight[1])
                if sleep:
                    old['sleep_hours'] = float(sleep[1])
                if activity:
                    old['activity_minutes'] = int(activity[1])
                record = DailyRecordInput.model_validate(old).model_dump(mode='json')
                svc.store.save_record(record)
                return svc.simple_reply('已保存今天的记录。下次复盘会参考这些数据。')
            position = body.location.model_dump() if body.location else None
            if body.conversation_id:
                current = svc.get_run(body.conversation_id)
                if current['status'] == 'waiting_clarification':
                    run = svc.reply(current['id'], message, body.city, current['state'].get('clarification_round', 0), position, body.use_external, source_message_id)
                    return {'run': run}
                if current['status'] == 'waiting_confirmation' and re.fullmatch(r'(确认|同意|可以|就这样|好的)[。！!\s]*', message):
                    return {'run': svc.decide(current['id'], True, source_message_id)}
                if current['status'] == 'waiting_confirmation' and re.fullmatch(r'(取消|拒绝|不要了)[。！!\s]*', message):
                    return {'run': svc.decide(current['id'], False, source_message_id)}
            run = svc.generate(body.mode, message, body.city, body.use_external, position, body.timezone_offset, body.conversation_id, source_message_id)
            return {'run': run}
        except KeyError:
            raise HTTPException(404, '原对话不存在，请新建对话。') from None
        except Conflict as exc:
            raise HTTPException(409, str(exc)) from None
        except ValueError:
            raise HTTPException(400, '请检查输入数值与本地模型配置。') from None

    @app.post('/api/location-context')
    def location_context(body: EnvironmentInput):
        svc = app.state.service
        with svc.lock:
            position = body.location.model_dump()
            output = call_tools([{'name': 'outing', 'arguments': {'latitude': position['latitude'],
                                 'longitude': position['longitude'], 'place_name': body.place_name}}])[0]
            svc.environment_cache = {'location': position, 'place_name': body.place_name,
                                     'fetched_at': datetime.now(timezone.utc).isoformat(), 'evidence': output}
            return output

    @app.get('/api/memories')
    def memories():
        return app.state.service.memory.entries(datetime.now(timezone(timedelta(hours=8))).date().isoformat())

    @app.post('/api/memories/{memory_id}/decision')
    def memory_decision(memory_id: str, body: DecisionInput):
        svc = app.state.service
        with svc.lock:
            try:
                result = svc.memory.decide(memory_id, body.approved, datetime.now(timezone(timedelta(hours=8))).date().isoformat())
                svc.memory.message('user', ('确认' if body.approved else '拒绝') + '记忆 ' + memory_id)
                svc.memory.message('assistant', '记忆状态已更新：' + result['status'])
                return result
            except KeyError:
                raise HTTPException(404, '记忆不存在。') from None
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from None

    @app.post('/api/memories/{memory_id}/revoke')
    def memory_revoke(memory_id: str):
        svc = app.state.service
        with svc.lock:
            return {'revoked': svc.memory.revoke(memory_id)}

    @app.post('/api/history/query')
    def history_query(body: HistoryQuery):
        svc = app.state.service
        with svc.lock:
            return svc.memory.history(body.model_dump(), datetime.now(timezone(timedelta(hours=8))).date().isoformat())

    @app.get('/api/history/messages')
    def history_messages(query: str = '', before: str | None = None, limit: int = 20):
        return app.state.service.memory.messages(query[:100], limit, before)

    @app.get('/api/history/messages/{message_id}')
    def history_message(message_id: str):
        try:
            return app.state.service.memory.source(message_id)
        except KeyError:
            raise HTTPException(404, '未找到来源消息。') from None

    @app.get('/api/history/records/{day}')
    def history_records(day: date):
        svc = app.state.service
        return {'records': svc.store.records_range(day.isoformat(), day.isoformat()),
                'revisions': svc.store.revisions('record', day.isoformat())}

    @app.get('/api/history/plans/{version}')
    def history_plan(version: int):
        plan = next((p for p in app.state.service.store.plans() if p['version'] == version), None)
        if plan is None:
            raise HTTPException(404, '没有保存该计划版本。')
        return plan

    @app.get('/api/plan/calendar.ics')
    def export_calendar():
        svc = app.state.service
        with svc.lock:
            plan = svc.store.active_plan()
            if not plan:
                raise HTTPException(404, '请先确认一份计划。')
            return Response(calendar_text(plan), media_type='text/calendar; charset=utf-8',
                            headers={'Content-Disposition': 'attachment; filename="hengban-plan.ics"'})


    @app.get('/api/personal')
    def personal_state():
        with app.state.service.lock:
            return app.state.personal.snapshot()

    @app.put('/api/personal/policy')
    def personal_policy(body: AutomationPolicy):
        return app.state.personal.save_policy(body.model_dump())

    @app.post('/api/personal/events')
    def ingest_event(body: LifeEvent):
        return app.state.personal.ingest(body.model_dump(mode='json'))

    @app.post('/api/personal/event-text')
    def ingest_text(body: EventText):
        return app.state.personal.ingest_message(body)

    @app.post('/api/personal/bridge/events')
    def bridge_event(body: LifeEvent, x_hengban_token: str = Header(default='')):
        if not settings.event_bridge_token:
            raise HTTPException(503,'本机尚未配置事件桥接令牌。')
        if not hmac.compare_digest(x_hengban_token,settings.event_bridge_token):
            raise HTTPException(403,'事件桥接认证失败。')
        return app.state.personal.ingest(body.model_dump(mode='json'))

    @app.post('/api/personal/bridge/messages')
    def bridge_message(body: EventText, x_hengban_token: str = Header(default='')):
        if not settings.event_bridge_token:
            raise HTTPException(503,'本机尚未配置事件桥接令牌。')
        if not hmac.compare_digest(x_hengban_token,settings.event_bridge_token):
            raise HTTPException(403,'事件桥接认证失败。')
        return ingest_text(body.model_copy(update={'source':'message'}))

    @app.post('/api/personal/calendar/import')
    def calendar_import(body: CalendarImport):
        try:return app.state.personal.import_calendar(body.content)
        except ValueError as exc:raise HTTPException(400,str(exc)) from None

    @app.post('/api/personal/calendar/sync')
    def calendar_sync():
        try:return app.state.personal.sync_calendar()
        except ValueError as exc:raise HTTPException(400,str(exc)) from None

    @app.post('/api/personal/actions/{action_id}/decision')
    def action_decision(action_id: str, body: DecisionInput):
        try:return app.state.personal.decide(action_id,body.approved)
        except ValueError as exc:raise HTTPException(409,str(exc)) from None

    @app.post('/api/personal/actions/{action_id}/undo')
    def undo_action(action_id: str):
        try:return app.state.personal.undo(action_id)
        except ValueError as exc:raise HTTPException(409,str(exc)) from None

    @app.post('/api/personal/outbox/{item_id}/read')
    def read_notification(item_id: str):
        app.state.personal.acknowledge(item_id)
        return {'read':True}

    @app.post('/api/nutrition/calculate')
    def calculate_meal(body: MealCalculation):
        return nutrition_total([i.model_dump() for i in body.items])

    return app


app = create_app()
