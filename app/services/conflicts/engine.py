from __future__ import annotations

import json
from collections import defaultdict

from sqlmodel import Session, delete, select

from app.core.constants import HARD_CONFLICTS
from app.core.timetable import (
    DAYS,
    DELIVERY_ONLINE,
    LESSON_MODE_ONLINE,
    LESSON_MODE_REGULAR,
    ONLINE_ALLOWED_DAYS,
    SLOT_CATEGORY_ONLINE_EXTRA,
    SLOT_CATEGORY_REGULAR,
    allowed_pairs_for_shift,
    day_label,
    online_slot_day,
    online_slot_matches_day,
    online_slot_numbers,
    online_slot_label,
    pair_label,
    pair_time_range,
    room_required,
    shift_label,
)
from app.core.week_scope import decode_week_scope, scopes_overlap
from app.models import AcademicPeriod, Conflict, CurriculumLoad, Group, GroupSubjectTeacher, Schedule, ScheduleEntry, Subject, Teacher, TeacherSubject
from app.services.online_policy import OnlinePolicyService


class ConflictEngine:
    def __init__(self) -> None:
        self.online_policy_service = OnlinePolicyService()

    def refresh(self, session: Session, schedule: Schedule) -> list[Conflict]:
        session.exec(delete(Conflict).where(Conflict.schedule_id == schedule.id))
        entries = session.exec(
            select(ScheduleEntry).where(ScheduleEntry.schedule_id == schedule.id)
        ).all()
        conflicts: list[Conflict] = []
        conflicts.extend(self._detect_slot_conflicts(entries, schedule.id))
        conflicts.extend(self._detect_teacher_eligibility(session, entries, schedule.id))
        conflicts.extend(self._detect_blocked_periods(session, entries, schedule.id))
        conflicts.extend(self._detect_shift_violations(session, entries, schedule.id))
        conflicts.extend(self._detect_room_requirements(entries, schedule.id))
        conflicts.extend(self._detect_unscheduled_load(session, schedule, entries))
        conflicts.extend(self._detect_online_rules(session, entries, schedule.id))
        conflicts.extend(self._detect_daily_load(entries, schedule.id))
        conflicts.extend(self._detect_subject_stacking(entries, schedule.id))
        for conflict in conflicts:
            session.add(conflict)
        session.commit()
        return session.exec(select(Conflict).where(Conflict.schedule_id == schedule.id)).all()

    def _detect_slot_conflicts(self, entries: list[ScheduleEntry], schedule_id: int) -> list[Conflict]:
        conflicts: list[Conflict] = []
        for index, left in enumerate(entries):
            for right in entries[index + 1 :]:
                if left.lesson_mode != right.lesson_mode:
                    continue
                if left.day_of_week != right.day_of_week:
                    continue
                if left.lesson_mode == LESSON_MODE_REGULAR and left.pair_number != right.pair_number:
                    continue
                if left.lesson_mode == LESSON_MODE_ONLINE and left.online_slot_number != right.online_slot_number:
                    continue
                if not scopes_overlap(left.week_scope, right.week_scope):
                    continue
                overlap_weeks = sorted(decode_week_scope(left.week_scope) & decode_week_scope(right.week_scope))
                weeks_text = ", ".join(str(week) for week in overlap_weeks[:10])
                if left.lesson_mode == LESSON_MODE_ONLINE:
                    slot_text = f"{day_label(left.day_of_week)}, {online_slot_label(left.online_slot_number or 1)}"
                else:
                    slot_text = f"{day_label(left.day_of_week)}, {pair_label(left.pair_number)} ({pair_time_range(left.pair_number)})"
                if left.group_id == right.group_id:
                    conflicts.append(
                        Conflict(
                            schedule_id=schedule_id,
                            type="group_double_booked",
                            severity="hard",
                            code="GRP-DBL",
                            message=f"Группа занята дважды в одном слоте: {slot_text}. Совпадающие недели: {weeks_text}.",
                            related_entry_ids=f"{left.id},{right.id}",
                        )
                    )
                if left.teacher_id == right.teacher_id:
                    conflicts.append(
                        Conflict(
                            schedule_id=schedule_id,
                            type="teacher_double_booked",
                            severity="hard",
                            code="TCH-DBL",
                            message=f"Преподаватель назначен на два занятия одновременно: {slot_text}. Совпадающие недели: {weeks_text}.",
                            related_entry_ids=f"{left.id},{right.id}",
                        )
                    )
                if left.lesson_mode == LESSON_MODE_REGULAR and room_required(left.delivery_mode) and room_required(right.delivery_mode) and left.room_id == right.room_id:
                    conflicts.append(
                        Conflict(
                            schedule_id=schedule_id,
                            type="room_double_booked",
                            severity="hard",
                            code="ROM-DBL",
                            message=f"Аудитория занята двумя очными занятиями одновременно: {slot_text}.",
                            related_entry_ids=f"{left.id},{right.id}",
                        )
                    )
        return conflicts

    def _detect_teacher_eligibility(
        self,
        session: Session,
        entries: list[ScheduleEntry],
        schedule_id: int,
    ) -> list[Conflict]:
        conflicts: list[Conflict] = []
        for entry in entries:
            fixed = session.exec(
                select(GroupSubjectTeacher).where(
                    GroupSubjectTeacher.group_id == entry.group_id,
                    GroupSubjectTeacher.subject_id == entry.subject_id,
                )
            ).all()
            fixed_teacher_ids = [item.teacher_id for item in fixed]
            if entry.teacher_id in fixed_teacher_ids:
                continue
            mapping = session.exec(
                select(TeacherSubject).where(
                    TeacherSubject.teacher_id == entry.teacher_id,
                    TeacherSubject.subject_id == entry.subject_id,
                    TeacherSubject.can_teach.is_(True),
                )
            ).first()
            if mapping:
                continue
            conflicts.append(
                Conflict(
                    schedule_id=schedule_id,
                    type="teacher_not_allowed",
                    severity="hard",
                    code="TCH-NOT-ALLOWED",
                    message=f"Преподаватель не назначен на этот предмет для выбранной группы.",
                    related_entry_ids=str(entry.id),
                    details_json=json.dumps({"allowed_teacher_ids": fixed_teacher_ids}),
                )
            )
        return conflicts

    def _detect_blocked_periods(
        self,
        session: Session,
        entries: list[ScheduleEntry],
        schedule_id: int,
    ) -> list[Conflict]:
        conflicts: list[Conflict] = []
        for entry in entries:
            weeks = decode_week_scope(entry.week_scope)
            if not weeks:
                continue
            blocked = session.exec(
                select(AcademicPeriod).where(
                    AcademicPeriod.group_id == entry.group_id,
                    AcademicPeriod.week_number.in_(weeks),
                    AcademicPeriod.is_schedulable.is_(False),
                )
            ).all()
            if not blocked:
                continue
            conflicts.append(
                Conflict(
                    schedule_id=schedule_id,
                    type="blocked_period",
                    severity="hard",
                    code="BLK-PERIOD",
                    message=(
                        "Занятие поставлено на недоступный учебный период: "
                        + ", ".join(str(period.week_number) for period in blocked)
                    ),
                    related_entry_ids=str(entry.id),
                )
            )
        return conflicts

    def _detect_shift_violations(self, session: Session, entries: list[ScheduleEntry], schedule_id: int) -> list[Conflict]:
        groups = {group.id: group for group in session.exec(select(Group)).all()}
        conflicts: list[Conflict] = []
        for entry in entries:
            group = groups.get(entry.group_id)
            if group is None:
                continue
            if entry.lesson_mode == LESSON_MODE_REGULAR:
                if entry.slot_category != SLOT_CATEGORY_REGULAR:
                    conflicts.append(
                        Conflict(
                            schedule_id=schedule_id,
                            type="regular_in_online_slot",
                            severity="hard",
                            code="REGULAR-SLOT",
                            message="Очные занятия нельзя ставить в онлайн-слоты.",
                            related_entry_ids=str(entry.id),
                        )
                    )
                    continue
                if entry.pair_number in allowed_pairs_for_shift(group.shift):
                    continue
                conflicts.append(
                    Conflict(
                        schedule_id=schedule_id,
                        type="shift_violation",
                        severity="hard",
                        code="SHIFT-VIOLATION",
                        message=(
                            f"Занятие группы {group.code} поставлено вне ее смены. "
                            f"Разрешена только {shift_label(group.shift)}."
                        ),
                        related_entry_ids=str(entry.id),
                    )
                )
                continue
            if entry.slot_category != SLOT_CATEGORY_ONLINE_EXTRA:
                conflicts.append(
                    Conflict(
                        schedule_id=schedule_id,
                        type="online_slot_violation",
                        severity="hard",
                        code="ONLINE-SLOT",
                        message="Онлайн-занятия можно ставить только в отдельные онлайн-слоты.",
                        related_entry_ids=str(entry.id),
                    )
                )
        return conflicts

    def _detect_room_requirements(self, entries: list[ScheduleEntry], schedule_id: int) -> list[Conflict]:
        conflicts: list[Conflict] = []
        for entry in entries:
            if entry.lesson_mode == LESSON_MODE_REGULAR and room_required(entry.delivery_mode) and entry.room_id is None:
                conflicts.append(
                    Conflict(
                        schedule_id=schedule_id,
                        type="room_required",
                        severity="hard",
                        code="ROOM-REQUIRED",
                        message="Для очного или гибридного занятия должна быть указана аудитория.",
                        related_entry_ids=str(entry.id),
                    )
                )
        return conflicts

    def _detect_unscheduled_load(
        self,
        session: Session,
        schedule: Schedule,
        entries: list[ScheduleEntry],
    ) -> list[Conflict]:
        loads = session.exec(select(CurriculumLoad).where(CurriculumLoad.semester == schedule.semester)).all()
        target_group_ids = {entry.group_id for entry in entries}
        relevant_loads = [load for load in loads if load.group_id in target_group_ids]
        grouped_entries: dict[tuple[int, int], list[ScheduleEntry]] = defaultdict(list)
        subjects = {subject.id: subject for subject in session.exec(select(Subject)).all()}
        groups = {group.id: group for group in session.exec(select(Group)).all()}
        study_weeks_by_group = {
            group_id: len(
                session.exec(
                    select(AcademicPeriod).where(
                        AcademicPeriod.group_id == group_id,
                        AcademicPeriod.semester == schedule.semester,
                        AcademicPeriod.is_schedulable.is_(True),
                    )
                ).all()
            )
            for group_id in {load.group_id for load in relevant_loads}
        }
        expected_pairs_by_group: dict[int, int] = defaultdict(int)
        for load in relevant_loads:
            expected_pairs_by_group[load.group_id] += max(int(round(load.total_hours / 2)), 1)
        conflicts: list[Conflict] = []
        for entry in entries:
            grouped_entries[(entry.group_id, entry.subject_id)].append(entry)
        for load in relevant_loads:
            scheduled_pairs = sum(
                len(decode_week_scope(entry.week_scope))
                for entry in grouped_entries.get((load.group_id, load.subject_id), [])
            )
            expected_pairs = max(int(round(load.total_hours / 2)), 1)
            if scheduled_pairs >= expected_pairs:
                continue
            missing_pairs = expected_pairs - scheduled_pairs
            group = groups.get(load.group_id)
            subject = subjects.get(load.subject_id)
            reason_parts: list[str] = []
            study_weeks = study_weeks_by_group.get(load.group_id, 0)
            if study_weeks == 0:
                reason_parts.append("для семестра не заданы учебные недели")
            teacher_mappings = session.exec(
                select(GroupSubjectTeacher).where(
                    GroupSubjectTeacher.group_id == load.group_id,
                    GroupSubjectTeacher.subject_id == load.subject_id,
                )
            ).all()
            teacher_allowed = teacher_mappings or session.exec(
                select(TeacherSubject).where(
                    TeacherSubject.subject_id == load.subject_id,
                    TeacherSubject.can_teach.is_(True),
                )
            ).all()
            if not teacher_allowed:
                reason_parts.append("не назначен допустимый преподаватель")
            if group is not None and study_weeks:
                regular_capacity = study_weeks * len(DAYS) * len(allowed_pairs_for_shift(group.shift))
                online_capacity = study_weeks * len(online_slot_numbers())
                capacity = regular_capacity + online_capacity
                expected_group_pairs = expected_pairs_by_group.get(load.group_id, 0)
                if expected_group_pairs > capacity:
                    reason_parts.append(
                        f"суммарная нагрузка группы ({expected_group_pairs} пар) превышает общую вместимость основного и онлайн-слотов ({capacity} пар)"
                    )
            related_ids = ",".join(
                str(entry.id)
                for entry in grouped_entries.get((load.group_id, load.subject_id), [])
                if entry.id is not None
            )
            message = (
                f"Не удалось разместить всю нагрузку по предмету "
                f"\"{subject.name if subject else load.subject_id}\" для группы "
                f"{group.code if group else load.group_id}: не хватает {missing_pairs} пар."
            )
            if reason_parts:
                message += " Причина: " + "; ".join(reason_parts) + "."
            conflicts.append(
                Conflict(
                    schedule_id=schedule.id or 0,
                    type="unscheduled_load",
                    severity="hard",
                    code="LOAD-MISSING",
                    message=message,
                    related_entry_ids=related_ids,
                    details_json=json.dumps(
                        {
                            "group_id": load.group_id,
                            "subject_id": load.subject_id,
                            "expected_pairs": expected_pairs,
                            "scheduled_pairs": scheduled_pairs,
                            "missing_pairs": missing_pairs,
                        }
                    ),
                )
            )
        return conflicts

    def _detect_online_rules(
        self,
        session: Session,
        entries: list[ScheduleEntry],
        schedule_id: int,
    ) -> list[Conflict]:
        conflicts: list[Conflict] = []
        groups = {group.id: group for group in session.exec(select(Group)).all()}
        subjects = {subject.id: subject for subject in session.exec(select(Subject)).all()}
        grouped_entries: dict[int, list[ScheduleEntry]] = defaultdict(list)
        for entry in entries:
            grouped_entries[entry.group_id].append(entry)
            if entry.lesson_mode != LESSON_MODE_ONLINE and entry.delivery_mode == DELIVERY_ONLINE:
                conflicts.append(
                    Conflict(
                        schedule_id=schedule_id,
                        type="online_slot_violation",
                        severity="hard",
                        code="ONLINE-SLOT",
                        message="Онлайн-занятия можно ставить только в отдельные онлайн-слоты.",
                        related_entry_ids=str(entry.id),
                    )
                )
            if entry.lesson_mode == LESSON_MODE_ONLINE:
                if entry.day_of_week not in ONLINE_ALLOWED_DAYS:
                    conflicts.append(
                        Conflict(
                            schedule_id=schedule_id,
                            type="online_day_violation",
                            severity="hard",
                            code="ONLINE-DAY-RULE",
                            message="Онлайн-занятия доступны только в среду, четверг и пятницу.",
                            related_entry_ids=str(entry.id),
                        )
                    )
                if entry.online_slot_number not in online_slot_numbers() or not online_slot_matches_day(entry.online_slot_number or 0, entry.day_of_week):
                    conflicts.append(
                        Conflict(
                            schedule_id=schedule_id,
                            type="online_slot_violation",
                            severity="hard",
                            code="ONLINE-SLOT",
                            message="Онлайн-занятия можно ставить только в отдельные онлайн-слоты.",
                            related_entry_ids=str(entry.id),
                        )
                    )
                group = groups.get(entry.group_id)
                subject = subjects.get(entry.subject_id)
                if group and subject and not self.online_policy_service.is_subject_allowed_online(session, group, subject):
                    conflicts.append(
                        Conflict(
                            schedule_id=schedule_id,
                            type="online_not_allowed",
                            severity="soft",
                            code="ONLINE-NOT-ALLOWED",
                            message="Для этого предмета онлайн-формат не разрешен политикой.",
                            related_entry_ids=str(entry.id),
                        )
                    )
        for group_id, group_entries in grouped_entries.items():
            group = groups.get(group_id)
            if group is None:
                continue
            target = self.online_policy_service.get_target_for_group(session, group)
            current = sum(1 for entry in group_entries if entry.lesson_mode == LESSON_MODE_ONLINE)
            if current < target:
                conflicts.append(
                    Conflict(
                        schedule_id=schedule_id,
                        type="online_target_not_met",
                        severity="soft",
                        code="ONLINE-TARGET",
                        message=f"Цель по онлайн-занятиям не достигнута: сейчас {current}, требуется {target} в неделю.",
                        related_entry_ids=",".join(str(entry.id) for entry in group_entries[:10] if entry.id),
                        details_json=json.dumps({"group_id": group_id, "current": current, "target": target}),
                    )
                )
            by_day: dict[int, int] = defaultdict(int)
            for entry in group_entries:
                if entry.lesson_mode == LESSON_MODE_ONLINE:
                    by_day[entry.day_of_week] += 1
            for day_of_week, count in by_day.items():
                if count <= 2:
                    continue
                conflicts.append(
                    Conflict(
                        schedule_id=schedule_id,
                        type="too_many_online_lessons_in_day",
                        severity="soft",
                        code="ONLINE-DAY",
                        message=f"Слишком много онлайн-занятий в один день: {day_label(day_of_week)} ({count}).",
                        related_entry_ids=",".join(str(entry.id) for entry in group_entries if entry.day_of_week == day_of_week and entry.lesson_mode == LESSON_MODE_ONLINE),
                    )
                )
        return conflicts

    def _detect_daily_load(self, entries: list[ScheduleEntry], schedule_id: int) -> list[Conflict]:
        counts: dict[tuple[int, int], int] = defaultdict(int)
        related: dict[tuple[int, int], list[int]] = defaultdict(list)
        for entry in entries:
            if entry.lesson_mode != LESSON_MODE_REGULAR:
                continue
            counts[(entry.group_id, entry.day_of_week)] += 1
            related[(entry.group_id, entry.day_of_week)].append(entry.id or 0)
        conflicts: list[Conflict] = []
        for (group_id, day_of_week), count in counts.items():
            if count <= 3:
                continue
            conflicts.append(
                Conflict(
                    schedule_id=schedule_id,
                    type="too_many_lessons_in_day",
                    severity="soft",
                    code="DAY-LOAD",
                    message=f"У группы слишком плотный день: {day_label(day_of_week)}, {count} повторяющихся занятий.",
                    related_entry_ids=",".join(str(item) for item in related[(group_id, day_of_week)]),
                )
            )
        return conflicts

    def _detect_subject_stacking(self, entries: list[ScheduleEntry], schedule_id: int) -> list[Conflict]:
        buckets: dict[tuple[int, int, int], list[int]] = defaultdict(list)
        for entry in entries:
            if entry.lesson_mode != LESSON_MODE_REGULAR:
                continue
            buckets[(entry.group_id, entry.day_of_week, entry.subject_id)].append(entry.id or 0)
        conflicts: list[Conflict] = []
        for (_group_id, day_of_week, _subject_id), related in buckets.items():
            if len(related) <= 2:
                continue
            conflicts.append(
                Conflict(
                    schedule_id=schedule_id,
                    type="same_subject_stack",
                    severity="soft",
                    code="SUBJ-STACK",
                    message=f"Один и тот же предмет поставлен слишком много раз в день: {day_label(day_of_week)}.",
                    related_entry_ids=",".join(str(item) for item in related),
                )
            )
        return conflicts

    @staticmethod
    def is_hard(conflict: Conflict) -> bool:
        return conflict.type in HARD_CONFLICTS or conflict.severity == "hard"
