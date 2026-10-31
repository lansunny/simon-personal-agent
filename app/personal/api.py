from fastapi import APIRouter,Request,HTTPException,Header
from pydantic import Field
from app.schemas import InputModel
from app.personal.contracts import SessionInput,GoalInput,TaskInput,TaskFeedback,PersonalChatInput,JobInput,ScheduleInput,MealTemplateInput
from app.personal.environment import LocationInput,EnvironmentQuery


class StateChange(InputModel):
    status: str = Field(pattern='^(active|paused|completed|cancelled)$')
    expected_revision: int = Field(ge=1)


class EnabledInput(InputModel):
    enabled: bool


class DeliveryReceipt(InputModel):
    claim_token: str = Field(min_length=1,max_length=100)
    status: str = Field(pattern='^(accepted|displayed|failed|unknown)$')
    provider_id: str = Field(default='',max_length=200)


def bridge_auth(token):
    import hmac
    from app.config import settings
    if not settings.event_bridge_token:raise HTTPException(503,'尚未配置接收端令牌。')
    if not hmac.compare_digest(token,settings.event_bridge_token):raise HTTPException(403,'接收端认证失败。')


router=APIRouter(prefix='/api/personal',tags=['健康Personal Agent后端'])


@router.post('/sessions')
def create_session(body:SessionInput,request:Request):
    return request.app.state.personal.continuity.session(body.title,body.timezone_name)


@router.get('/sessions/{session_id}/messages')
def messages(session_id:str,request:Request,limit:int=20):
    return request.app.state.personal.continuity.messages(session_id,limit)


@router.post('/goals')
def create_goal(body:GoalInput,request:Request):
    return request.app.state.personal.continuity.create_goal(body.model_dump(mode='json'))


@router.get('/goals')
def goals(request:Request):return request.app.state.personal.continuity.list('goal')


@router.post('/goals/{goal_id}/state')
def goal_state(goal_id:str,body:StateChange,request:Request):
    agent=request.app.state.personal;goal=agent.continuity.get('goal',goal_id)
    result=agent.continuity.put('goal',goal_id,goal,body.status,body.expected_revision)
    agent.update_live(goal_id=goal_id);return result


@router.get('/context')
def context(request:Request,session_id:str|None=None,goal_id:str|None=None):return request.app.state.personal.context(session_id,goal_id)


@router.get('/quantities')
def quantities(request:Request):
    from app.personal.schedule_engine import normalize
    from app.multi_agent.quantification import attach_quantification
    from datetime import datetime
    from zoneinfo import ZoneInfo
    agent=request.app.state.personal
    with agent.service.lock:
        active=agent.store.active_plan();profile=agent.store.profile()
        if not active or not profile:return {'status':'missing','message':'请先建立档案并确认计划。'}
        policy=agent.policy();today=datetime.now(ZoneInfo(policy['timezone_name'])).date().isoformat()
        profile=agent.service.memory.profile(profile,agent.service.memory.recall(today))
        plan=attach_quantification(normalize(active['body'],profile,policy),profile,agent.store.records(),today)
    return {'status':'ok','plan_version':active['version'],'as_of':today,'quantification':plan['quantification'],
            'advice':plan['quantified_advice'],'note':'按最新有效输入重新计算的读取视图；没有修改已确认计划或实际记录。'}


@router.post('/tasks')
def create_task(body:TaskInput,request:Request):
    result=request.app.state.personal.continuity.create_task(body.model_dump(mode='json'))
    request.app.state.personal.sync_reminders();request.app.state.personal.update_live(goal_id=body.goal_id);return result


@router.get('/tasks')
def tasks(request:Request,goal_id:str|None=None,status:str|None=None):
    return [t for t in request.app.state.personal.continuity.list('task',status,1000) if not goal_id or t.get('goal_id')==goal_id]


@router.post('/tasks/{task_id}/feedback')
def feedback(task_id:str,body:TaskFeedback,request:Request):return request.app.state.personal.feedback(task_id,body.model_dump(mode='json'))


@router.get('/tasks/{task_id}')
def get_task(task_id:str,request:Request):return request.app.state.personal.continuity.get('task',task_id)


@router.post('/chat')
def chat(body:PersonalChatInput,request:Request):
    from app.personal.conversation import chat as handle
    return handle(request.app.state.personal,body)


@router.get('/jobs')
def jobs(request:Request):return request.app.state.personal.runtime.list()


@router.post('/jobs')
def enqueue_job(body:JobInput,request:Request):
    payload={**body.payload,'goal_id':body.goal_id}
    return request.app.state.personal.runtime.enqueue(body.kind,payload,'api:'+body.idempotency_key,body.run_at)


@router.get('/jobs/{job_id}')
def get_job(job_id:str,request:Request):return request.app.state.personal.runtime.get(job_id)


@router.post('/jobs/{job_id}/run')
def run_job(job_id:str,request:Request):return request.app.state.personal.runtime.run_now(job_id)


@router.post('/jobs/{job_id}/retry')
def retry_job(job_id:str,request:Request):return request.app.state.personal.runtime.retry(job_id)


@router.post('/jobs/{job_id}/cancel')
def cancel_job(job_id:str,request:Request):return request.app.state.personal.runtime.cancel(job_id)


@router.post('/schedules')
def create_schedule(body:ScheduleInput,request:Request):return request.app.state.personal.runtime.add_schedule(body.model_dump())


@router.get('/schedules')
def schedules(request:Request):return request.app.state.personal.runtime.schedules()


@router.post('/schedules/{schedule_id}/enabled')
def schedule_enabled(schedule_id:str,body:EnabledInput,request:Request):
    request.app.state.personal.runtime.schedule_enabled(schedule_id,body.enabled);return {'enabled':body.enabled}


@router.put('/location')
def location(body:LocationInput,request:Request):return request.app.state.personal.set_location(body.model_dump(mode='json'))


@router.post('/environment/query')
def query_environment(body:EnvironmentQuery,request:Request):return request.app.state.personal.environment().query(**body.model_dump())


@router.get('/reviews')
def reviews(request:Request):return request.app.state.personal.continuity.list('review')


@router.post('/meal-templates')
def create_meal_template(body:MealTemplateInput,request:Request):return request.app.state.personal.save_meal_template(body.model_dump())


@router.get('/meal-templates')
def meal_templates(request:Request):return request.app.state.personal.meal_templates()


@router.get('/observations')
def observations(request:Request):return request.app.state.personal.continuity.list('observation')


@router.get('/decisions')
def decisions(request:Request):return request.app.state.personal.continuity.journal('decision',limit=50)


@router.get('/skills')
def skills():
    from app.personal.capabilities import SkillCatalog
    return SkillCatalog().list()


@router.get('/delivery/receipts')
def delivery_receipts(request:Request):return request.app.state.personal.delivery.list()


@router.post('/delivery/claim')
def claim_delivery(request:Request,x_hengban_token:str=Header(default='')):
    bridge_auth(x_hengban_token)
    if not request.app.state.personal.policy()['delivery_enabled']:raise HTTPException(409,'请先开启通知接收端投递。')
    return request.app.state.personal.delivery.claim()


@router.post('/delivery/{outbox_id}/receipt')
def receipt(outbox_id:str,body:DeliveryReceipt,request:Request,x_hengban_token:str=Header(default='')):
    bridge_auth(x_hengban_token)
    return request.app.state.personal.delivery.receipt(outbox_id,body.claim_token,body.status,body.provider_id)


@router.post('/delivery/{outbox_id}/retry')
def retry_delivery(outbox_id:str,request:Request):return request.app.state.personal.delivery.retry(outbox_id)
