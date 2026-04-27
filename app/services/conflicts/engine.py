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
    SLOT_CATEGORY_ONLINE_EXTRA,
    SLOT_CATEGORY_REGULAR,
    allowed_pairs_for_shift,
    day_label,
    intervals_overlap,
    online_slot_label,
    pair_label,
    pair_time_range,
    room_required,
    shift_label,
)
from app.core.week_scope import decode_week_scope, scopes_overlap
from app.models import (
    AcademicPeriod,
    Conflict,
    CurriculumLoad,
    Group,
    GroupSubjectTeacher,
    Schedule,
    ScheduleEntry,
    Subject,
    TeacherSubject,
    WeeklyLoad,
)
from app.services.online_policy import OnlinePolicyService
from app.services.online_slots import OnlineSlotService
from app.services.scheduler.normalizer import WorkloadNormalizer


class ConflictEngine:
    def __init__(self) -> None:
        self.online_policy_service = OnlinePolicyService()
        self.online_slot_service = OnlineSlotService()
        self.normalizer = WorkloadNormalizer()

    def refresh(self, session: Session, schedule: Schedule) -> list[Conflict]:
        session.exec(delete(Conflict).where(Conflict.schedule_id == schedule.id))
        related_schedule_ids = self._related_schedule_ids(session, schedule)
        all_entries = session.exec(select(ScheduleEntry).where(ScheduleEntry.schedule_id.in_(related_schedule_ids))).all()
        entries = [entry for entry in all_entries if entry.schedule_id == schedule.id]
        conflicts: list[Conflict] = []
        conflicts.extend(self._detect_slot_conflicts(all_entries, schedule.id or 0))
        conflicts.extend(self._detect_teacher_eligibility(session, entries, schedule.id or 0))
        conflicts.extend(self._detect_blocked_periods(session, entries, schedule.id or 0))
        conflicts.extend(self._detect_shift_violations(session, entries, schedule.id or 0))
        conflicts.extend(self._detect_room_requirements(entries, schedule.id or 0))
        conflicts.extend(self._detect_unscheduled_load(session, schedule, entries))
        conflicts.extend(self._detect_online_rules(session, entries, schedule.id or 0))
        conflicts.extend(self._detect_daily_load(entries, schedule.id or 0))
        conflicts.extend(self._detect_subject_stacking(entries, schedule.id or 0))
        for conflict in conflicts:
            session.add(conflict)
        session.commit()
        return session.exec(select(Conflict).where(Conflict.schedule_id == schedule.id)).all()

    def _detect_slot_conflicts(self, entries: list[ScheduleEntry], schedule_id: int) -> list[Conflict]:
        conflicts: list[Conflict] = []
        for index, left in enumerate(entries):
            for right in entries[index + 1 :]:
                if left.schedule_id != schedule_id and right.schedule_id != schedule_id:
                    continue
                if left.day_of_week != right.day_of_week:
                    continue
                if not scopes_overlap(left.week_scope, right.week_scope):
                    continue
                if not intervals_overlap(left.start_time, left.end_time, right.start_time, right.end_time):
                    continue
                overlap_weeks = sorted(decode_week_scope(left.week_scope) & decode_week_scope(right.week_scope))
                weeks_text = ", ".join(str(week) for week in overlap_weeks[:10])
                slot_text = self._slot_text(left)
                if self._group_overlap(left, right):
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
                    message = "Преподаватель назначен на два занятия одновременно"
                    if left.lesson_mode != right.lesson_mode:
                        message = "Конфликт онлайн- и очного занятия у преподавателя"
                    conflicts.append(
                        Conflict(
                            schedule_id=schedule_id,
                            type="teacher_double_booked",
                            severity="hard",
                            code="TCH-DBL",
                            message=f"{message}: {slot_text}. Совпадающие недели: {weeks_text}.",
                            related_entry_ids=f"{left.id},{right.id}",
                        )
                    )
                if room_required(left.delivery_mode) and room_required(right.delivery_mode) and left.room_id and left.room_id == right.room_id:
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

    @staticmethod
    def _related_schedule_ids(session: Session, schedule: Schedule) -> list[int]:
        if schedule.generation_job_id:
            related = session.exec(select(Schedule.id).where(Schedule.generation_job_id == schedule.generation_job_id)).all()
            ids = [item for item in related if item is not None]
            return ids or [schedule.id or 0]
        return [schedule.id or 0]

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
                    message="Преподаватель не назначен на этот предмет для выбранной группы.",
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
                    message="Занятие поставлено на недоступный учебный период: " + ", ".join(str(period.week_number) for period in blocked),
                    related_entry_ids=str(entry.id),
                )
            )
        return conflicts

    def _detect_shift_violations(self, session: Session, entries: list[ScheduleEntry], schedule_id: int) -> list[Conflict]:
        groups = {group.id: group for group in session.exec(select(Group)).all()}
        online_slot_map = self.online_slot_service.slot_map(session)
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
            slot = online_slot_map.get(entry.online_slot_number or 0)
            if entry.slot_category != SLOT_CATEGORY_ONLINE_EXTRA or slot is None:
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
                continue
            if entry.day_of_week != slot.day_of_week:
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
        conflicts: list[Conflict] = []
        groups = self._schedule_groups(session, schedule)
        _, normalized_rows, _ = self.normalizer.normalize_scope(
            session,
            semester=schedule.semester,
            group_codes=[group.code for group in groups],
            include_facultatives=False,
        )
        placed_by_load: dict[str, int] = defaultdict(int)
        related_entries_by_load: dict[str, list[ScheduleEntry]] = defaultdict(list)
        for entry in entries:
            if not entry.source_load_key:
                continue
            placed_by_load[entry.source_load_key] += len(decode_week_scope(entry.week_scope))
            related_entries_by_load[entry.source_load_key].append(entry)

        for row in normalized_rows:
            if row.excluded_status or row.total_pairs <= 0:
                continue
            placed_pairs = placed_by_load.get(row.load_key, 0)
            if placed_pairs >= row.total_pairs:
                continue
            missing_pairs = row.total_pairs - placed_pairs
            related_ids = ",".join(str(entry.id) for entry in related_entries_by_load.get(row.load_key, []) if entry.id is not None)
            reason = self._unscheduled_reason(row)
            conflicts.append(
                Conflict(
                    schedule_id=schedule.id or 0,
                    type="unscheduled_load",
                    severity="hard",
                    code="LOAD-MISSING",
                    message=(
                        f"Не удалось полностью разместить предмет «{row.subject_name}» для группы {row.group_code}"
                        f"{f', подгруппа {row.subgroup_code}' if row.subgroup_code else ''}: не хватает {missing_pairs} пар. "
                        f"Причина: {reason}"
                    ),
                    related_entry_ids=related_ids,
                    details_json=json.dumps(
                        {
                            "group_id": row.group_id,
                            "subject_id": row.subject_id,
                            "subgroup_code": row.subgroup_code,
                            "expected_pairs": row.total_pairs,
                            "placed_pairs": placed_pairs,
                            "missing_pairs": missing_pairs,
                            "assignment_state": row.assignment_state,
                            "source_load_key": row.load_key,
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
        online_slot_map = self.online_slot_service.slot_map(session)
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
                slot = online_slot_map.get(entry.online_slot_number or 0)
                if slot is None or not slot.is_active:
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
                elif entry.day_of_week != slot.day_of_week:
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
                        related_entry_ids=",".join(
                            str(entry.id)
                            for entry in group_entries
                            if entry.day_of_week == day_of_week and entry.lesson_mode == LESSON_MODE_ONLINE
                        ),
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
        for (_group_id, day_of_week), count in counts.items():
            if count <= 3:
                continue
            conflicts.append(
                Conflict(
                    schedule_id=schedule_id,
                    type="too_many_lessons_in_day",
                    severity="soft",
                    code="DAY-LOAD",
                    message=f"У группы слишком плотный день: {day_label(day_of_week)}, {count} повторяющихся занятий.",
                    related_entry_ids=",".join(str(item) for item in related[(_group_id, day_of_week)]),
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
    def _group_overlap(left: ScheduleEntry, right: ScheduleEntry) -> bool:
        if left.group_id != right.group_id:
            return False
        if left.subgroup_code and right.subgroup_code and left.subgroup_code != right.subgroup_code:
            return False
        return True

    @staticmethod
    def _slot_text(entry: ScheduleEntry) -> str:
        if entry.lesson_mode == LESSON_MODE_ONLINE:
            return f"{day_label(entry.day_of_week)}, {online_slot_label(entry.online_slot_number or 1)}"
        return f"{day_label(entry.day_of_week)}, {pair_label(entry.pair_number)} ({pair_time_range(entry.pair_number)})"

    @staticmethod
    def _schedule_groups(session: Session, schedule: Schedule) -> list[Group]:
        group_ids = ConflictEngine._schedule_group_ids(session, schedule)
        if not group_ids:
            return []
        return session.exec(select(Group).where(Group.id.in_(group_ids))).all()

    @staticmethod
    def _unscheduled_reason(row) -> str:
        if row.assignment_state == "vacancy":
            return "для этой нагрузки не назначен преподаватель."
        if row.assignment_state == "multi_teacher_ambiguous":
            return "неоднозначное закрепление преподавателя по семестру."
        if row.assignment_state == "multi_teacher":
            return "в исходной строке указано несколько преподавателей, требуется уточнение."
        if row.assignment_state == "unresolved_manual_review":
            return "строка требует ручной проверки преподавателя."
        if row.assignment_state == "candidate_pool" and not row.teacher_candidates:
            return "не найден допустимый преподаватель."
        if row.normalization_issue:
            text = row.normalization_issue.rstrip(".")
            if text:
                return text[0].lower() + text[1:] + "."
        return "не хватило свободных слотов без нарушения жёстких ограничений."

    @staticmethod
    def _schedule_group_ids(session: Session, schedule: Schedule) -> list[int]:
        codes = [item.strip() for item in (schedule.group_scope or "").split(",") if item.strip()]
        if codes:
            return [group.id or 0 for group in session.exec(select(Group).where(Group.code.in_(codes))).all()]
        rows = session.exec(select(ScheduleEntry.group_id).where(ScheduleEntry.schedule_id == schedule.id).distinct()).all()
        return [row[0] if isinstance(row, tuple) else int(row) for row in rows]

    @staticmethod
    def is_hard(conflict: Conflict) -> bool:
        return conflict.type in HARD_CONFLICTS or conflict.severity == "hard"
