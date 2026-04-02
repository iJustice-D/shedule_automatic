from __future__ import annotations

from dataclasses import dataclass
from math import ceil
from typing import Sequence

from app.core.week_scope import spread_weeks


@dataclass(slots=True)
class WeeklyLoadPlan:
    total_hours: int
    total_pairs_needed: int
    study_weeks_available: int
    target_pairs_per_week: float
    base_pairs_per_week: int
    remainder_pairs: int
    remainder_hours: int
    extra_weeks: list[int]
    uneven_distribution_strategy: str


def academic_hours_to_pairs(total_hours: int) -> int:
    hours = max(int(total_hours or 0), 0)
    return ceil(hours / 2) if hours else 0


def build_weekly_load_plan(total_hours: int, study_weeks: Sequence[int] | int) -> WeeklyLoadPlan:
    hours = max(int(total_hours or 0), 0)
    if isinstance(study_weeks, int):
        week_numbers = list(range(1, max(study_weeks, 0) + 1))
    else:
        week_numbers = sorted({int(week) for week in study_weeks})

    total_pairs = academic_hours_to_pairs(hours)
    remainder_hours = hours % 2
    week_count = len(week_numbers)
    if week_count == 0:
        strategy = "Нет доступных учебных недель для распределения нагрузки."
        if remainder_hours:
            strategy += " Последняя единица нагрузки содержит неполную пару на 1 академический час."
        return WeeklyLoadPlan(
            total_hours=hours,
            total_pairs_needed=total_pairs,
            study_weeks_available=0,
            target_pairs_per_week=0.0,
            base_pairs_per_week=0,
            remainder_pairs=0,
            remainder_hours=remainder_hours,
            extra_weeks=[],
            uneven_distribution_strategy=strategy,
        )

    base_pairs = total_pairs // week_count
    remainder_pairs = total_pairs % week_count
    extra_weeks = spread_weeks(week_numbers, remainder_pairs) if remainder_pairs else []
    target_pairs = round(total_pairs / week_count, 2)

    if remainder_pairs:
        extra_text = ", ".join(str(week) for week in extra_weeks)
        strategy = (
            f"Базовая нагрузка: {base_pairs} пар(ы) в учебную неделю, "
            f"дополнительно +1 пара в недели: {extra_text}."
        )
    else:
        strategy = f"Равномерно по {base_pairs} пар(ы) в каждую учебную неделю."
    if remainder_hours:
        strategy += " Есть остаток 1 академический час, поэтому итоговая нагрузка округлена вверх до полной пары."

    return WeeklyLoadPlan(
        total_hours=hours,
        total_pairs_needed=total_pairs,
        study_weeks_available=week_count,
        target_pairs_per_week=target_pairs,
        base_pairs_per_week=base_pairs,
        remainder_pairs=remainder_pairs,
        remainder_hours=remainder_hours,
        extra_weeks=extra_weeks,
        uneven_distribution_strategy=strategy,
    )
