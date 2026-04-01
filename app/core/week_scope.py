from __future__ import annotations

from math import floor


def encode_week_scope(weeks: list[int]) -> str:
    ordered = sorted(set(weeks))
    return "weeks:" + ",".join(str(week) for week in ordered)


def decode_week_scope(value: str) -> set[int]:
    if not value:
        return set()
    if value.startswith("weeks:"):
        raw = value.split(":", 1)[1]
        return {int(part) for part in raw.split(",") if part}
    if value == "all":
        return set()
    return set()


def scopes_overlap(left: str, right: str) -> bool:
    left_weeks = decode_week_scope(left)
    right_weeks = decode_week_scope(right)
    if not left_weeks or not right_weeks:
        return True
    return bool(left_weeks & right_weeks)


def spread_weeks(weeks: list[int], required: int) -> list[int]:
    if required <= 0:
        return []
    if required >= len(weeks):
        return list(weeks)
    if required == 1:
        return [weeks[0]]
    step = (len(weeks) - 1) / (required - 1)
    picks: list[int] = []
    for index in range(required):
        picks.append(weeks[floor(index * step)])
    return sorted(set(picks))
