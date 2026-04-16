from __future__ import annotations

from typing import Any

from app.ui.i18n import t


SHIFT_MORNING = "morning"
SHIFT_AFTERNOON = "afternoon"
SHIFT_ONLINE = "online"

DELIVERY_OFFLINE = "offline"
DELIVERY_ONLINE = "online"
DELIVERY_HYBRID = "hybrid"

LESSON_MODE_REGULAR = "regular"
LESSON_MODE_ONLINE = "online"
SLOT_CATEGORY_REGULAR = "regular"
SLOT_CATEGORY_ONLINE_EXTRA = "online_extra"

DAYS = {
    1: "monday",
    2: "tuesday",
    3: "wednesday",
    4: "thursday",
    5: "friday",
}

PAIR_NUMBERS = (1, 2, 3, 4, 5, 6)
MORNING_PAIR_NUMBERS = (1, 2, 3)
AFTERNOON_PAIR_NUMBERS = (4, 5, 6)

PAIR_DEFINITIONS: dict[int, dict[str, str]] = {
    1: {"shift": SHIFT_MORNING, "start": "08:00", "end": "09:20"},
    2: {"shift": SHIFT_MORNING, "start": "09:40", "end": "11:00"},
    3: {"shift": SHIFT_MORNING, "start": "11:10", "end": "12:30"},
    4: {"shift": SHIFT_AFTERNOON, "start": "13:30", "end": "14:50"},
    5: {"shift": SHIFT_AFTERNOON, "start": "15:10", "end": "16:30"},
    6: {"shift": SHIFT_AFTERNOON, "start": "16:40", "end": "18:00"},
}

SHIFT_PAIR_NUMBERS = {
    SHIFT_MORNING: MORNING_PAIR_NUMBERS,
    SHIFT_AFTERNOON: AFTERNOON_PAIR_NUMBERS,
}

DELIVERY_MODES = (DELIVERY_OFFLINE, DELIVERY_ONLINE, DELIVERY_HYBRID)
SHIFT_VALUES = (SHIFT_MORNING, SHIFT_AFTERNOON)
LESSON_MODES = (LESSON_MODE_REGULAR, LESSON_MODE_ONLINE)
ONLINE_ALLOWED_DAYS = (3, 4, 5)
ONLINE_SLOT_DEFINITIONS: dict[int, dict[str, int | str]] = {
    1: {"day_of_week": 3, "label": "Онлайн-слот 1", "start": "18:10", "end": "19:30"},
    2: {"day_of_week": 4, "label": "Онлайн-слот 2", "start": "18:10", "end": "19:30"},
    3: {"day_of_week": 5, "label": "Онлайн-слот 3", "start": "18:10", "end": "19:30"},
}


def day_label(day_of_week: int, lang: str = "ru") -> str:
    return t(f"day.{DAYS[day_of_week]}", lang=lang)


def shift_label(shift: str, lang: str = "ru") -> str:
    return t(f"shift.{shift}", lang=lang)


def delivery_mode_label(mode: str, lang: str = "ru") -> str:
    return t(f"delivery.{mode}", lang=lang)


def pair_label(pair_number: int, lang: str = "ru") -> str:
    return t("pair.label", lang=lang, pair=pair_number)


def pair_time_range(pair_number: int, lang: str = "ru") -> str:
    data = PAIR_DEFINITIONS[pair_number]
    return t("pair.time_range", lang=lang, start=data["start"], end=data["end"])


def pair_shift(pair_number: int) -> str:
    return PAIR_DEFINITIONS[pair_number]["shift"]


def pair_start(pair_number: int) -> str:
    return PAIR_DEFINITIONS[pair_number]["start"]


def pair_end(pair_number: int) -> str:
    return PAIR_DEFINITIONS[pair_number]["end"]


def allowed_pairs_for_shift(shift: str) -> tuple[int, ...]:
    return SHIFT_PAIR_NUMBERS.get(shift, PAIR_NUMBERS)


def pair_allowed_for_shift(shift: str, pair_number: int) -> bool:
    return pair_number in allowed_pairs_for_shift(shift)


