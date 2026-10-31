"""长期记忆与历史查询白名单契约；模型只能提出候选，不能直接写数据库。"""
from datetime import date
from typing import Literal
import re
from pydantic import BaseModel, ConfigDict, Field, model_validator


class MemoryProposal(BaseModel):
    model_config = ConfigDict(extra='forbid')
    key: Literal['diet.preferences', 'activity.constraints', 'activity.minutes', 'activity.days', 'activity.type', 'activity.time', 'sleep.bedtime','sleep.wake_time','sleep.target_hours']
    value: str | int | float | list[str]
    scope: Literal['long_term', 'temporary'] = 'long_term'
    valid_from: str
    valid_until: str | None = None
    reason: str = Field(min_length=1, max_length=300)

    @model_validator(mode='after')
    def check(self):
        start = date.fromisoformat(self.valid_from)
        if self.scope == 'temporary' and not self.valid_until:
            raise ValueError('临时记忆必须指定结束日期')
        if self.scope == 'long_term' and self.valid_until:
            raise ValueError('长期记忆不设置结束日期')
        if self.valid_until and date.fromisoformat(self.valid_until) < start:
            raise ValueError('记忆结束日期早于开始日期')
        if self.key in ('diet.preferences', 'activity.constraints'):
            if not isinstance(self.value, list) or not 1 <= len(self.value) <= 8 or any(not isinstance(v, str) or not v.strip() or len(v) > 100 for v in self.value):
                raise ValueError('偏好和限制必须为最多八条非空短文本')
        elif self.key in ('activity.minutes', 'activity.days'):
            upper = 1440 if self.key == 'activity.minutes' else 7
            if type(self.value) is not int or not 0 <= self.value <= upper:
                raise ValueError('活动数值超出允许范围')
        elif self.key == 'activity.type' and self.value not in ('walk', 'jog', 'indoor'):
            raise ValueError('运动类型不在白名单')
        elif self.key=='sleep.target_hours' and (type(self.value) not in (int,float) or not 4<=self.value<=12):
            raise ValueError('个人睡眠目标需在4至12小时内，不代表通用推荐')
        elif self.key in ('activity.time','sleep.bedtime','sleep.wake_time') and (not isinstance(self.value, str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', self.value)):
            raise ValueError('开始时间须为 HH:MM')
        return self


class HistoryQuery(BaseModel):
    model_config = ConfigDict(extra='forbid')
    kind: Literal['none', 'records', 'messages', 'plans', 'memories'] = 'none'
    start_date: str = ''
    end_date: str = ''
    query: str = Field(default='', max_length=100)
    version: int | None = Field(default=None, ge=1)

    @model_validator(mode='after')
    def check(self):
        for value in (self.start_date, self.end_date):
            if value:
                date.fromisoformat(value)
        if self.start_date and self.end_date:
            days = (date.fromisoformat(self.end_date) - date.fromisoformat(self.start_date)).days
            if not 0 <= days <= 366:
                raise ValueError('单次历史查询最多覆盖367天，结束日期不能早于开始日期')
        return self


class AgentHistoryQuery(HistoryQuery):
    """角色内补查参数；实际日期默认值和范围再由仓储验证。"""
    kind: Literal['records', 'messages', 'plans'] = 'records'
    topic: Literal['all', 'diet', 'activity', 'sleep'] = 'all'
    limit: int = Field(default=5, ge=1, le=8)
    cursor: str = Field(default='', max_length=64)
    include_revisions: bool = False


class AgentMemoryQuery(BaseModel):
    model_config = ConfigDict(extra='forbid')
    topic: Literal['all', 'diet', 'activity', 'sleep'] = 'all'
    include_history: bool = False
    limit: int = Field(default=5, ge=1, le=8)


class AgentSourceQuery(BaseModel):
    model_config = ConfigDict(extra='forbid')
    message_id: str = Field(min_length=1, max_length=64)
    offset: int = Field(default=0, ge=0, le=100000)
    max_chars: int = Field(default=1200, ge=100, le=1200)
