"""Small role outputs and graph state; keep the coordination contract explicit."""
import operator
from typing import Annotated, Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field


class Output(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Analysis(Output):
    route: Literal["plan", "clarify", "professional_support"]
    reason: str


class RestaurantChoice(Output):
    candidate_id: str = Field(max_length=100)
    meal: Literal['breakfast', 'lunch', 'dinner'] = 'dinner'
    reason: str = Field(max_length=500)
    ordering_tips: list[str] = Field(default_factory=list, max_length=4)


class MealChoice(Output):
    template_id: str = Field(min_length=1,max_length=100)
    weekday: Literal[0,1,2,3,4,5,6]
    meal: Literal['breakfast','lunch','dinner']
    servings: float = Field(default=1,ge=0.25,le=3)
    reason: str = Field(default='',max_length=300)


class DietAdvice(Output):
    suggestions: list[str]
    rationale: str
    restaurant_choices: list[RestaurantChoice] = Field(default_factory=list, max_length=3)
    meal_choices: list[MealChoice] = Field(default_factory=list, max_length=21)


class VenueChoice(Output):
    weekday: Literal[0, 1, 2, 3, 4, 5, 6]
    candidate_id: str = Field(default='', max_length=100)
    activity: Literal['walk', 'jog', 'indoor'] = 'walk'
    reason: str = Field(max_length=500)


class ActivityAdvice(Output):
    days: int = Field(ge=0, le=7)
    minutes: int = Field(ge=0, le=1440)
    suggestions: list[str]
    venue_choices: list[VenueChoice] = Field(default_factory=list, max_length=7)


class SleepAdvice(Output):
    suggestions: list[str]
    rationale: str


class Review(Output):
    evidence_status: Literal["limited", "available"]
    observations: list[str]
    adjustment: Literal["maintain", "reduce_activity", "collect_more"] = "collect_more"
    recommendations: list[str] = Field(default_factory=list)


class Plan(Output):
    meal_schedule: list[dict] = Field(default_factory=list)
    sleep_suggestions: list[str] = Field(default_factory=list)
    sleep_schedule: list[dict] = Field(default_factory=list)
    quantification: dict = Field(default_factory=dict)
    schedule_conflicts: list[str] = Field(default_factory=list)
    diet_suggestions: list[str]
    activity_days: int = Field(ge=0, le=7)
    minutes_per_session: int = Field(ge=0, le=1440)
    activity_suggestions: list[str]
    rationale: str
    changes: list[str]
    dining_recommendations: list[dict] = Field(default_factory=list)
    dining_note: str = ''
    daily_schedule: list[dict] = Field(default_factory=list)
    planning_context: dict = Field(default_factory=dict)


class TemporaryConstraints(Output):
    available_activity_days: int | None = Field(default=None, ge=0, le=7)
    minutes_per_session: int | None = Field(default=None, ge=0, le=1440)
    food_preferences: list[str] | None = None
    activity_constraints: list[str] | None = None


from app.multi_agent.memory_contracts import MemoryProposal, HistoryQuery


class IntentPlan(Output):
    history_query: HistoryQuery = Field(default_factory=HistoryQuery)
    memory_updates: list[MemoryProposal] = Field(default_factory=list, max_length=3)
    dining_mode: Literal['unspecified', 'home', 'eat_out'] = 'unspecified'
    restaurant_categories: list[Literal['chinese', 'light', 'noodles', 'vegetarian', 'restaurant']] = Field(default_factory=lambda: ['chinese', 'light'], max_length=3)
    meal_budget_yuan: int | None = Field(default=None, ge=1, le=10000)

    geo_categories: list[Literal['park', 'trail', 'sports', 'fitness']] = Field(default_factory=lambda: ['park', 'trail', 'sports'], max_length=3)
    search_radius_m: int = Field(default=2000, ge=300, le=5000)
    available_weekdays: list[Literal[0, 1, 2, 3, 4, 5, 6]] = Field(default_factory=list)
    preferred_time: str = Field(default='', max_length=5)
    place_name: str = Field(default='', max_length=100)
    start_place: str = Field(default='', max_length=100)
    activity_type: Literal['walk', 'jog', 'indoor'] = 'walk'
    wants_reminder: bool = False
    intent: Literal['plan', 'adjust', 'review', 'lookup', 'clarify']
    sections: list[Literal['diet', 'activity', 'review', 'sleep']] = Field(default_factory=list)
    temporary: TemporaryConstraints = Field(default_factory=TemporaryConstraints)
    city: str = Field(default='', max_length=100)
    target_date: str = Field(default='', max_length=10)
    external_tools: list[Literal['weather', 'venues', 'search', 'ingredients']] = Field(default_factory=list)
    public_query: str = Field(default='', max_length=160)
    question: str = Field(default='', max_length=400)
    reason: str = Field(min_length=1, max_length=1000)


class AdviceAnswer(Output):
    text: str = Field(min_length=1)
    evidence_ids: list[str] = Field(default_factory=list)


class GraphState(TypedDict, total=False):
    quantitative_records: list[dict]
    life_events: list[dict]
    automation_policy: dict
    retrieved_evidence: Annotated[list[dict], operator.add]
    base_profile: dict
    memory_context: list[dict]
    history_evidence: list[dict]
    source_message_id: str
    local_now: str
    location: dict | None
    prefetched_environment: dict | None
    user_message: str
    dialogue: list[dict]
    intent_plan: dict
    effective_profile: dict
    selected_workers: list[str]
    clarification_round: int
    clarification_question: str
    task_city: str
    use_external: bool
    external_evidence: list[dict]
    answer: dict
    mode: str
    as_of: str
    task_kind: str
    previous_plan: dict | None
    recent_records: list[dict]
    summary: dict
    plan_diff: list[dict]
    profile: dict
    records: list[dict]
    analysis: dict
    sleep: dict
    diet: dict
    activity: dict
    review: dict
    proposal: dict
    active_plan: dict
    errors: list[str]
    coordination_round: int
    status: str
    # Parallel branches append independently; do not overwrite each other.
    events: Annotated[list[str], operator.add]
