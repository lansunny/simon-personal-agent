"""Lesson 1: input contracts, not medical recommendations or database models."""

from datetime import date
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator


class InputModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        allow_inf_nan=False,
    )


class WeightGoal(str, Enum):
    MAINTAIN = "maintain"
    GRADUAL_REDUCTION = "gradual_reduction"
    BUILD_HABITS = "build_habits"


class UserProfileInput(InputModel):
    """Confirmed profile input; authentication supplies user ID separately."""

    age: int = Field(ge=18, strict=True, description="Adult-only MVP scope")
    height_cm: float = Field(gt=0, description="Height in centimetres")
    baseline_weight_kg: float = Field(gt=0, description="Self-reported baseline, kg")
    baseline_date: date
    timezone_name: str = 'Asia/Shanghai'
    sleep_bedtime: str = "23:00"
    sleep_wake_time: str = "07:00"
    sleep_target_hours: float = Field(default=8, ge=4, le=12)

    @field_validator("sleep_bedtime", "sleep_wake_time")
    @classmethod
    def validate_clock(cls, value):
        import re
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
            raise ValueError("时间使用HH:MM")
        return value

    goal: WeightGoal
    available_activity_days: int = Field(ge=0, le=7, strict=True)
    minutes_per_session: int = Field(ge=0, le=1440, strict=True)
    food_preferences: list[str] = Field(default_factory=list)
    activity_constraints: list[str] = Field(default_factory=list)
    # Unknown must be clarified before plan generation. This is not clinical triage.
    needs_professional_support: bool | None = Field(default=None, strict=True)


class DailyRecordInput(InputModel):
    """A day can be partially recorded; None is different from an observed zero."""

    record_date: date
    weight_kg: float | None = Field(default=None, gt=0)
    activity_minutes: int | None = Field(default=None, ge=0, le=1440, strict=True)
    sleep_hours: float | None = Field(default=None, ge=0, le=24)
    sleep_quality: int | None = Field(default=None, ge=1, le=5)
    fatigue: int | None = Field(default=None, ge=1, le=5)
    meal_notes: str | None = Field(default=None, min_length=1, max_length=2000)
    feedback: str | None = Field(default=None, min_length=1, max_length=2000)
