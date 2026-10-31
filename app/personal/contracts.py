from datetime import datetime
from typing import Literal
from pydantic import Field, model_validator, field_validator
from app.schemas import InputModel


class LifeEvent(InputModel):
    source: Literal['manual', 'calendar', 'message', 'environment'] = 'manual'
    external_id: str = Field(min_length=1, max_length=200)
    source_sequence: int | None = Field(default=None, ge=0)
    title: str = Field(min_length=1, max_length=200)
    kind: Literal['meeting', 'dinner', 'trip', 'other'] = 'meeting'
    status: Literal['confirmed', 'tentative', 'cancelled'] = 'confirmed'
    start_at: datetime
    end_at: datetime
    location: str = Field(default='', max_length=300)
    source_text: str = Field(default='', max_length=4000)
    timezone_name: str = Field(default='Asia/Shanghai', max_length=80)
    related_event_id: str | None = Field(default=None, max_length=200)
    city: str = Field(default='', max_length=100)
    start_place: str = Field(default='', max_length=200)
    location_confirmed: bool = False
    all_day: bool = False
    occupies_time: bool | None = None
    trigger: str = Field(default='',max_length=50)
    duration_basis: str = Field(default='explicit',max_length=100)

    @model_validator(mode='after')
    def check(self):
        if self.start_at.tzinfo is None or self.end_at.tzinfo is None:
            raise ValueError('事件时间必须包含时区。')
        if self.end_at <= self.start_at:
            raise ValueError('事件结束时间必须晚于开始时间。')
        if (self.end_at-self.start_at).total_seconds() > 31*86400:
            raise ValueError('单个事件最长31天。')
        return self


class EventExtraction(InputModel):
    title: str = Field(max_length=200)
    kind: Literal['meeting', 'dinner', 'trip', 'other']
    status: Literal['confirmed', 'tentative', 'cancelled']
    start_at: str = Field(default='', max_length=40)
    end_at: str = Field(default='', max_length=40)
    location: str = Field(default='', max_length=300)
    missing_fields: list[str] = Field(default_factory=list, max_length=8)
    related_event_id: str | None = Field(default=None,max_length=100)
    city: str = Field(default='',max_length=100)
    start_place: str = Field(default='',max_length=200)
    all_day: bool = False


class EventText(InputModel):
    text: str = Field(min_length=1, max_length=4000)
    external_id: str = Field(default='', max_length=200)
    source: Literal['manual', 'message'] = 'manual'
    reference_at: datetime | None = None
    mode: Literal['demo', 'live'] = 'demo'
    related_event_id: str | None = Field(default=None,max_length=100)
    session_id: str | None = Field(default=None,max_length=100)


class AutomationPolicy(InputModel):
    auto_adjust: bool = False
    max_shift_minutes: int = Field(default=120, ge=0, le=360)
    earliest_time: str = '07:00'
    latest_time: str = '21:30'
    bedtime_buffer_minutes: int = Field(default=30, ge=0, le=180)
    event_padding_minutes: int = Field(default=10, ge=0, le=120)
    reminders_enabled: bool = False
    reminder_before_minutes: int = Field(default=5, ge=0, le=120)
    quiet_start: str = '23:00'
    quiet_end: str = '07:00'
    calendar_enabled: bool = False
    agent_mode: Literal['demo', 'live'] = 'demo'
    timezone_name: str = 'Asia/Shanghai'
    auto_adjust_domains: list[Literal['activity','diet','sleep']] = Field(default_factory=lambda:['activity'])
    allow_cross_day: bool = False
    max_reschedule_days: int = Field(default=2, ge=0, le=7)
    allow_activity_shortening: bool = False
    min_activity_minutes: int = Field(default=10, ge=5, le=120)
    allow_sleep_shift: bool = False
    allow_wake_shift: bool = False
    max_sleep_shift_minutes: int = Field(default=60, ge=0, le=180)
    min_sleep_opportunity_hours: float = Field(default=7, ge=4, le=12)
    allow_meal_shift: bool = False
    max_meal_shift_minutes: int = Field(default=120,ge=0,le=360)
    auto_allow_environment_fallback: bool = False
    meal_duration_minutes: int = Field(default=30, ge=10, le=120)
    breakfast_time: str = '08:00'
    lunch_time: str = '12:00'
    dinner_time: str = '18:30'
    allow_environment_queries: bool = False
    environment_ttl_minutes: int = Field(default=60, ge=5, le=1440)
    location_ttl_minutes: int = Field(default=120, ge=5, le=10080)
    external_daily_budget: int = Field(default=24, ge=0, le=500)
    model_daily_budget: int = Field(default=200, ge=0, le=1000)
    reviews_enabled: bool = False
    review_time: str = '21:30'
    delivery_enabled: bool = False

    @field_validator('timezone_name')
    @classmethod
    def timezone(cls,value):
        from zoneinfo import ZoneInfo
        try:ZoneInfo(value)
        except Exception:raise ValueError('时区名称无效。') from None
        return value

    @field_validator('earliest_time', 'latest_time', 'quiet_start', 'quiet_end', 'breakfast_time', 'lunch_time', 'dinner_time', 'review_time')
    @classmethod
    def clock(cls, value):
        import re
        if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', value):
            raise ValueError('时间使用HH:MM。')
        return value

    @model_validator(mode='after')
    def times(self):
        if self.earliest_time >= self.latest_time:
            raise ValueError('活动可用时段需在同一天内，结束晚于开始。')
        return self


