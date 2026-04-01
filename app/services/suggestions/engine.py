from __future__ import annotations

import json
from collections import defaultdict

from sqlmodel import Session, delete, select

from app.core.timetable import (
    DAYS,
    DELIVERY_OFFLINE,
    DELIVERY_ONLINE,
    LESSON_MODE_ONLINE,
    LESSON_MODE_REGULAR,
    ONLINE_ALLOWED_DAYS,
    allowed_pairs_for_shift,
    day_label,
    online_slot_day,
    online_slot_for_day,
    online_slot_label,
    online_slot_numbers,
    pair_label,
    room_required,
)
from app.core.week_scope import scopes_overlap
from app.models import Conflict, Group, GroupSubjectTeacher, Room, Schedule, ScheduleEntry, Subject, Suggestion, TeacherSubject
from app.services.online_policy import OnlinePolicyService


class SuggestionEngine:
    def __init__(self) -> None:
        self.online_policy_service = OnlinePolicyService()

    def refresh(self, session: Session, schedule: Schedule) -> list[Suggestion]:
        session.exec(
            delete(Suggestion).where(
                Suggestion.conflict_id.in_(
                    select(Conflict.id).where(Conflict.schedule_id == schedule.id)
                )
            )
        )
        conflicts = session.exec(select(Conflict).where(Conflict.schedule_id == schedule.id)).all()
        suggestions: list[Suggestion] = []
        for conflict in conflicts:
            suggestions.extend(self._for_conflict(session, schedule, conflict))
        for suggestion in suggestions:
            session.add(suggestion)
        session.commit()
        return suggestions

    def _for_conflict(self, session: Session, schedule: Schedule, conflict: Conflict) -> list[Suggestion]:
        related_entry_ids = [int(item) for item in conflict.related_entry_ids.split(",") if item]
        entries = [
            session.get(ScheduleEntry, entry_id)
            for entry_id in related_entry_ids
            if session.get(ScheduleEntry, entry_id)
        ]
        primary = entries[0] if entries else None
        suggestions: list[Suggestion] = []

        if primary and conflict.type in {
            "group_double_booked",
            "teacher_double_booked",
            "room_double_booked",
            "blocked_period",
            "too_many_lessons_in_day",
            "same_subject_stack",
            "too_many_online_lessons_in_day",
            "shift_violation",
            "online_slot_violation",
            "online_day_violation",
            "regular_in_online_slot",
        }:
            candidate = self._find_nearest_free_slot(session, schedule, primary)
            if candidate:
                day_of_week, pair_number, online_slot_number = candidate
                if primary.lesson_mode == LESSON_MODE_ONLINE:
                    message = f"Перенести занятие на {day_label(day_of_week)}, {online_slot_label(online_slot_number or 1)}."
                    payload = {"day_of_week": day_of_week, "pair_number": 0, "online_slot_number": online_slot_number, "lesson_mode": LESSON_MODE_ONLINE}
                else:
                    message = f"Перенести занятие на {day_label(day_of_week)}, {pair_label(pair_number)}."
                    payload = {"day_of_week": day_of_week, "pair_number": pair_number, "lesson_mode": LESSON_MODE_REGULAR}
                suggestions.append(
                    Suggestion(
                        conflict_id=conflict.id or 0,
                        action_type="move_lesson",
                        rank=1,
                        message=message,
                        payload_json=json.dumps(payload),
                    )
                )
            swap = self._find_swap_candidate(session, schedule, primary)
            if swap:
                suggestions.append(
                    Suggestion(
                        conflict_id=conflict.id or 0,
                        action_type="swap_lessons",
                        rank=2,
                        message=f"Поменять местами с записью #{swap.id}, чтобы сохранить компактность расписания.",
                        payload_json=json.dumps({"swap_entry_id": swap.id}),
                    )
                )
            lower_day = self._find_lower_load_day(session, schedule, primary)
            if lower_day:
                suggestions.append(
                    Suggestion(
                        conflict_id=conflict.id or 0,
                        action_type="move_to_lower_load_day",
                        rank=3,
                        message=f"Перенести занятие на {day_label(lower_day)}, где нагрузка ниже.",
                        payload_json=json.dumps({"day_of_week": lower_day}),
                    )
                )

        if primary and conflict.type == "teacher_not_allowed":
            allowed_teacher = self._find_alternate_teacher(session, schedule, primary)
            if allowed_teacher:
                suggestions.append(
                    Suggestion(
                        conflict_id=conflict.id or 0,
                        action_type="change_teacher",
                        rank=1,
                        message=f"Назначить другого допустимого преподавателя: #{allowed_teacher}.",
                        payload_json=json.dumps({"teacher_id": allowed_teacher}),
                    )
                )

        if primary and conflict.type == "room_required":
            room_id = self._find_free_room(session, schedule, primary)
            if room_id:
                suggestions.append(
                    Suggestion(
                        conflict_id=conflict.id or 0,
                        action_type="assign_room",
                        rank=1,
                        message=f"Назначить свободную аудиторию #{room_id}.",
                        payload_json=json.dumps({"room_id": room_id}),
                    )
                )

        if primary and conflict.type == "online_not_allowed":
            suggestions.append(
                Suggestion(
                    conflict_id=conflict.id or 0,
                    action_type="switch_to_offline",
                    rank=1,
                    message="Перевести занятие в очный формат.",
                    payload_json=json.dumps({"delivery_mode": DELIVERY_OFFLINE, "lesson_mode": LESSON_MODE_REGULAR, "pair_number": 1, "online_slot_number": None}),
                )
            )

        if conflict.type == "online_target_not_met":
            suggestions.extend(self._online_target_suggestions(session, schedule, conflict))

        if conflict.type == "unscheduled_load":
            suggestions.append(
                Suggestion(
                    conflict_id=conflict.id or 0,
                    action_type="review_load",
                    rank=1,
                    message="Уточнить учебную нагрузку или распределить часть пар по отдельным неделям вручную.",
                    payload_json=conflict.details_json or "{}",
                )
            )

        return suggestions[:3]

    def _online_target_suggestions(self, session: Session, schedule: Schedule, conflict: Conflict) -> list[Suggestion]:
        details = json.loads(conflict.details_json or "{}")
        group_id = details.get("group_id")
        if not group_id:
            return []
        group = session.get(Group, group_id)
        if group is None:
            return []
        entries = session.exec(
            select(ScheduleEntry).where(
                ScheduleEntry.schedule_id == schedule.id,
                ScheduleEntry.group_id == group_id,
            )
        ).all()
        subjects = {subject.id: subject for subject in session.exec(select(Subject)).all()}
        convertible = [
            entry
            for entry in entries
            if entry.lesson_mode != LESSON_MODE_ONLINE
            and self.online_policy_service.is_subject_allowed_online(session, group, subjects[entry.subject_id])
        ]
        suggestions: list[Suggestion] = []
        if convertible:
            entry = sorted(convertible, key=lambda item: (item.day_of_week, item.pair_number))[0]
            slot_number = self._first_free_online_slot(entries, entry)
            suggestions.append(
                Suggestion(
                    conflict_id=conflict.id or 0,
                    action_type="convert_to_online",
                    rank=1,
                    message=f"Перевести занятие #{entry.id} в онлайн, чтобы приблизиться к недельной цели.",
                    payload_json=json.dumps(
                        {
                            "entry_id": entry.id,
                            "delivery_mode": DELIVERY_ONLINE,
                            "lesson_mode": LESSON_MODE_ONLINE,
                            "room_id": None,
                            "pair_number": 0,
                            "online_slot_number": slot_number,
                            "day_of_week": online_slot_day(slot_number),
                        }
                    ),
                )
            )
        online_entries = [entry for entry in entries if entry.lesson_mode == LESSON_MODE_ONLINE]
        if online_entries:
            by_day: dict[int, int] = defaultdict(int)
            for entry in online_entries:
                by_day[entry.day_of_week] += 1
            overloaded_days = [day for day, count in by_day.items() if count > 1]
            if overloaded_days:
                suggestions.append(
                    Suggestion(
                        conflict_id=conflict.id or 0,
                        action_type="rebalance_online",
                        rank=2,
                        message="Перераспределить онлайн-занятия по разным дням недели.",
                        payload_json=json.dumps({"overloaded_days": overloaded_days}),
                    )
                )
        return suggestions

    def _find_nearest_free_slot(
        self,
        session: Session,
        schedule: Schedule,
        entry: ScheduleEntry,
    ) -> tuple[int, int, int | None] | None:
        group = session.get(Group, entry.group_id)
        if group is None:
            return None
        entries = session.exec(
            select(ScheduleEntry).where(ScheduleEntry.schedule_id == schedule.id)
        ).all()
        candidates: list[tuple[int, int, int, int]] = []
        if entry.lesson_mode == LESSON_MODE_ONLINE:
            for slot_number in online_slot_numbers():
                day_of_week = online_slot_day(slot_number)
                if day_of_week == entry.day_of_week and slot_number == entry.online_slot_number:
                    continue
                if self._slot_has_conflict(entries, entry, day_of_week, 0, slot_number):
                    continue
                distance = abs(day_of_week - entry.day_of_week) * 10 + abs(slot_number - (entry.online_slot_number or 0))
                candidates.append((distance, day_of_week, 0, slot_number))
        else:
            for day_of_week in DAYS:
                for pair_number in allowed_pairs_for_shift(group.shift):
                    if day_of_week == entry.day_of_week and pair_number == entry.pair_number:
                        continue
                    if self._slot_has_conflict(entries, entry, day_of_week, pair_number, None):
                        continue
                    distance = abs(day_of_week - entry.day_of_week) * 10 + abs(pair_number - entry.pair_number)
                    candidates.append((distance, day_of_week, pair_number, 0))
        if not candidates:
            return None
        _, day_of_week, pair_number, slot_number = min(candidates)
        return day_of_week, pair_number, slot_number or None

    def _find_swap_candidate(self, session: Session, schedule: Schedule, entry: ScheduleEntry) -> ScheduleEntry | None:
        group = session.get(Group, entry.group_id)
        if group is None:
            return None
        entries = session.exec(
            select(ScheduleEntry).where(
                ScheduleEntry.schedule_id == schedule.id,
                ScheduleEntry.group_id == entry.group_id,
                ScheduleEntry.id != entry.id,
            )
        ).all()
        for other in sorted(entries, key=lambda item: (item.day_of_week, item.pair_number)):
            if other.locked or other.lesson_mode != entry.lesson_mode:
                continue
            if other.lesson_mode == LESSON_MODE_REGULAR and other.pair_number not in allowed_pairs_for_shift(group.shift):
                continue
            if self._slot_has_conflict(entries, entry, other.day_of_week, other.pair_number, other.online_slot_number, ignored_id=other.id):
                continue
            if self._slot_has_conflict(entries, other, entry.day_of_week, entry.pair_number, entry.online_slot_number, ignored_id=entry.id):
                continue
            return other
        return None

    def _find_lower_load_day(self, session: Session, schedule: Schedule, entry: ScheduleEntry) -> int | None:
        entries = session.exec(
            select(ScheduleEntry).where(
                ScheduleEntry.schedule_id == schedule.id,
                ScheduleEntry.group_id == entry.group_id,
            )
        ).all()
        daily = {day: 0 for day in DAYS}
        for current in entries:
            if current.lesson_mode != entry.lesson_mode:
                continue
            daily[current.day_of_week] += 1
        ordered = sorted(daily.items(), key=lambda item: (item[1], item[0]))
        for day_of_week, _ in ordered:
            if day_of_week != entry.day_of_week:
                return day_of_week
        return None

    def _find_alternate_teacher(self, session: Session, schedule: Schedule, entry: ScheduleEntry) -> int | None:
        fixed = session.exec(
            select(GroupSubjectTeacher).where(
                GroupSubjectTeacher.group_id == entry.group_id,
                GroupSubjectTeacher.subject_id == entry.subject_id,
            )
        ).all()
        allowed_ids = [item.teacher_id for item in fixed]
        if not allowed_ids:
            allowed_ids = [
                item.teacher_id
                for item in session.exec(
                    select(TeacherSubject).where(
                        TeacherSubject.subject_id == entry.subject_id,
                        TeacherSubject.can_teach.is_(True),
                    )
                ).all()
            ]
        entries = session.exec(
            select(ScheduleEntry).where(ScheduleEntry.schedule_id == schedule.id)
        ).all()
        for teacher_id in allowed_ids:
            if teacher_id == entry.teacher_id:
                continue
            blocked = False
            for current in entries:
                if current.id == entry.id or current.teacher_id != teacher_id:
                    continue
                if current.lesson_mode != entry.lesson_mode:
                    continue
                same_slot = (
                    current.day_of_week == entry.day_of_week
                    and (
                        (entry.lesson_mode == LESSON_MODE_REGULAR and current.pair_number == entry.pair_number)
                        or (entry.lesson_mode == LESSON_MODE_ONLINE and current.online_slot_number == entry.online_slot_number)
                    )
                )
                if same_slot and scopes_overlap(current.week_scope, entry.week_scope):
                    blocked = True
                    break
            if not blocked:
                return teacher_id
        return None

    def _find_free_room(self, session: Session, schedule: Schedule, entry: ScheduleEntry) -> int | None:
        rooms = session.exec(select(Room).order_by(Room.code)).all()
        entries = session.exec(select(ScheduleEntry).where(ScheduleEntry.schedule_id == schedule.id)).all()
        for room in rooms:
            occupied = False
            for current in entries:
                if current.id == entry.id or current.room_id != room.id:
                    continue
                if current.lesson_mode != LESSON_MODE_REGULAR:
                    continue
                if current.day_of_week == entry.day_of_week and current.pair_number == entry.pair_number and scopes_overlap(current.week_scope, entry.week_scope):
                    if room_required(current.delivery_mode):
                        occupied = True
                        break
            if not occupied:
                return room.id
        return None

    @staticmethod
    def _slot_has_conflict(
        entries: list[ScheduleEntry],
        entry: ScheduleEntry,
        day_of_week: int,
        pair_number: int,
        online_slot_number: int | None,
        ignored_id: int | None = None,
    ) -> bool:
        for current in entries:
            if current.id == entry.id or current.id == ignored_id:
                continue
            if current.lesson_mode != entry.lesson_mode:
                continue
            if current.day_of_week != day_of_week:
                continue
            if entry.lesson_mode == LESSON_MODE_REGULAR and current.pair_number != pair_number:
                continue
            if entry.lesson_mode == LESSON_MODE_ONLINE and current.online_slot_number != online_slot_number:
                continue
            if not scopes_overlap(current.week_scope, entry.week_scope):
                continue
            if current.group_id == entry.group_id or current.teacher_id == entry.teacher_id:
                return True
            if entry.lesson_mode == LESSON_MODE_REGULAR and room_required(current.delivery_mode) and room_required(entry.delivery_mode):
                if entry.room_id is not None and current.room_id == entry.room_id:
                    return True
        return False

    @staticmethod
    def _first_free_online_slot(entries: list[ScheduleEntry], entry: ScheduleEntry) -> int:
        for slot_number in online_slot_numbers():
            day_of_week = online_slot_day(slot_number)
            occupied = any(
                current.lesson_mode == LESSON_MODE_ONLINE
                and current.group_id == entry.group_id
                and current.day_of_week == day_of_week
                and current.online_slot_number == slot_number
                and scopes_overlap(current.week_scope, entry.week_scope)
                for current in entries
            )
            if not occupied:
                return slot_number
        return online_slot_for_day(ONLINE_ALLOWED_DAYS[0]) or 1
