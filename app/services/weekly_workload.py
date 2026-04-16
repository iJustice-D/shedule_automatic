from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from sqlmodel import Session, delete, select

from app.core.timetable import DELIVERY_OFFLINE, SHIFT_MORNING
from app.models import (
    AcademicPeriod,
    CurriculumLoad,
    Department,
    Group,
    GroupSubjectTeacher,
    Subject,
    Teacher,
    TeacherSubject,
    WeeklyLoad,
)
from app.services.importers.curriculum_xls import CurriculumXlsImporter
from app.services.importers.workload_docx import ImportedWeeklyLoadRow, WeeklyWorkloadDocxImporter


class WeeklyWorkloadService:
    def __init__(self) -> None:
        self.docx_importer = WeeklyWorkloadDocxImporter()
        self.curriculum_importer = CurriculumXlsImporter()

    def import_docx(
        self,
        session: Session,
        docx_path: Path,
        calendar_path: Path | None = None,
        curriculum_path: Path | None = None,
        target_group_codes: list[str] | None = None,
    ) -> list[WeeklyLoad]:
        imported_rows = self.docx_importer.import_rows(docx_path, target_group_codes=target_group_codes)
        if not imported_rows:
            return []

        departments = self._department_map(session)
        reference_map = self._reference_subject_map(curriculum_path) if curriculum_path and curriculum_path.exists() else {}
        delete_query = delete(WeeklyLoad).where(WeeklyLoad.source_file == docx_path.name)
        if target_group_codes:
            existing_group_ids = [
                group.id or 0
                for group in session.exec(select(Group).where(Group.code.in_(list(target_group_codes)))).all()
            ]
            if existing_group_ids:
                delete_query = delete_query.where(WeeklyLoad.group_id.in_(existing_group_ids))
        session.exec(delete_query)
        session.commit()

        created: list[WeeklyLoad] = []
        for row in imported_rows:
            group = self._ensure_group(session, row.group_code, row.course, departments)
            subject = self._ensure_subject(session, row, departments, reference_map)
            concrete_teacher_ids = self._ensure_teachers_and_mappings(session, group, subject, row, departments)
            study_weeks = self._actual_study_weeks(session, group.id or 0, row.semester, row.study_weeks, row.load_category)
            weekly_hours = round(row.total_hours / study_weeks, 2) if study_weeks else row.weekly_hours
            weekly_pairs = round(weekly_hours / 2, 2)
            fixed_teacher_id = concrete_teacher_ids[0] if row.teacher_assignment_type == "fixed" and len(concrete_teacher_ids) == 1 else None
            item = WeeklyLoad(
                group_id=group.id or 0,
                subject_id=subject.id or 0,
                semester=row.semester,
                source_semester_label=row.source_semester_label,
                weekly_hours=weekly_hours,
                weekly_pairs=weekly_pairs,
                total_hours=row.total_hours,
                study_weeks=study_weeks,
                load_category=row.load_category,
                subgroup_code=row.subgroup_code,
                is_facultative=row.is_facultative,
                is_practice=row.is_practice,
                practice_type=row.practice_type,
                source_priority=row.source_priority,
                delivery_mode=DELIVERY_OFFLINE,
                source_file=row.source_file,
                raw_import_notes=row.raw_import_notes,
                raw_teacher_names=row.raw_teacher_names,
                candidate_teacher_ids=",".join(str(item) for item in concrete_teacher_ids),
                assignment_state=row.teacher_assignment_type,
                fixed_teacher_id=fixed_teacher_id,
                resolved_teacher_id=fixed_teacher_id,
                is_active=True,
            )
            session.add(item)
            created.append(item)
        session.commit()
        return session.exec(select(WeeklyLoad).where(WeeklyLoad.source_file == docx_path.name)).all()

    @staticmethod
    def active_rows(
        session: Session,
        semester: int | None = None,
        group_ids: list[int] | None = None,
    ) -> list[WeeklyLoad]:
        query = select(WeeklyLoad).where(WeeklyLoad.is_active.is_(True)).order_by(WeeklyLoad.group_id, WeeklyLoad.subject_id, WeeklyLoad.semester)
        if semester is not None:
            query = query.where(WeeklyLoad.semester == semester)
        if group_ids:
            query = query.where(WeeklyLoad.group_id.in_(group_ids))
        return session.exec(query).all()

    @staticmethod
    def unresolved_rows(session: Session, semester: int | None = None) -> list[WeeklyLoad]:
        query = select(WeeklyLoad).where(
            WeeklyLoad.is_active.is_(True),
            WeeklyLoad.assignment_state.in_(["vacancy", "unresolved_manual_review", "multi_teacher", "candidate_pool"]),
        ).order_by(WeeklyLoad.group_id, WeeklyLoad.subject_id)
        if semester is not None:
            query = query.where(WeeklyLoad.semester == semester)
        return session.exec(query).all()

    @staticmethod
    def teacher_balance_report(session: Session) -> list[dict[str, object]]:
        teachers = {teacher.id: teacher for teacher in session.exec(select(Teacher)).all()}
        grouped: dict[int, dict[int, float]] = defaultdict(lambda: defaultdict(float))
        pending: dict[int, int] = defaultdict(int)
        for row in session.exec(select(WeeklyLoad).where(WeeklyLoad.is_active.is_(True))).all():
            teacher_id = row.resolved_teacher_id or row.fixed_teacher_id
            if teacher_id:
                grouped[teacher_id][row.semester] += row.weekly_pairs
            else:
                for teacher_id in [int(item) for item in row.candidate_teacher_ids.split(",") if item]:
                    pending[teacher_id] += 1
        report: list[dict[str, object]] = []
        for teacher_id, semester_map in grouped.items():
            teacher = teachers.get(teacher_id)
            if teacher is None:
                continue
            sem3 = round(semester_map.get(3, 0.0), 2)
            sem4 = round(semester_map.get(4, 0.0), 2)
            report.append(
                {
                    "teacher_id": teacher_id,
                    "teacher_name": teacher.editable_name or teacher.full_name,
                    "semester_3_pairs": sem3,
                    "semester_4_pairs": sem4,
                    "normalized_balance_score": round(abs(sem3 - sem4), 2),
                    "pending_rows": pending.get(teacher_id, 0),
                }
            )
        return sorted(report, key=lambda item: (-float(item["normalized_balance_score"]), str(item["teacher_name"])))

    def _ensure_group(self, session: Session, group_code: str, course: int | None, departments: dict[str, Department]) -> Group:
        group = session.exec(select(Group).where(Group.code == group_code)).first()
        if group is None:
            group = Group(
                code=group_code,
                name=group_code,
                home_department_id=departments["IT"].id or 0,
                course=course or 2,
                year=course or 2,
                semester=4,
                student_count=25,
                shift=SHIFT_MORNING,
            )
        else:
            if course:
                group.course = course
                group.year = course
        session.add(group)
        session.commit()
        session.refresh(group)
        return group

    def _ensure_subject(
        self,
        session: Session,
        row: ImportedWeeklyLoadRow,
        departments: dict[str, Department],
        reference_map: dict[str, dict[str, object]],
    ) -> Subject:
        key = self._normalize_name(row.subject_name)
        reference = reference_map.get(key, {})
        subject_code = str(reference.get("code") or f"{row.group_code}-{row.subject_name[:18].upper()}").replace(" ", "-")
        subject = session.exec(select(Subject).where(Subject.code == subject_code)).first()
        lesson_type = str(reference.get("lesson_type") or ("practice" if row.is_practice else "mixed"))
        can_be_online = bool(reference.get("can_be_online") or (row.load_category == "facultative"))
        requires_special_room = bool(reference.get("requires_special_room") or row.is_practice)
        owner_code = "SPORT" if "дене" in row.subject_name.lower() else "HUM" if "эконом" in row.subject_name.lower() else "IT"
        if subject is None:
            subject = Subject(
                code=subject_code,
                name=row.subject_name,
                owner_department_id=departments[owner_code].id or 0,
                lesson_type=lesson_type,
                requires_special_room=requires_special_room,
                can_be_online=can_be_online,
                default_delivery_mode=DELIVERY_OFFLINE,
            )
        else:
            subject.name = row.subject_name
            subject.owner_department_id = departments[owner_code].id or 0
            subject.lesson_type = lesson_type
            subject.requires_special_room = requires_special_room
            subject.can_be_online = can_be_online
        session.add(subject)
        session.commit()
        session.refresh(subject)
        return subject

    def _ensure_teachers_and_mappings(
        self,
        session: Session,
        group: Group,
        subject: Subject,
        row: ImportedWeeklyLoadRow,
        departments: dict[str, Department],
    ) -> list[int]:
        teacher_ids: list[int] = []
        for name in row.assigned_teacher_names:
            if name in {"Вакансия", "Өндіріс жетекшісі"}:
                continue
            teacher = session.exec(select(Teacher).where(Teacher.full_name == name)).first()
            if teacher is None:
                teacher = Teacher(
                    full_name=name,
                    short_name=name,
                    editable_name=name,
                    home_department_id=departments["IT"].id or 0,
                    max_weekly_pairs=24,
                )
                session.add(teacher)
                session.commit()
                session.refresh(teacher)
            teacher_ids.append(teacher.id or 0)
            if session.exec(
                select(TeacherSubject).where(
                    TeacherSubject.teacher_id == teacher.id,
                    TeacherSubject.subject_id == subject.id,
                )
            ).first() is None:
                session.add(
                    TeacherSubject(
                        teacher_id=teacher.id or 0,
                        subject_id=subject.id or 0,
                        can_teach=True,
                        priority=1,
                    )
                )
            fixed = row.teacher_assignment_type == "fixed"
            if session.exec(
                select(GroupSubjectTeacher).where(
                    GroupSubjectTeacher.group_id == group.id,
                    GroupSubjectTeacher.subject_id == subject.id,
                    GroupSubjectTeacher.teacher_id == teacher.id,
                )
            ).first() is None:
                session.add(
                    GroupSubjectTeacher(
                        group_id=group.id or 0,
                        subject_id=subject.id or 0,
                        teacher_id=teacher.id or 0,
                        fixed=fixed,
                    )
                )
        session.commit()
        return teacher_ids

    @staticmethod
    def _actual_study_weeks(session: Session, group_id: int, semester: int, fallback: int, load_category: str) -> int:
        if load_category in {"practice", "study_practice", "industrial_practice"}:
            return fallback
        periods = session.exec(
            select(AcademicPeriod).where(
                AcademicPeriod.group_id == group_id,
                AcademicPeriod.semester == semester,
                AcademicPeriod.is_schedulable.is_(True),
            )
        ).all()
        if periods:
            return len(periods)
        return fallback

    @staticmethod
    def _department_map(session: Session) -> dict[str, Department]:
        rows = {item.code: item for item in session.exec(select(Department)).all()}
        if rows:
            return rows
        defaults = {
            "IT": Department(code="IT", name="Информационные технологии"),
            "GEN": Department(code="GEN", name="Общеобразовательные дисциплины"),
            "HUM": Department(code="HUM", name="Гуманитарные дисциплины"),
            "SPORT": Department(code="SPORT", name="Физическая культура"),
        }
        for item in defaults.values():
            session.add(item)
        session.commit()
        return {item.code: item for item in session.exec(select(Department)).all()}

    def _reference_subject_map(self, curriculum_path: Path) -> dict[str, dict[str, object]]:
        rows = self.curriculum_importer.import_group_loads(curriculum_path, semesters=(3, 4))
        result: dict[str, dict[str, object]] = {}
        for row in rows:
            result[self._normalize_name(row.subject_name)] = {
                "code": f"ETB-{row.subject_code}",
                "lesson_type": row.lesson_type,
                "requires_special_room": row.requires_special_room,
                "can_be_online": row.can_be_online,
            }
        return result

    @staticmethod
    def _normalize_name(value: str) -> str:
        return " ".join(value.lower().replace("-", " ").split())
