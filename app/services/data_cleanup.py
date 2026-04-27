from __future__ import annotations

import re
from collections import defaultdict

from sqlmodel import Session, select

from app.models import (
    AcademicPeriod,
    CurriculumLoad,
    Group,
    GroupSubjectTeacher,
    OnlinePolicy,
    ScheduleEntry,
    Subject,
    Teacher,
    TeacherSubject,
    WeeklyLoad,
)


SPACE_RE = re.compile(r"\s+")


def normalize_visible_name(value: str) -> str:
    cleaned = SPACE_RE.sub(" ", (value or "").strip()).lower()
    cleaned = cleaned.replace("–", "-").replace("—", "-").replace("ё", "е")
    return cleaned


class DataCleanupService:
    def list_groups(self, session: Session, include_inactive: bool = False) -> list[Group]:
        query = select(Group)
        if not include_inactive:
            query = query.where(Group.is_active.is_(True))
        return session.exec(query.order_by(Group.code)).all()

    def list_teachers(self, session: Session, include_inactive: bool = False) -> list[Teacher]:
        query = select(Teacher)
        if not include_inactive:
            query = query.where(Teacher.is_active.is_(True))
        return session.exec(query.order_by(Teacher.full_name)).all()

    def list_subjects(
        self,
        session: Session,
        include_inactive: bool = False,
        include_duplicates: bool = False,
    ) -> list[Subject]:
        query = select(Subject)
        if not include_inactive:
            query = query.where(Subject.is_active.is_(True))
        if not include_duplicates:
            query = query.where(Subject.canonical_subject_id.is_(None))
        return session.exec(query.order_by(Subject.name, Subject.code)).all()

    def cleanup(self, session: Session, *, hide_demo: bool = True) -> None:
        self._normalize_subject_names(session)
        self._merge_duplicate_subjects(session)
        if hide_demo:
            self._mark_group_states(session)
            self._mark_subject_states(session)
            self._mark_teacher_states(session)
        session.commit()

    def _normalize_subject_names(self, session: Session) -> None:
        subjects = session.exec(select(Subject)).all()
        for subject in subjects:
            subject.normalized_name = normalize_visible_name(subject.name)
            session.add(subject)
        session.commit()

    def _merge_duplicate_subjects(self, session: Session) -> None:
        subjects = session.exec(select(Subject).order_by(Subject.id)).all()
        clusters: dict[str, list[Subject]] = defaultdict(list)
        for subject in subjects:
            subject.normalized_name = normalize_visible_name(subject.name)
            clusters[subject.normalized_name].append(subject)

        for normalized_name, cluster in clusters.items():
            if not normalized_name or len(cluster) < 2:
                continue
            canonical = self._select_canonical_subject(session, cluster)
            canonical.canonical_subject_id = None
            canonical.is_active = True
            session.add(canonical)
            for duplicate in cluster:
                if duplicate.id == canonical.id:
                    continue
                self._relink_subject_references(session, duplicate.id or 0, canonical.id or 0)
                duplicate.is_active = False
                duplicate.canonical_subject_id = canonical.id
                session.add(duplicate)
        session.commit()

    def _select_canonical_subject(self, session: Session, cluster: list[Subject]) -> Subject:
        def score(subject: Subject) -> tuple[int, int, int]:
            refs = self._subject_reference_count(session, subject.id or 0)
            return (
                0 if not subject.is_demo else 1,
                -refs,
                subject.id or 0,
            )

        return sorted(cluster, key=score)[0]

    @staticmethod
    def _subject_reference_count(session: Session, subject_id: int) -> int:
        return (
            len(session.exec(select(CurriculumLoad).where(CurriculumLoad.subject_id == subject_id)).all())
            + len(session.exec(select(WeeklyLoad).where(WeeklyLoad.subject_id == subject_id, WeeklyLoad.is_active.is_(True))).all())
            + len(session.exec(select(ScheduleEntry).where(ScheduleEntry.subject_id == subject_id)).all())
            + len(session.exec(select(GroupSubjectTeacher).where(GroupSubjectTeacher.subject_id == subject_id)).all())
            + len(session.exec(select(TeacherSubject).where(TeacherSubject.subject_id == subject_id)).all())
            + len(session.exec(select(OnlinePolicy).where(OnlinePolicy.subject_id == subject_id)).all())
        )

    def _relink_subject_references(self, session: Session, old_subject_id: int, new_subject_id: int) -> None:
        if old_subject_id == new_subject_id:
            return

        for load in session.exec(select(CurriculumLoad).where(CurriculumLoad.subject_id == old_subject_id)).all():
            conflict = session.exec(
                select(CurriculumLoad).where(
                    CurriculumLoad.group_id == load.group_id,
                    CurriculumLoad.subject_id == new_subject_id,
                    CurriculumLoad.semester == load.semester,
                    CurriculumLoad.source_type == load.source_type,
                )
            ).first()
            if conflict is None:
                load.subject_id = new_subject_id
                session.add(load)
            else:
                session.delete(load)

        for load in session.exec(select(WeeklyLoad).where(WeeklyLoad.subject_id == old_subject_id)).all():
            conflict = session.exec(
                select(WeeklyLoad).where(
                    WeeklyLoad.group_id == load.group_id,
                    WeeklyLoad.subject_id == new_subject_id,
                    WeeklyLoad.semester == load.semester,
                    WeeklyLoad.source_semester_label == load.source_semester_label,
                    WeeklyLoad.subgroup_code == load.subgroup_code,
                    WeeklyLoad.source_file == load.source_file,
                )
            ).first()
            if conflict is None:
                load.subject_id = new_subject_id
                session.add(load)
            else:
                session.delete(load)

        for entry in session.exec(select(ScheduleEntry).where(ScheduleEntry.subject_id == old_subject_id)).all():
            entry.subject_id = new_subject_id
            session.add(entry)

        for mapping in session.exec(select(GroupSubjectTeacher).where(GroupSubjectTeacher.subject_id == old_subject_id)).all():
            conflict = session.exec(
                select(GroupSubjectTeacher).where(
                    GroupSubjectTeacher.group_id == mapping.group_id,
                    GroupSubjectTeacher.subject_id == new_subject_id,
                    GroupSubjectTeacher.teacher_id == mapping.teacher_id,
                )
            ).first()
            if conflict is None:
                mapping.subject_id = new_subject_id
                session.add(mapping)
            else:
                if mapping.fixed:
                    conflict.fixed = True
                    session.add(conflict)
                session.delete(mapping)

        for mapping in session.exec(select(TeacherSubject).where(TeacherSubject.subject_id == old_subject_id)).all():
            conflict = session.exec(
                select(TeacherSubject).where(
                    TeacherSubject.teacher_id == mapping.teacher_id,
                    TeacherSubject.subject_id == new_subject_id,
                )
            ).first()
            if conflict is None:
                mapping.subject_id = new_subject_id
                session.add(mapping)
            else:
                conflict.can_teach = conflict.can_teach or mapping.can_teach
                conflict.priority = min(conflict.priority, mapping.priority)
                session.add(conflict)
                session.delete(mapping)

        for policy in session.exec(select(OnlinePolicy).where(OnlinePolicy.subject_id == old_subject_id)).all():
            conflict = session.exec(
                select(OnlinePolicy).where(
                    OnlinePolicy.subject_id == new_subject_id,
                    OnlinePolicy.course == policy.course,
                    OnlinePolicy.group_id == policy.group_id,
                )
            ).first()
            if conflict is None:
                policy.subject_id = new_subject_id
                session.add(policy)
            else:
                session.delete(policy)

    def _mark_group_states(self, session: Session) -> None:
        groups = session.exec(select(Group)).all()
        real_weekly_group_ids = {row.group_id for row in session.exec(select(WeeklyLoad).where(WeeklyLoad.is_active.is_(True))).all()}
        imported_curriculum_group_ids = {
            row.group_id
            for row in session.exec(select(CurriculumLoad).where(CurriculumLoad.source_type != "demo")).all()
        }
        scheduled_group_ids = {self._scalar_id(row) for row in session.exec(select(ScheduleEntry.group_id).distinct()).all()}
        manual_group_ids = {
            row.group_id
            for row in session.exec(select(CurriculumLoad).where(CurriculumLoad.source_type == "manual")).all()
        }
        calendar_group_ids = {self._scalar_id(row) for row in session.exec(select(AcademicPeriod.group_id).distinct()).all()}

        for group in groups:
            has_real_link = (
                (group.id or 0) in real_weekly_group_ids
                or (group.id or 0) in imported_curriculum_group_ids
                or (group.id or 0) in scheduled_group_ids
                or (group.id or 0) in manual_group_ids
            )
            has_calendar = (group.id or 0) in calendar_group_ids
            demo_only = not has_real_link and has_calendar and not group.is_manual
            if group.code in {"DTP-2201", "ETB-2202", "IS-2201"} and (group.id or 0) not in real_weekly_group_ids:
                group.is_demo = True
            group.is_active = bool(has_real_link or group.is_manual or ((group.id or 0) in real_weekly_group_ids))
            if demo_only:
                group.is_active = False
            session.add(group)

    def _mark_subject_states(self, session: Session) -> None:
        subjects = session.exec(select(Subject)).all()
        real_subject_ids = {row.subject_id for row in session.exec(select(WeeklyLoad).where(WeeklyLoad.is_active.is_(True))).all()}
        manual_subject_ids = {
            row.subject_id
            for row in session.exec(select(CurriculumLoad).where(CurriculumLoad.source_type == "manual")).all()
        }
        scheduled_subject_ids = {self._scalar_id(row) for row in session.exec(select(ScheduleEntry.subject_id).distinct()).all()}
        for subject in subjects:
            subject.normalized_name = normalize_visible_name(subject.name)
            if subject.code.endswith("-SUB-1") or "-SUB-" in subject.code:
                subject.is_demo = True
            is_canonical = subject.canonical_subject_id is None
            subject.is_active = is_canonical and (
                (subject.id or 0) in real_subject_ids
                or (subject.id or 0) in manual_subject_ids
                or (subject.id or 0) in scheduled_subject_ids
                or not subject.is_demo
            )
            session.add(subject)

    def _mark_teacher_states(self, session: Session) -> None:
        teachers = session.exec(select(Teacher)).all()
        active_group_ids = {group.id or 0 for group in session.exec(select(Group).where(Group.is_active.is_(True))).all()}
        active_teacher_ids = {self._scalar_id(row) for row in session.exec(select(ScheduleEntry.teacher_id).distinct()).all()}
        for row in session.exec(select(WeeklyLoad).where(WeeklyLoad.is_active.is_(True), WeeklyLoad.group_id.in_(active_group_ids))).all():
            for teacher_id in [row.resolved_teacher_id, row.fixed_teacher_id]:
                if teacher_id:
                    active_teacher_ids.add(teacher_id)
            for raw_id in (row.candidate_teacher_ids or "").split(","):
                if raw_id:
                    active_teacher_ids.add(int(raw_id))

        for teacher in teachers:
            if teacher.full_name in {
                "Maksat Nurpeisov",
                "Aliya Serik",
                "Dana Kairatova",
                "Aidos Zhaparov",
                "Marzhan Ibragim",
                "Saule Tolegenova",
                "Aiman Abdullaeva",
                "Askar Beket",
                "Gulnar Omarova",
                "Erlan Saparbayev",
            }:
                teacher.is_demo = True
            teacher.is_active = bool((teacher.id or 0) in active_teacher_ids or teacher.is_manual)
            session.add(teacher)

        inactive_teacher_ids = {teacher.id or 0 for teacher in teachers if not teacher.is_active}
        if inactive_teacher_ids:
            for mapping in session.exec(select(GroupSubjectTeacher).where(GroupSubjectTeacher.teacher_id.in_(inactive_teacher_ids))).all():
                session.delete(mapping)
            for mapping in session.exec(select(TeacherSubject).where(TeacherSubject.teacher_id.in_(inactive_teacher_ids))).all():
                session.delete(mapping)

    @staticmethod
    def _scalar_id(value: object) -> int:
        if isinstance(value, tuple):
            return int(value[0])
        return int(value)