def visible_pairs_for_view(view_mode: str, group_shift: str | None, shift_filter: str) -> tuple[int, ...]:
    if view_mode == "group" and group_shift:
        return allowed_pairs_for_shift(group_shift)
    if shift_filter in SHIFT_VALUES:
        return allowed_pairs_for_shift(shift_filter)
    return PAIR_NUMBERS


def online_slot_numbers() -> tuple[int, ...]:
    return tuple(ONLINE_SLOT_DEFINITIONS.keys())


def online_slot_day(slot_number: int) -> int:
    return int(ONLINE_SLOT_DEFINITIONS[slot_number]["day_of_week"])


def online_slot_label(slot_number: int, lang: str = "ru", custom_label: str | None = None) -> str:
    if custom_label:
        return custom_label
    return t("online_slot.label", lang=lang, slot=slot_number)


def online_slot_start(slot_number: int) -> str:
    return str(ONLINE_SLOT_DEFINITIONS[slot_number].get("start", ""))


def online_slot_end(slot_number: int) -> str:
    return str(ONLINE_SLOT_DEFINITIONS[slot_number].get("end", ""))


def online_day_allowed(day_of_week: int) -> bool:
    return day_of_week in ONLINE_ALLOWED_DAYS


def online_slot_matches_day(slot_number: int, day_of_week: int) -> bool:
    return slot_number in ONLINE_SLOT_DEFINITIONS and online_slot_day(slot_number) == day_of_week


def online_slot_for_day(day_of_week: int) -> int | None:
    for slot_number, payload in ONLINE_SLOT_DEFINITIONS.items():
        if int(payload["day_of_week"]) == day_of_week:
            return slot_number
    return None


def slot_text(entry: Any, lang: str = "ru", custom_label: str | None = None) -> str:
    if getattr(entry, "lesson_mode", LESSON_MODE_REGULAR) == LESSON_MODE_ONLINE:
        return online_slot_label(getattr(entry, "online_slot_number", 0) or 1, lang=lang, custom_label=custom_label)
    return f"{pair_label(entry.pair_number, lang=lang)} {pair_time_range(entry.pair_number, lang=lang)}"


def room_required(delivery_mode: str) -> bool:
    return delivery_mode != DELIVERY_ONLINE


def apply_timeslot_to_entry(entry: Any) -> None:
    lesson_mode = getattr(entry, "lesson_mode", LESSON_MODE_REGULAR)
    if lesson_mode == LESSON_MODE_ONLINE:
        entry.slot_category = SLOT_CATEGORY_ONLINE_EXTRA
        entry.delivery_mode = DELIVERY_ONLINE
        entry.room_id = None
        if not getattr(entry, "online_slot_number", None):
            slot_number = online_slot_for_day(entry.day_of_week)
            entry.online_slot_number = slot_number or 1
        entry.pair_number = 0
        entry.shift = SHIFT_ONLINE
        entry.start_time = online_slot_start(entry.online_slot_number)
        entry.end_time = online_slot_end(entry.online_slot_number)
        return
    entry.lesson_mode = LESSON_MODE_REGULAR
    entry.slot_category = SLOT_CATEGORY_REGULAR
    entry.online_slot_number = None
    if getattr(entry, "pair_number", 0) not in PAIR_DEFINITIONS:
        entry.shift = ""
        entry.start_time = ""
        entry.end_time = ""
        return
    entry.shift = pair_shift(entry.pair_number)
    entry.start_time = pair_start(entry.pair_number)
    entry.end_time = pair_end(entry.pair_number)


def time_to_minutes(value: str) -> int:
    if not value or ":" not in value:
        return -1
    hour, minute = value.split(":", 1)
    return int(hour) * 60 + int(minute)


def intervals_overlap(start_a: str, end_a: str, start_b: str, end_b: str) -> bool:
    left_start = time_to_minutes(start_a)
    left_end = time_to_minutes(end_a)
    right_start = time_to_minutes(start_b)
    right_end = time_to_minutes(end_b)
    if min(left_start, left_end, right_start, right_end) < 0:
        return False
    return left_start < right_end and right_start < left_end
