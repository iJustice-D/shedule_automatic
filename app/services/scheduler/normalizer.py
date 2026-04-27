from __future__ import annotations

import math
from collections import defaultdict

from sqlmodel import Session, select

from app.core.timetable import DELIVERY_OFFLINE
from app.core.week_scope import encode_week_scope, spread_weeks
from app.models import AcademicPeriod, CurriculumLoad, Group, GroupSubjectTeacher, Subject, Teacher, TeacherSubject, WeeklyLoad
from app.services.online_policy import OnlinePolicyService
from app.services.scheduler.models import NormalizedLoadRow, PlacementRequest


class WorkloadNormalizer:
    def __init__(self) -> None:
        self.online_policy_service = OnlinePolicyService()

    def normalize_scope(
        self,
        session: Session,
        *,
        semester: int,
        group_codes: list[str] | None = None,
        include_facultatives: bool = False,
    ) -> tuple[list[Group], list[NormalizedLoadRow], list[PlacementRequest]]:
        groups = self._groups(session, group_codes)
        group_map = {group.id or 0: group for group in groups}
        subject_map = {subject.id or 0: subject for subject in session.exec(select(Subject)).all()}
        study_weeks_by_group = {
            group.id or 0: self._study_weeks(session, group.id or 0, semester)
            for group in groups
        }

        weekly_rows = self._weekly_rows(session, semester, list(group_map))
        use_weekly = bool(weekly_rows)
        normalized_rows: list[NormalizedLoadRow] = []

        if use_weekly:
            for row in weekly_rows:
                group = group_map.get(row.group_id)
                subject = subject_map.get(row.subject_id)
                if group is None or subject is None:
                    continue
                normalized = self._normalize_weekly_row(
                    session,
                    group=group,
                    subject=subject,
                    row=row,
                    study_weeks=study_weeks_by_group.get(group.id or 0, []),
                    include_facultatives=include_facultatives,
                )
                normalized_rows.append(normalized)
        else:
            for row in self._curriculum_rows(session, semester, list(group_map)):
                group = group_map.get(row.group_id)
                subject = subject_map.get(row.subject_id)
                if group is None or subject is None:
                    continue
                normalized = self._normalize_curriculum_row(
                    session,
                    group=group,
                    subject=subject,
                    row=row,
                    study_weeks=study_weeks_by_group.get(group.id or 0, []),
                )
                normalized_rows.append(normalized)

        normalized_rows = self._collapse_ambiguous_duplicates(normalized_rows)
        requests: list[PlacementRequest] = []
        for normalized in normalized_rows:
            requests.extend(self._build_requests(session, group_map[normalized.group_id], normalized, subject_map[normalized.subject_id]))
        return groups, normalized_rows, requests

    @staticmethod
    def _groups(session: Session, group_codes: list[str] | None) -> list[Group]:
        query = select(Group).where(Group.is_active.is_(True)).order_by(Group.code)
        if group_codes:
            query = query.where(Group.code.in_(group_codes))
        return session.exec(query).all()

    @staticmethod
    def _weekly_rows(session: Session, semester: int, group_ids: list[int]) -> list[WeeklyLoad]:
        if not group_ids:
            return []
        return session.exec(
            select(WeeklyLoad).where(
                WeeklyLoad.is_active.is_(True),
                WeeklyLoad.semester == semester,
                WeeklyLoad.group_id.in_(group_ids),
            ).order_by(WeeklyLoad.group_id, WeeklyLoad.source_priority.desc(), WeeklyLoad.subject_id, WeeklyLoad.id)
        ).all()

    @staticmethod
    def _curriculum_rows(session: Session, semester: int, group_ids: list[int]) -> list[CurriculumLoad]:
        if not group_ids:
            return []
        return session.exec(
            select(CurriculumLoad).where(
                CurriculumLoad.semester == semester,
                CurriculumLoad.group_id.in_(group_ids),
            ).order_by(CurriculumLoad.group_id, CurriculumLoad.subject_id, CurriculumLoad.id)
        ).all()

    @staticmethod
    def _study_weeks(session: Session, group_id: int, semester: int) -> list[int]:
        periods = session.exec(
            select(AcademicPeriod).where(
                AcademicPeriod.group_id == group_id,
                AcademicPeriod.semester == semester,
                AcademicPeriod.is_schedulable.is_(True),
            ).order_by(AcademicPeriod.week_number)
        ).all()
        if periods:
            return [period.week_number for period in periods]
        weekly_rows = session.exec(
            select(WeeklyLoad).where(
                WeeklyLoad.group_id == group_id,
                WeeklyLoad.semester == semester,
                WeeklyLoad.is_active.is_(True),
            )
        ).all()
        source_week_count = max((row.study_weeks for row in weekly_rows if row.study_weeks), default=0)
        if source_week_count > 0:
            return list(range(1, source_week_count + 1))
        curriculum_rows = session.exec(
            select(CurriculumLoad).where(
                CurriculumLoad.group_id == group_id,
                CurriculumLoad.semester == semester,
            )
        ).all()
        source_week_count = max((row.study_weeks for row in curriculum_rows if row.study_weeks), default=0)
        if source_week_count > 0:
            return list(range(1, source_week_count + 1))
        return []

    def _normalize_weekly_row(
        self,
        session: Session,
        *,
        group: Group,
        subject: Subject,
        row: WeeklyLoad,
        study_weeks: list[int],
        include_facultatives: bool,
    ) -> NormalizedLoadRow:
        teacher_candidates = self._teacher_candidates_from_weekly(session, row)
        excluded_status = ""
        excluded_reason = ""
        if row.is_practice or row.load_category in {"practice", "study_practice", "industrial_practice"}:
            excluded_status = "Исключено как практика"
            excluded_reason = "Практика учитывается отдельно и не размещается в обычной сетке."
        elif row.is_facultative and not include_facultatives:
            excluded_status = "Исключено как факультатив"
            excluded_reason = "Факультатив не включён в текущий режим генерации."

        normalization_issue = ""
        if not study_weeks and not excluded_status:
            normalization_issue = "Для выбранной группы и семестра не найдены учебные недели."
        elif row.study_weeks and study_weeks and row.study_weeks != len(study_weeks):
            normalization_issue = (
                f"Несовпадение диапазона учебных недель: в источнике {row.study_weeks}, "
                f"в учебном календаре {len(study_weeks)}."
            )
        elif row.assignment_state in {"vacancy", "unresolved_manual_review", "multi_teacher"}:
            normalization_issue = self._teacher_state_message(row.assignment_state)
        elif row.assignment_state == "candidate_pool" and not teacher_candidates:
            normalization_issue = "Для строки не найден допустимый пул преподавателей."

        total_pairs = self._expected_total_pairs(row.total_hours, row.weekly_pairs, len(study_weeks) or row.study_weeks)
        return NormalizedLoadRow(
            load_key=f"weekly:{row.id}",
            source_kind="weekly",
            source_id=row.id or 0,
            group_id=group.id or 0,
            group_code=group.code,
            semester=row.semester,
            subject_id=subject.id or 0,
            subject_name=subject.name,
            load_type=row.load_category,
            subgroup_code=row.subgroup_code,
            assignment_state=row.assignment_state,
            teacher_candidates=teacher_candidates,
            fixed_teacher_id=row.resolved_teacher_id or row.fixed_teacher_id,
            weekly_pairs=float(row.weekly_pairs or 0.0),
            total_pairs=total_pairs,
            study_weeks=study_weeks,
            can_be_online=self.online_policy_service.is_subject_allowed_online(session, group, subject),
            default_delivery_mode=row.delivery_mode or subject.default_delivery_mode or DELIVERY_OFFLINE,
            requires_special_room=bool(subject.requires_special_room),
            source_priority=row.source_priority,
            raw_teacher_names=row.raw_teacher_names,
            note=row.raw_import_notes,
            excluded_status=excluded_status,
            excluded_reason=excluded_reason,
            normalization_issue=normalization_issue,
        )

    def _normalize_curriculum_row(
        self,
        session: Session,
        *,
        group: Group,
        subject: Subject,
        row: CurriculumLoad,
        study_weeks: list[int],
    ) -> NormalizedLoadRow:
        teacher_candidates = self._teacher_candidates_for_subject(session, group.id or 0, subject.id or 0)
        assignment_state = "fixed" if teacher_candidates else "unresolved_manual_review"
        normalization_issue = ""
        if not study_weeks:
            normalization_issue = "Для выбранной группы и семестра не найдены учебные недели."
        elif not teacher_candidates:
            normalization_issue = "Для этой нагрузки не назначен преподаватель."
        total_pairs = self._expected_total_pairs(row.total_hours, row.pairs_per_week, len(study_weeks) or row.study_weeks)
        return NormalizedLoadRow(
            load_key=f"curriculum:{row.id}",
            source_kind="curriculum",
            source_id=row.id or 0,
            group_id=group.id or 0,
            group_code=group.code,
            semester=row.semester,
            subject_id=subject.id or 0,
            subject_name=subject.name,
            load_type="regular",
            subgroup_code=None,
            assignment_state=assignment_state,
            teacher_candidates=teacher_candidates,
            fixed_teacher_id=teacher_candidates[0] if teacher_candidates else None,
            weekly_pairs=float(row.pairs_per_week or 0.0),
            total_pairs=total_pairs,
            study_weeks=study_weeks,
            can_be_online=self.online_policy_service.is_subject_allowed_online(session, group, subject),
            default_delivery_mode=row.delivery_mode or subject.default_delivery_mode or DELIVERY_OFFLINE,
            requires_special_room=bool(subject.requires_special_room),
            source_priority=120 if row.source_type == "manual" else 60,
            note=row.note,
            normalization_issue=normalization_issue,
        )

    def _build_requests(
        self,
        session: Session,
        group: Group,
        row: NormalizedLoadRow,
        subject: Subject,
    ) -> list[PlacementRequest]:
        if row.excluded_status or not row.study_weeks:
            return []
        if row.assignment_state in {"vacancy", "unresolved_manual_review", "multi_teacher", "multi_teacher_ambiguous"}:
            return []
        teacher_candidates = row.teacher_candidates
        room_candidates = self._room_candidates(session, subject)
        weekly_pairs = max(float(row.weekly_pairs or 0.0), 0.0)
        requests: list[PlacementRequest] = []
        if weekly_pairs <= 0.0 and row.total_pairs > 0 and row.study_weeks:
            weekly_pairs = round(row.total_pairs / max(len(row.study_weeks), 1), 2)
        whole_pairs = int(math.floor(weekly_pairs + 1e-9))
        fractional_pairs = max(weekly_pairs - whole_pairs, 0.0)
        for index in range(max(whole_pairs, 0)):
            requests.append(
                PlacementRequest(
                    request_key=f"{row.load_key}:full:{index}",
                    load_key=row.load_key,
                    source_kind=row.source_kind,
                    group_id=row.group_id,
                    group_code=row.group_code,
                    semester=row.semester,
                    subject_id=row.subject_id,
                    subject_name=row.subject_name,
                    subgroup_code=row.subgroup_code,
                    assignment_state=row.assignment_state,
                    teacher_candidates=list(teacher_candidates),
                    fixed_teacher_id=row.fixed_teacher_id,
                    room_candidates=list(room_candidates),
                    shift=group.shift,
                    week_scope=encode_week_scope(row.study_weeks),
                    lesson_mode="regular",
                    delivery_mode=row.default_delivery_mode,
                    requires_special_room=row.requires_special_room,
                    can_be_online=row.can_be_online,
                    source_priority=row.source_priority,
                    total_pairs=row.total_pairs,
                    weekly_weight=1.0,
                    note=row.note,
                )
            )
        if fractional_pairs > 0.01 and row.study_weeks:
            active_weeks = max(1, min(len(row.study_weeks), int(round(len(row.study_weeks) * fractional_pairs))))
            requests.append(
                PlacementRequest(
                    request_key=f"{row.load_key}:partial:0",
                    load_key=row.load_key,
                    source_kind=row.source_kind,
                    group_id=row.group_id,
                    group_code=row.group_code,
                    semester=row.semester,
                    subject_id=row.subject_id,
                    subject_name=row.subject_name,
                    subgroup_code=row.subgroup_code,
                    assignment_state=row.assignment_state,
                    teacher_candidates=list(teacher_candidates),
                    fixed_teacher_id=row.fixed_teacher_id,
                    room_candidates=list(room_candidates),
                    shift=group.shift,
                    week_scope=encode_week_scope(spread_weeks(row.study_weeks, active_weeks)),
                    lesson_mode="regular",
                    delivery_mode=row.default_delivery_mode,
                    requires_special_room=row.requires_special_room,
                    can_be_online=row.can_be_online,
                    source_priority=row.source_priority,
                    total_pairs=row.total_pairs,
                    weekly_weight=round(active_weeks / max(len(row.study_weeks), 1), 2),
                    note=row.note,
                )
            )
        return requests

    def _collapse_ambiguous_duplicates(self, rows: list[NormalizedLoadRow]) -> list[NormalizedLoadRow]:
        grouped: dict[tuple[int, int, int, str, str], list[NormalizedLoadRow]] = defaultdict(list)
        passthrough: list[NormalizedLoadRow] = []
        for row in rows:
            if row.excluded_status or row.load_type != "regular":
                passthrough.append(row)
                continue
            grouped[(row.group_id, row.semester, row.subject_id, row.subgroup_code or "", row.load_type)].append(row)

        collapsed: list[NormalizedLoadRow] = list(passthrough)
        for cluster in grouped.values():
            if len(cluster) == 1:
                collapsed.extend(cluster)
                continue
            canonical = cluster[0]
            distinct_fixed = {row.fixed_teacher_id for row in cluster if row.fixed_teacher_id}
            distinct_pairs = {row.total_pairs for row in cluster}
            distinct_weeks = {tuple(row.study_weeks) for row in cluster}
            if len(distinct_fixed) <= 1 and len(distinct_pairs) == 1 and len(distinct_weeks) == 1:
                collapsed.append(canonical)
                continue
            teacher_candidates: list[int] = []
            raw_names: list[str] = []
            states = {row.assignment_state for row in cluster}
            for row in cluster:
                for teacher_id in row.teacher_candidates:
                    if teacher_id not in teacher_candidates:
                        teacher_candidates.append(teacher_id)
                if row.raw_teacher_names:
                    raw_names.append(row.raw_teacher_names)
            merged_state = "multi_teacher_ambiguous"
            if "vacancy" in states and not teacher_candidates:
                merged_state = "vacancy"
            elif "vacancy" in states and teacher_candidates:
                merged_state = "candidate_pool"
            collapsed.append(
                NormalizedLoadRow(
                    load_key="cluster:" + "|".join(sorted(row.load_key for row in cluster)),
                    source_kind="weekly_cluster",
                    source_id=canonical.source_id,
                    group_id=canonical.group_id,
                    group_code=canonical.group_code,
                    semester=canonical.semester,
                    subject_id=canonical.subject_id,
                    subject_name=canonical.subject_name,
                    load_type=canonical.load_type,
                    subgroup_code=canonical.subgroup_code,
                    assignment_state=merged_state,
                    teacher_candidates=teacher_candidates,
                    fixed_teacher_id=None,
                    weekly_pairs=max(row.weekly_pairs for row in cluster),
                    total_pairs=max(row.total_pairs for row in cluster),
                    study_weeks=max((row.study_weeks for row in cluster), key=len),
                    can_be_online=any(row.can_be_online for row in cluster),
                    default_delivery_mode=canonical.default_delivery_mode,
                    requires_special_room=canonical.requires_special_room,
                    source_priority=max(row.source_priority for row in cluster),
                    raw_teacher_names="; ".join(dict.fromkeys(raw_names)),
                    note="Автоматически объединено из неоднозначных строк одного предмета.",
                    normalization_issue="Неоднозначное закрепление преподавателя по семестру.",
                )
            )
        return collapsed

    @staticmethod
    def _expected_total_pairs(total_hours: int, weekly_pairs: float, week_count: int) -> int:
        if total_hours > 0:
            return max(int(round(total_hours / 2)), 1)
        if weekly_pairs > 0 and week_count > 0:
            return max(int(round(weekly_pairs * week_count)), 1)
        return 0

    @staticmethod
    def _teacher_state_message(state: str) -> str:
        messages = {
            "vacancy": "Для этой нагрузки не назначен преподаватель.",
            "multi_teacher": "В исходной строке указано несколько преподавателей, требуется уточнение.",
            "multi_teacher_ambiguous": "Неоднозначное закрепление преподавателя по семестру.",
            "unresolved_manual_review": "Строка требует ручной проверки преподавателя.",
            "candidate_pool": "Для строки доступен только пул кандидатов, требуется уточнение выбора.",
        }
        return messages.get(state, "Строка требует ручной проверки.")

    @staticmethod
    def _teacher_candidates_from_weekly(session: Session, row: WeeklyLoad) -> list[int]:
        active_ids = {
            teacher.id or 0
            for teacher in session.exec(select(Teacher).where(Teacher.is_active.is_(True))).all()
        }
        candidates: list[int] = []
        for teacher_id in [row.resolved_teacher_id, row.fixed_teacher_id]:
            if teacher_id and teacher_id in active_ids and teacher_id not in candidates:
                candidates.append(teacher_id)
        for raw_id in (row.candidate_teacher_ids or "").split(","):
            if not raw_id:
                continue
            teacher_id = int(raw_id)
            if teacher_id in active_ids and teacher_id not in candidates:
                candidates.append(teacher_id)
        return candidates

    @staticmethod
    def _teacher_candidates_for_subject(session: Session, group_id: int, subject_id: int) -> list[int]:
        fixed = session.exec(
            select(GroupSubjectTeacher).where(
                GroupSubjectTeacher.group_id == group_id,
                GroupSubjectTeacher.subject_id == subject_id,
            )
        ).all()
        if fixed:
            return [item.teacher_id for item in sorted(fixed, key=lambda item: (not item.fixed, item.teacher_id))]
        allowed = session.exec(
            select(TeacherSubject).where(
                TeacherSubject.subject_id == subject_id,
                TeacherSubject.can_teach.is_(True),
            )
        ).all()
        return [item.teacher_id for item in sorted(allowed, key=lambda item: (item.priority, item.teacher_id))]

    @staticmethod
    def _room_candidates(session: Session, subject: Subject) -> list[int | None]:
        from app.models import Room

        if subject.requires_special_room:
            rooms = session.exec(
                select(Room).where(Room.room_type.in_(["computer_lab", "design_lab"]))
            ).all()
            return [room.id or 0 for room in rooms]
        rooms = session.exec(select(Room).order_by(Room.code)).all()
        return [room.id or 0 for room in rooms]