class CalendarImport(InputModel):
    content: str = Field(min_length=1, max_length=1_000_000)


class FoodPortion(InputModel):
    name: str = Field(min_length=1, max_length=100)
    grams: float = Field(gt=0, le=10000)
    kcal_per_100g: float = Field(ge=0, le=1000)
    protein_g_per_100g: float | None = Field(default=None, ge=0, le=100)
    fat_g_per_100g: float | None = Field(default=None, ge=0, le=100)
    carbs_g_per_100g: float | None = Field(default=None, ge=0, le=100)
    source_label: str = Field(min_length=1, max_length=300, description='用户抄录的标签或可核实来源')

    @model_validator(mode='after')
    def mass(self):
        if sum(value for value in (self.protein_g_per_100g, self.fat_g_per_100g, self.carbs_g_per_100g) if value is not None) > 101:
            raise ValueError('每100g食物的已知三大营养素合计超过100g，请检查单位。')
        return self


class MealCalculation(InputModel):
    items: list[FoodPortion] = Field(min_length=1, max_length=30)


class MealTemplateInput(InputModel):
    name: str = Field(min_length=1,max_length=120)
    items: list[FoodPortion] = Field(min_length=1,max_length=30)


class SessionInput(InputModel):
    title: str = Field(default='健康日常',max_length=120)
    timezone_name: str = 'Asia/Shanghai'


class GoalInput(InputModel):
    title: str = Field(min_length=1,max_length=120)
    objective: str = Field(min_length=1,max_length=1000)
    domains: list[Literal['activity','diet','sleep']] = Field(default_factory=lambda:['activity','diet','sleep'],min_length=1,max_length=3)
    target: dict = Field(default_factory=dict)
    deadline: datetime | None = None
    session_id: str | None = Field(default=None,max_length=100)


class TaskInput(InputModel):
    title: str = Field(min_length=1,max_length=200)
    kind: Literal['activity','meal','sleep','checkin','review','other']
    goal_id: str | None = Field(default=None,max_length=100)
    session_id: str | None = Field(default=None,max_length=100)
    start_at: datetime | None = None
    end_at: datetime | None = None
    notes: str = Field(default='',max_length=1000)
    @model_validator(mode='after')
    def interval(self):
        for t in (self.start_at,self.end_at):
            if t is not None and t.tzinfo is None:raise ValueError('任务时间需包含时区。')
        if self.end_at and (not self.start_at or self.end_at<=self.start_at):raise ValueError('任务结束时间需晚于开始时间。')
        return self


class TaskFeedback(InputModel):
    outcome: Literal['completed','partial','skipped']
    note: str = Field(default='',max_length=2000)
    actual_minutes: float | None = Field(default=None,ge=0,le=1440)
    sleep_hours: float | None = Field(default=None,ge=0,le=24)
    fatigue: int | None = Field(default=None,ge=1,le=5)
    meal: MealCalculation | None = None
    idempotency_key: str = Field(min_length=1,max_length=120)
    recorded_at: datetime | None = None
    @model_validator(mode='after')
    def check_feedback(self):
        if self.recorded_at and self.recorded_at.tzinfo is None:raise ValueError('反馈时间需包含时区。')
        if self.outcome=='skipped' and self.actual_minutes not in (None,0):raise ValueError('已有活动分钟时请使用部分完成或完成。')
        return self


class PersonalChatInput(InputModel):
    message: str = Field(min_length=1,max_length=4000)
    session_id: str | None = Field(default=None,max_length=100)
    goal_id: str | None = Field(default=None,max_length=100)
    mode: Literal['demo','live'] = 'demo'
    timezone_name: str = 'Asia/Shanghai'
    city: str = Field(default='',max_length=100)
    use_external: bool = False


class PersonalIntent(InputModel):
    intent: Literal['chat','status','plan','event','review','feedback','task','goal','clarify']
    domains: list[Literal['activity','diet','sleep']] = Field(default_factory=list,max_length=3)
    task_id: str = Field(default='',max_length=100)
    outcome: Literal['completed','partial','skipped'] | None = None
    question: str = Field(default='',max_length=500)
    reason: str = Field(default='',max_length=500)
    task_draft: TaskInput | None = None
    goal_draft: GoalInput | None = None


class JobInput(InputModel):
    kind: Literal['event_replan','review','environment_check','checkin','calendar_sync','consolidate']
    run_at: datetime | None = None
    goal_id: str | None = Field(default=None,max_length=100)
    payload: dict = Field(default_factory=dict)
    idempotency_key: str = Field(min_length=1,max_length=150)


class ScheduleInput(InputModel):
    title: str = Field(min_length=1,max_length=120)
    kind: Literal['review','checkin','environment_check','calendar_sync','consolidate']
    frequency: Literal['daily','weekly','interval'] = 'daily'
    time: str = '21:30'
    weekdays: list[int] = Field(default_factory=list,max_length=7)
    interval_minutes: int = Field(default=60,ge=1,le=10080)
    timezone_name: str = 'Asia/Shanghai'
    goal_id: str | None = Field(default=None,max_length=100)
    enabled: bool = True
    @model_validator(mode='after')
    def validate_schedule(self):
        import re
        from zoneinfo import ZoneInfo
        ZoneInfo(self.timezone_name)
        if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',self.time):raise ValueError('时间需为HH:MM。')
        if any(n<0 or n>6 for n in self.weekdays):raise ValueError('星期为0至6。')
        if self.frequency=='weekly' and not self.weekdays:raise ValueError('每周任务需要星期。')
        return self
