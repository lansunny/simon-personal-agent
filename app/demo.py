"""Synthetic input validation demonstration; no network, storage or model call."""

import sys

from pydantic import ValidationError

from app.schemas import DailyRecordInput, UserProfileInput


def main() -> None:
    profile = UserProfileInput(
        age=25,
        height_cm=170,
        baseline_weight_kg=70,
        baseline_date="2026-09-28",
        goal="build_habits",
        available_activity_days=3,
        minutes_per_session=30,
        food_preferences=["普通家常餐"],
    )
    missing_activity = DailyRecordInput(record_date="2026-09-29", weight_kg=70.1)
    recorded_zero = DailyRecordInput(record_date="2026-09-30", activity_minutes=0)

    print("SYNTHETIC DEMO — no health plan is generated")
    print(profile.model_dump_json(indent=2))
    print("Missing activity:", missing_activity.model_dump_json())
    print("Recorded zero:", recorded_zero.model_dump_json())
    try:
        DailyRecordInput(record_date="2026-09-30", weight_kg=-1)
    except ValidationError as exc:
        print("Rejected invalid input:", [error["loc"] for error in exc.errors()])


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
