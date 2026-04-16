from __future__ import annotations

from sqlmodel import Session, select

from app.core.timetable import (
    DAYS,
    LESSON_MODE_ONLINE,
    PAIR_NUMBERS,
    day_label,
    delivery_mode_label,
    online_slot_label,
    pair_label,
    pair_time_range,
    shift_label,
)
from app.core.week_scope import format_week_scope
from app.models import Group, OnlineSlot, Room, Schedule, ScheduleEntry, Subject, Teacher
from app.services.weekly_workload import WeeklyWorkloadService


def build_schedule_context(session: Session, schedule_id: int) -> dict:
    schedule = session.get(Schedule, schedule_id)
    if schedule is None:
        raise ValueError("Расписание не найдено.")
    entries = session.exec(select(ScheduleEntry).where(ScheduleEntry.schedule_id == schedule_id)).all()
    subjects = {subject.id: subject for subject in session.exec(select(Subject)).all()}
    teachers = {teacher.id: teacher for teacher in session.exec(select(Teacher)).all()}
    rooms = {room.id: room for room in session.exec(select(Room)).all()}
    groups = {group.id: group for group in session.exec(select(Group)).all()}
    online_slots = {slot.id or 0: slot for slot in session.exec(select(OnlineSlot)).all()}
    balance_rows = WeeklyWorkloadService.teacher_balance_report(session)
    unresolved_rows = WeeklyWorkloadService.unresolved_rows(session, semester=schedule.semester)

    def online_label(slot_number: int | None) -> str:
        slot = slot_number or 1
        if slot in online_slots:
            return online_slots[slot].label
        return online_slot_label(slot)

    def teacher_name(entry: ScheduleEntry) -> str:
        return teachers[entry.teacher_id].editable_name or teachers[entry.teacher_id].full_name

    def room_name(entry: ScheduleEntry) -> str:
        return rooms[entry.room_id].code if entry.room_id and entry.room_id in rooms else "Без аудитории"

    def weeks_text(entry: ScheduleEntry) -> str:
        return format_week_scope(entry.week_scope)

    def regular_text(entry: ScheduleEntry) -> str:
        return (
            f"{subjects[entry.subject_id].name}\n"
            f"{teacher_name(entry)}\n"
            f"{delivery_mode_label(entry.delivery_mode)}\n"
            f"{room_name(entry)}\n"
            f"Недели: {weeks_text(entry)}"
        )

    def online_row(entry: ScheduleEntry) -> dict[str, str]:
        return {
            "day": day_label(entry.day_of_week),
            "online_slot": online_label(entry.online_slot_number),
            "subject": subjects[entry.subject_id].name,
            "teacher": teacher_name(entry),
            "format": "Онлайн",
            "weeks": weeks_text(entry),
        }

    regular_entries = [entry for entry in entries if entry.lesson_mode != LESSON_MODE_ONLINE]
    online_entries = [entry for entry in entries if entry.lesson_mode == LESSON_MODE_ONLINE]

    group_grids: dict[str, dict[tuple[int, int], str]] = {}
    teacher_grids: dict[str, dict[tuple[int, int], str]] = {}
    group_online_rows: dict[str, list[dict[str, str]]] = {}
    teacher_online_rows: dict[str, list[dict[str, str]]] = {}

    for group in groups.values():
        current_entries = [entry for entry in regular_entries if entry.group_id == group.id]
        grid: dict[tuple[int, int], str] = {}
        for day_of_week in DAYS:
            for pair_number in PAIR_NUMBERS:
                items = [
                    regular_text(entry)
                    for entry in sorted(current_entries, key=lambda item: item.subject_id)
                    if entry.day_of_week == day_of_week and entry.pair_number == pair_number
                ]
                grid[(day_of_week, pair_number)] = "\n\n".join(items)
        group_grids[group.code] = grid
        group_online_rows[group.code] = [
            online_row(entry)
            for entry in sorted(
                [entry for entry in online_entries if entry.group_id == group.id],
                key=lambda item: (item.day_of_week, item.online_slot_number or 0, item.subject_id),
            )
        ]

    for teacher in teachers.values():
        current_entries = [entry for entry in regular_entries if entry.teacher_id == teacher.id]
        grid: dict[tuple[int, int], str] = {}
        for day_of_week in DAYS:
            for pair_number in PAIR_NUMBERS:
                items = [
                    (
                        f"{groups[entry.group_id].code}\n"
                        f"{subjects[entry.subject_id].name}\n"
                        f"{delivery_mode_label(entry.delivery_mode)}\n"
                        f"{room_name(entry)}\n"
                        f"{pair_label(entry.pair_number)} {pair_time_range(entry.pair_number)}"
                    )
                    for entry in sorted(current_entries, key=lambda item: item.group_id)
                    if entry.day_of_week == day_of_week and entry.pair_number == pair_number
                ]
                grid[(day_of_week, pair_number)] = "\n\n".join(items)
        teacher_grids[teacher.short_name] = grid
        teacher_online_rows[teacher.short_name] = [
            {
                **online_row(entry),
                "group": groups[entry.group_id].code,
            }
            for entry in sorted(
                [entry for entry in online_entries if entry.teacher_id == teacher.id],
                key=lambda item: (item.day_of_week, item.online_slot_number or 0, item.group_id),
            )
        ]

    return {
        "schedule": schedule,
        "group_grids": group_grids,
        "teacher_grids": teacher_grids,
        "group_online_rows": group_online_rows,
        "teacher_online_rows": teacher_online_rows,
        "teacher_balance_rows": balance_rows,
        "unresolved_weekly_rows": unresolved_rows,
        "unresolved_weekly_rows_report": [
            {
                "group": groups[row.group_id].code if row.group_id in groups else str(row.group_id),
                "semester": row.semester,
                "subject": subjects[row.subject_id].name if row.subject_id in subjects else str(row.subject_id),
                "subgroup": row.subgroup_code or "",
                "assignment_state": row.assignment_state,
                "teacher_names": row.raw_teacher_names or "",
            }
            for row in unresolved_rows
        ],
        "day_labels": {day: day_label(day) for day in DAYS},
        "pair_headers": {
            pair: f"{pair_label(pair)}\n{pair_time_range(pair)}\n{shift_label('morning' if pair <= 3 else 'afternoon')}"
            for pair in PAIR_NUMBERS
        },
    }
