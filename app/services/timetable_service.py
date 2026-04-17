from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path

from sqlmodel import Session, select

from app.core.config import settings
from app.core.timetable import (
    DAYS,
    DELIVERY_HYBRID,
    DELIVERY_OFFLINE,
    DELIVERY_ONLINE,
    LESSON_MODE_ONLINE,
    LESSON_MODE_REGULAR,
    ONLINE_ALLOWED_DAYS,
    SHIFT_AFTERNOON,
    SHIFT_MORNING,
    SLOT_CATEGORY_ONLINE_EXTRA,
    SLOT_CATEGORY_REGULAR,
    allowed_pairs_for_shift,
    apply_timeslot_to_entry,
    intervals_overlap,
    online_slot_matches_day,
    online_slot_numbers,
    pair_allowed_for_shift,
    room_required,
)
from app.core.week_scope import decode_week_scope, scopes_overlap
from app.db.session import engine
from app.models import (
    AcademicPeriod,
    AppSetting,
    ChangeLog,
    Conflict,
    CurriculumLoad,
    GenerationJob,
    Group,
    GroupSubjectTeacher,
    OnlineSlot,
    OnlinePolicy,
    Room,
    Schedule,
    ScheduleEntry,
    Subject,
    Suggestion,
    Teacher,
    TeacherSubject,
    WeeklyLoad,
)
from app.services.ai.dummy import DummyExplanationProvider
from app.services.ai.gemini_provider import GeminiExplanationProvider
from app.services.conflicts.engine import ConflictEngine
from app.services.exporters.context import build_schedule_context
from app.services.exporters.docx_exporter import DocxExporter
from app.services.exporters.pdf_exporter import PdfExporter
from app.services.exporters.xlsx_exporter import XlsxExporter
from app.services.online_policy import OnlinePolicyService
from app.services.online_slots import OnlineSlotService
from app.services.scheduler.generator import HybridScheduleGenerator
from app.services.suggestions.engine import SuggestionEngine
from app.services.weekly_workload import WeeklyWorkloadService


class TimetableService:
    def __init__(self) -> None:
        self.engine = engine
        self.generator = HybridScheduleGenerator()
        self.conflict_engine = ConflictEngine()
        self.suggestion_engine = SuggestionEngine()
        self.xlsx_exporter = XlsxExporter()
        self.pdf_exporter = PdfExporter()
        self.docx_exporter = DocxExporter()
        self.online_policy_service = OnlinePolicyService()
        self.online_slot_service = OnlineSlotService()
        self.weekly_workload_service = WeeklyWorkloadService()

    def generate_schedule(
        self,
        session: Session,
        semester: int,
        group_codes: list[str] | None = None,
        name: str | None = None,
        include_facultatives: bool = False,
        enable_online: bool = True,
    ) -> Schedule:
        schedule = self.generator.generate(
            session,
            semester=semester,
            group_codes=group_codes,
            schedule_name=name,
            include_facultatives=include_facultatives,
            enable_online=enable_online,
        )
        self.revalidate_schedule(session, schedule.id or 0)
        return session.get(Schedule, schedule.id)

    @staticmethod
    def _utcnow() -> datetime:
        return datetime.now(timezone.utc)

    def create_generation_job(
        self,
        session: Session,
        *,
        group_id: int,
        semester: int,
        requested_name: str = "",
        generation_mode: str = "best_effort",
        include_facultatives: bool = False,
        enable_online: bool = True,
        source_scope: str = "normalized_weekly",
    ) -> GenerationJob:
        self.engine = session.get_bind()
        group = session.get(Group, group_id)
        if group is None:
            raise ValueError("Выбранная группа не найдена.")
        job = GenerationJob(
            group_id=group_id,
            semester=semester,
            requested_name=requested_name,
            generation_mode=generation_mode,
            include_facultatives=include_facultatives,
            enable_online=enable_online,
            source_scope=source_scope,
            status="pending",
            progress_percent=0,
            summary_message="Подготовка данных",
        )
        session.add(job)
        session.commit()
        session.refresh(job)
        return job

    def list_generation_jobs(
        self,
        session: Session,
        *,
        group_id: int | None = None,
        semester: int | None = None,
        limit: int = 20,
    ) -> list[GenerationJob]:
        query = select(GenerationJob).order_by(GenerationJob.created_at.desc())
        if group_id is not None:
            query = query.where(GenerationJob.group_id == group_id)
        if semester is not None:
            query = query.where(GenerationJob.semester == semester)
        return session.exec(query.limit(limit)).all()

    def latest_result_for_scope(self, session: Session, *, group_id: int, semester: int) -> Schedule | None:
        job = session.exec(
            select(GenerationJob).where(
                GenerationJob.group_id == group_id,
                GenerationJob.semester == semester,
                GenerationJob.status == "completed",
                GenerationJob.result_schedule_id.is_not(None),
            ).order_by(GenerationJob.finished_at.desc(), GenerationJob.created_at.desc())
        ).first()
        if job is None or not job.result_schedule_id:
            return None
        return session.get(Schedule, job.result_schedule_id)

    def get_generation_job(self, session: Session, job_id: int) -> GenerationJob | None:
        return session.get(GenerationJob, job_id)

    def run_generation_job(self, job_id: int) -> GenerationJob:
        try:
            self._update_job(job_id, status="running", progress=5, message="Подготовка данных", started=True)
            with Session(self.engine) as session:
                job = session.get(GenerationJob, job_id)
                if job is None:
                    raise ValueError("Запуск генерации не найден.")
                group = session.get(Group, job.group_id)
                if group is None:
                    raise ValueError("Выбранная группа не найдена.")

                self._update_job(job_id, progress=20, message="Нормализация нагрузки")
                weekly_rows = self.weekly_workload_service.active_rows(session, semester=job.semester, group_ids=[group.id or 0])
                manual_rows = session.exec(
                    select(CurriculumLoad).where(
                        CurriculumLoad.group_id == group.id,
                        CurriculumLoad.semester == job.semester,
                    )
                ).all()
                relevant_weekly = [
                    row
                    for row in weekly_rows
                    if not row.is_practice and (job.include_facultatives or not row.is_facultative)
                ]
                if job.source_scope == "normalized_weekly" and not weekly_rows and not manual_rows:
                    raise ValueError("Невозможно запустить генерацию: отсутствуют нормализованные данные.")
                if not relevant_weekly and not manual_rows:
                    raise ValueError("Для выбранной группы нет учебной нагрузки.")

                study_weeks = session.exec(
                    select(AcademicPeriod).where(
                        AcademicPeriod.group_id == group.id,
                        AcademicPeriod.semester == job.semester,
                        AcademicPeriod.is_schedulable.is_(True),
                    )
                ).all()
                if not study_weeks:
                    raise ValueError("Для выбранного семестра нет доступных учебных недель.")
                if not group.shift:
                    raise ValueError("Невозможно запустить генерацию: для группы не указана смена.")

                self._update_job(job_id, progress=35, message="Проверка выполнимости")
                feasibility_message = self._feasibility_message(session, group, job.semester, relevant_weekly, manual_rows, job.enable_online)
                self._update_job(job_id, progress=50, message="Генерация расписания")
                schedule = self.generate_schedule(
                    session,
                    semester=job.semester,
                    group_codes=[group.code],
                    name=job.requested_name or f"Расписание {group.code} семестр {job.semester}",
                    include_facultatives=job.include_facultatives,
                    enable_online=job.enable_online,
                )

                self._update_job(job_id, progress=80, message="Локальная оптимизация")
                diagnostics = self.result_diagnostics(session, schedule.id or 0, group.id or 0)
                summary = self._job_summary(group, job.semester, diagnostics, feasibility_message)
                self._update_job(
                    job_id,
                    status="completed",
                    progress=100,
                    message=summary,
                    finished=True,
                    result_schedule_id=schedule.id or 0,
                )
        except Exception as exc:
            self._update_job(job_id, status="failed", progress=100, message=str(exc), finished=True)
        with Session(self.engine) as session:
            job = session.get(GenerationJob, job_id)
            if job is None:
                raise ValueError("Запуск генерации не найден.")
            return job

    def revalidate_schedule(self, session: Session, schedule_id: int) -> tuple[list[Conflict], list[Suggestion]]:
        schedule = session.get(Schedule, schedule_id)
        if schedule is None:
            raise ValueError("Расписание не найдено.")
        conflicts = self.conflict_engine.refresh(session, schedule)
        suggestions = self.suggestion_engine.refresh(session, schedule)
        return conflicts, suggestions

    def update_entry(self, session: Session, entry_id: int, payload: dict) -> ScheduleEntry:
        entry = session.get(ScheduleEntry, entry_id)
        if entry is None:
            raise ValueError("Запись расписания не найдена.")
        group = session.get(Group, entry.group_id)
        before = entry.model_dump()
        requested_day_of_week = payload.get("day_of_week")
        if payload.get("rename_teacher_to"):
            teacher = session.get(Teacher, entry.teacher_id)
            teacher.editable_name = payload["rename_teacher_to"]
            teacher.full_name = payload["rename_teacher_to"]
            teacher.short_name = payload["rename_teacher_to"]
            session.add(teacher)
        if payload.get("reassign_teacher_id"):
            reassign_teacher_id = payload["reassign_teacher_id"]
            mapping = session.exec(
                select(GroupSubjectTeacher).where(
                    GroupSubjectTeacher.group_id == entry.group_id,
                    GroupSubjectTeacher.subject_id == entry.subject_id,
                    GroupSubjectTeacher.teacher_id == entry.teacher_id,
                )
            ).first()
            if mapping:
                mapping.teacher_id = reassign_teacher_id
                mapping.fixed = True
                session.add(mapping)
            else:
                session.add(
                    GroupSubjectTeacher(
                        group_id=entry.group_id,
                        subject_id=entry.subject_id,
                        teacher_id=reassign_teacher_id,
                        fixed=True,
                    )
                )
            teacher_subject = session.exec(
                select(TeacherSubject).where(
                    TeacherSubject.teacher_id == reassign_teacher_id,
                    TeacherSubject.subject_id == entry.subject_id,
                )
            ).first()
            if teacher_subject is None:
                teacher_subject = TeacherSubject(
                    teacher_id=reassign_teacher_id,
                    subject_id=entry.subject_id,
                    can_teach=True,
                    priority=99,
                )
            else:
                teacher_subject.can_teach = True
            session.add(teacher_subject)
            entry.teacher_id = reassign_teacher_id
        if payload.get("subject_id") is not None:
            entry.subject_id = payload["subject_id"]
        for field in ("day_of_week", "pair_number", "teacher_id", "locked", "online_slot_number", "lesson_mode"):
            if payload.get(field) is not None:
                setattr(entry, field, payload[field])
        if "delivery_mode" in payload and payload["delivery_mode"] is not None:
            entry.delivery_mode = payload["delivery_mode"]
        if "room_id" in payload:
            entry.room_id = payload["room_id"] or None
        if entry.lesson_mode == LESSON_MODE_ONLINE:
            entry.slot_category = SLOT_CATEGORY_ONLINE_EXTRA
            entry.delivery_mode = DELIVERY_ONLINE
            entry.room_id = None
        else:
            entry.lesson_mode = LESSON_MODE_REGULAR
            entry.slot_category = SLOT_CATEGORY_REGULAR
            if entry.delivery_mode == DELIVERY_ONLINE:
                entry.delivery_mode = DELIVERY_OFFLINE
        apply_timeslot_to_entry(entry)
        if entry.lesson_mode == LESSON_MODE_ONLINE:
            self.online_slot_service.apply_to_entry(session, entry)
        self._validate_slot_rules(session, group, entry, requested_day_of_week=requested_day_of_week)
        if entry.lesson_mode == LESSON_MODE_REGULAR and room_required(entry.delivery_mode) and entry.room_id is None and payload.get("room_id") is None:
            available_room = self._find_available_room(session, entry)
            entry.room_id = available_room.id if available_room else None
        self._validate_teacher_assignment(session, entry)
        self._validate_slot_availability(session, entry)
        if entry.lesson_mode == LESSON_MODE_REGULAR and room_required(entry.delivery_mode) and entry.room_id is None:
            raise ValueError("Для очного или гибридного занятия должна быть указана аудитория.")
        session.add(entry)
        session.commit()
        session.refresh(entry)
        session.add(
            ChangeLog(
                schedule_id=entry.schedule_id,
                action_type="update_entry",
                before_json=json.dumps(before, default=str),
                after_json=json.dumps(entry.model_dump(), default=str),
            )
        )
        session.commit()
        self.revalidate_schedule(session, entry.schedule_id)
        return entry

    def create_teacher(self, session: Session, full_name: str, short_name: str | None, home_department_id: int, max_weekly_pairs: int) -> Teacher:
        teacher = Teacher(
            full_name=full_name,
            short_name=short_name or full_name,
            home_department_id=home_department_id,
            editable_name=full_name,
            max_weekly_pairs=max_weekly_pairs,
        )
        session.add(teacher)
        session.commit()
        session.refresh(teacher)
        return teacher

    def rename_teacher(self, session: Session, teacher_id: int, name: str) -> Teacher:
        teacher = session.get(Teacher, teacher_id)
        if teacher is None:
            raise ValueError("Преподаватель не найден.")
        teacher.full_name = name
        teacher.short_name = name
        teacher.editable_name = name
        session.add(teacher)
        session.commit()
        session.refresh(teacher)
        return teacher

    def create_group(self, session: Session, payload: dict) -> Group:
        self._validate_group_payload(session, payload)
        existing = session.exec(select(Group).where(Group.code == payload["code"])).first()
        if existing is not None:
            raise ValueError("Группа с таким кодом уже существует.")
        group = Group(**payload)
        session.add(group)
        session.commit()
        session.refresh(group)
        return group

    def update_group(self, session: Session, group_id: int, payload: dict) -> Group:
        group = session.get(Group, group_id)
        if group is None:
            raise ValueError("Группа не найдена.")
        self._validate_group_payload(session, payload, current_group_id=group_id)
        for key, value in payload.items():
            setattr(group, key, value)
        session.add(group)
        session.commit()
        session.refresh(group)
        return group

    def delete_group(self, session: Session, group_id: int) -> None:
        group = session.get(Group, group_id)
        if group is None:
            raise ValueError("Группа не найдена.")
        if session.exec(select(CurriculumLoad).where(CurriculumLoad.group_id == group_id)).first() is not None:
            raise ValueError("Нельзя удалить группу, пока для нее есть учебная нагрузка.")
        if session.exec(select(WeeklyLoad).where(WeeklyLoad.group_id == group_id, WeeklyLoad.is_active.is_(True))).first() is not None:
            raise ValueError("Нельзя удалить группу, пока для нее есть импортированная недельная нагрузка.")
        if session.exec(select(ScheduleEntry).where(ScheduleEntry.group_id == group_id)).first() is not None:
            raise ValueError("Нельзя удалить группу, пока она используется в расписании.")
        session.delete(group)
        session.commit()

    def create_subject(self, session: Session, payload: dict) -> Subject:
        self._validate_subject_payload(session, payload)
        existing = session.exec(select(Subject).where(Subject.code == payload["code"])).first()
        if existing is not None:
            raise ValueError("Предмет с таким кодом уже существует.")
        subject = Subject(**payload)
        session.add(subject)
        session.commit()
        session.refresh(subject)
        return subject

    def update_subject(self, session: Session, subject_id: int, payload: dict) -> Subject:
        subject = session.get(Subject, subject_id)
        if subject is None:
            raise ValueError("Предмет не найден.")
        self._validate_subject_payload(session, payload, current_subject_id=subject_id)
        for key, value in payload.items():
            setattr(subject, key, value)
        session.add(subject)
        session.commit()
        session.refresh(subject)
        return subject

    def delete_subject(self, session: Session, subject_id: int) -> None:
        subject = session.get(Subject, subject_id)
        if subject is None:
            raise ValueError("Предмет не найден.")
        if session.exec(select(CurriculumLoad).where(CurriculumLoad.subject_id == subject_id)).first() is not None:
            raise ValueError("Нельзя удалить предмет, пока для него есть учебная нагрузка.")
        if session.exec(select(WeeklyLoad).where(WeeklyLoad.subject_id == subject_id, WeeklyLoad.is_active.is_(True))).first() is not None:
            raise ValueError("Нельзя удалить предмет, пока для него есть импортированная недельная нагрузка.")
        if session.exec(select(ScheduleEntry).where(ScheduleEntry.subject_id == subject_id)).first() is not None:
            raise ValueError("Нельзя удалить предмет, пока он используется в расписании.")
        session.delete(subject)
        session.commit()

    def create_curriculum_load(self, session: Session, payload: dict) -> CurriculumLoad:
        self._validate_load_payload(payload)
        load = CurriculumLoad(**payload)
        session.add(load)
        session.commit()
        session.refresh(load)
        return load

    def update_curriculum_load(self, session: Session, load_id: int, payload: dict) -> CurriculumLoad:
        load = session.get(CurriculumLoad, load_id)
        if load is None:
            raise ValueError("Запись учебной нагрузки не найдена.")
        self._validate_load_payload(payload)
        for key, value in payload.items():
            setattr(load, key, value)
        session.add(load)
        session.commit()
        session.refresh(load)
        return load

    def delete_curriculum_load(self, session: Session, load_id: int) -> None:
        load = session.get(CurriculumLoad, load_id)
        if load is None:
            raise ValueError("Запись учебной нагрузки не найдена.")
        session.delete(load)
        session.commit()

    def export_schedule(self, session: Session, schedule_id: int, export_format: str) -> Path:
        schedule = session.get(Schedule, schedule_id)
        if schedule is None:
            raise ValueError("Расписание не найдено.")
        slug = schedule.name.lower().replace(" ", "_")
        suffix = {"xlsx": ".xlsx", "pdf": ".pdf", "docx": ".docx"}[export_format]
        output_path = settings.exports_dir / f"{slug}_{schedule_id}{suffix}"
        if export_format == "xlsx":
            return self.xlsx_exporter.export(session, schedule_id, output_path)
        if export_format == "pdf":
            return self.pdf_exporter.export(session, schedule_id, output_path)
        if export_format == "docx":
            return self.docx_exporter.export(session, schedule_id, output_path)
        raise ValueError(f"Неподдерживаемый формат экспорта: {export_format}")

    def explain_conflict(self, session: Session, conflict_id: int) -> str:
        conflict = session.get(Conflict, conflict_id)
        if conflict is None:
            raise ValueError("Конфликт не найден.")
        suggestions = session.exec(select(Suggestion).where(Suggestion.conflict_id == conflict.id)).all()
        provider = self._provider(session)
        return provider.explain_conflict(conflict, suggestions)

    def upsert_course_online_target(self, session: Session, course: int, target: int, note: str = "") -> OnlinePolicy:
        return self.online_policy_service.upsert_course_target(session, course, target, note=note)

    def upsert_group_online_target(self, session: Session, group_id: int, target: int, note: str = "") -> OnlinePolicy:
        return self.online_policy_service.upsert_group_target(session, group_id, target, note=note)

    def upsert_subject_online_policy(
        self,
        session: Session,
        subject_id: int,
        allow_online: bool,
        course: int | None = None,
        group_id: int | None = None,
        note: str = "",
    ) -> OnlinePolicy:
        subject = session.get(Subject, subject_id)
        if subject is None:
            raise ValueError("Предмет не найден.")
        if course is None and group_id is None:
            subject.can_be_online = allow_online
        elif allow_online:
            subject.can_be_online = True
        session.add(subject)
        session.commit()
        return self.online_policy_service.upsert_subject_policy(
            session,
            subject_id=subject_id,
            allow_online=allow_online,
            course=course,
            group_id=group_id,
            note=note,
        )

    def import_weekly_workload(
        self,
        session: Session,
        docx_path: Path,
        calendar_path: Path | None = None,
        curriculum_path: Path | None = None,
        group_codes: list[str] | None = None,
    ) -> list[WeeklyLoad]:
        if not docx_path.exists():
            raise ValueError("Файл недельной нагрузки не найден.")
        return self.weekly_workload_service.import_docx(
            session,
            docx_path,
            calendar_path=calendar_path,
            curriculum_path=curriculum_path,
            target_group_codes=group_codes,
        )

    def teacher_balance_report(self, session: Session) -> list[dict[str, object]]:
        return self.weekly_workload_service.teacher_balance_report(session)

    def unresolved_weekly_rows(self, session: Session, semester: int | None = None) -> list[WeeklyLoad]:
        return self.weekly_workload_service.unresolved_rows(session, semester=semester)

    def _update_job(
        self,
        job_id: int,
        *,
        status: str | None = None,
        progress: int | None = None,
        message: str | None = None,
        started: bool = False,
        finished: bool = False,
        result_schedule_id: int | None = None,
    ) -> None:
        with Session(self.engine) as session:
            job = session.get(GenerationJob, job_id)
            if job is None:
                return
            if status is not None:
                job.status = status
            if progress is not None:
                job.progress_percent = progress
            if message is not None:
                job.summary_message = message
            if started and job.started_at is None:
                job.started_at = self._utcnow()
            if finished:
                job.finished_at = self._utcnow()
            if result_schedule_id is not None:
                job.result_schedule_id = result_schedule_id
            session.add(job)
            session.commit()

    def _feasibility_message(
        self,
        session: Session,
        group: Group,
        semester: int,
        weekly_rows: list[WeeklyLoad],
        manual_rows: list[CurriculumLoad],
        enable_online: bool,
    ) -> str:
        required_regular_pairs = round(
            sum(row.weekly_pairs for row in weekly_rows if row.delivery_mode != DELIVERY_ONLINE) + sum(load.pairs_per_week for load in manual_rows),
            2,
        )
        available_regular_slots = len(allowed_pairs_for_shift(group.shift)) * len(DAYS)
        available_online_slots = len(self.online_slot_service.active_slots(session)) if enable_online else 0
        unresolved_rows = sum(
            1
            for row in weekly_rows
            if row.assignment_state in {"vacancy", "candidate_pool", "multi_teacher", "unresolved_manual_review"}
        )
        parts = [
            f"Ожидалось пар: {required_regular_pairs}",
            f"Доступно обычных слотов: {available_regular_slots}",
            f"Доступно онлайн-слотов: {available_online_slots}",
        ]
        if unresolved_rows:
            parts.append(f"Неразрешённых строк: {unresolved_rows}")
        if required_regular_pairs > available_regular_slots + available_online_slots:
            parts.append(
                f"Не хватает пар: {round(required_regular_pairs - available_regular_slots - available_online_slots, 2)}"
            )
        return ". ".join(parts)

    @staticmethod
    def _job_summary(group: Group, semester: int, diagnostics: dict[str, object], feasibility_message: str) -> str:
        summary = diagnostics["summary"]
        base = (
            f"{group.code}, семестр {semester}: полностью размещено {summary['fully_placed_subjects_count']} из "
            f"{summary['expected_subjects_count']} предметов."
        )
        if summary["total_missing_pairs"]:
            base += f" Не хватает пар: {summary['total_missing_pairs']}."
        if summary["hard_conflicts_count"]:
            base += f" Жёстких конфликтов: {summary['hard_conflicts_count']}."
        return f"{base} {feasibility_message}".strip()

    def result_diagnostics(self, session: Session, schedule_id: int, group_id: int | None = None) -> dict[str, object]:
        schedule = session.get(Schedule, schedule_id)
        if schedule is None:
            raise ValueError("Расписание не найдено.")
        scoped_group_ids = self._schedule_group_ids(session, schedule)
        if group_id is not None:
            scoped_group_ids = [group_id]
        entries_query = select(ScheduleEntry).where(ScheduleEntry.schedule_id == schedule_id)
        if scoped_group_ids:
            entries_query = entries_query.where(ScheduleEntry.group_id.in_(scoped_group_ids))
        entries = session.exec(entries_query).all()
        entry_map = {entry.id or 0: entry for entry in entries}
        group_map = {group.id: group for group in session.exec(select(Group)).all()}
        subject_map = {subject.id: subject for subject in session.exec(select(Subject)).all()}

        conflicts = session.exec(select(Conflict).where(Conflict.schedule_id == schedule_id)).all()
        scoped_conflicts = [
            conflict
            for conflict in conflicts
            if self._conflict_matches_groups(conflict, scoped_group_ids, entry_map)
        ]
        hard_conflicts = [
            conflict
            for conflict in scoped_conflicts
            if conflict.type != "unscheduled_load" and conflict.severity == "hard"
        ]
        unscheduled_conflicts = [conflict for conflict in scoped_conflicts if conflict.type == "unscheduled_load"]
        warning_conflicts = [conflict for conflict in scoped_conflicts if conflict.severity != "hard"]

        expected_rows = self._expected_subject_rows(session, schedule.semester, scoped_group_ids)
        grouped_entries: dict[tuple[int, int, str], list[ScheduleEntry]] = defaultdict(list)
        for entry in entries:
            grouped_entries[(entry.group_id, entry.subject_id, entry.subgroup_code or "")].append(entry)

        regular_capacities = self._group_regular_capacity(session, scoped_group_ids, schedule.semester)
        online_targets = {group_id_: self.online_policy_service.get_target_for_group(session, group_map[group_id_]) for group_id_ in scoped_group_ids if group_id_ in group_map}
        online_counts = defaultdict(int)
        for entry in entries:
            if entry.lesson_mode == LESSON_MODE_ONLINE:
                online_counts[entry.group_id] += 1

        subject_rows: list[dict[str, object]] = []
        normalization_issues: list[dict[str, object]] = []
        summary = {
            "selected_group": group_map[scoped_group_ids[0]].code if len(scoped_group_ids) == 1 and scoped_group_ids[0] in group_map else "Несколько групп",
            "selected_semester": schedule.semester,
            "expected_subjects_count": 0,
            "fully_placed_subjects_count": 0,
            "partially_placed_subjects_count": 0,
            "not_placed_subjects_count": 0,
            "total_missing_pairs": 0,
            "hard_conflicts_count": len(hard_conflicts),
            "unscheduled_count": len(unscheduled_conflicts),
            "warnings_count": len(warning_conflicts),
            "unresolved_teacher_rows_count": 0,
            "online_placed_count": sum(online_counts.values()),
            "online_missing_count": 0,
            "teachers_with_balance_issue_count": 0,
        }

        for row in expected_rows:
            key = (row["group_id"], row["subject_id"], row["subgroup_code"])
            placed_pairs = sum(len(decode_week_scope(entry.week_scope)) for entry in grouped_entries.get(key, []))
            expected_pairs = int(row["expected_pairs"])
            missing_pairs = max(expected_pairs - placed_pairs, 0)
            status = "Полностью размещено"
            reason = ""
            if row["excluded_status"]:
                status = str(row["excluded_status"])
                reason = str(row["reason"])
                placed_pairs = 0
                missing_pairs = expected_pairs
            elif missing_pairs and row["assignment_state"] in {"vacancy", "unresolved_manual_review", "multi_teacher", "candidate_pool"}:
                status = "Требуется уточнение преподавателя"
                reason = self._row_reason(row, regular_capacities)
            elif missing_pairs and placed_pairs:
                status = "Частично размещено"
                reason = self._row_reason(row, regular_capacities)
            elif missing_pairs:
                status = "Не размещено"
                reason = self._row_reason(row, regular_capacities)

            subject_rows.append(
                {
                    "group_id": row["group_id"],
                    "subject_id": row["subject_id"],
                    "subject": row["subject_label"],
                    "expected_pairs": expected_pairs,
                    "placed_pairs": placed_pairs,
                    "missing_pairs": missing_pairs,
                    "status": status,
                    "reason": reason,
                    "assignment_state": row["assignment_state"],
                    "is_excluded": bool(row["excluded_status"]),
                }
            )
            summary["expected_subjects_count"] += 1
            if status == "Полностью размещено":
                summary["fully_placed_subjects_count"] += 1
            elif status == "Частично размещено":
                summary["partially_placed_subjects_count"] += 1
                summary["total_missing_pairs"] += missing_pairs
            elif status in {"Не размещено", "Требуется уточнение преподавателя", "Требуется уточнение подгруппы"}:
                summary["not_placed_subjects_count"] += 1
                summary["total_missing_pairs"] += missing_pairs

            if row["assignment_state"] in {"vacancy", "unresolved_manual_review", "multi_teacher", "candidate_pool"}:
                summary["unresolved_teacher_rows_count"] += 1
                normalization_issues.append(
                    {
                        "subject": row["subject_label"],
                        "state": row["assignment_state"],
                        "message": self._normalization_message(row),
                    }
                )

        relevant_teacher_ids = {
            row["teacher_id"]
            for row in self._group_teacher_links(session, scoped_group_ids, schedule.semester)
            if row["teacher_id"]
        }
        balance_rows = [
            item
            for item in self.teacher_balance_report(session)
            if item["teacher_id"] in relevant_teacher_ids
        ]
        balance_warnings = [
            {
                "teacher_name": item["teacher_name"],
                "message": (
                    f"Баланс нагрузки преподавателя нарушен: семестр 3 = {item['semester_3_pairs']} пар, "
                    f"семестр 4 = {item['semester_4_pairs']} пар."
                ),
                "score": item["normalized_balance_score"],
            }
            for item in balance_rows
            if float(item["normalized_balance_score"]) >= 2.0
        ]
        summary["teachers_with_balance_issue_count"] = len(balance_warnings)
        warning_items: list[dict[str, object]] = [{"type": conflict.type, "message": conflict.message, "conflict": conflict} for conflict in warning_conflicts]
        warning_items.extend(
            {"type": "teacher_balance", "message": item["message"], "score": item["score"]}
            for item in balance_warnings
        )
        summary["warnings_count"] = len(warning_items)

        if scoped_group_ids:
            summary["online_missing_count"] = sum(
                max(online_targets.get(group_id_, 0) - online_counts.get(group_id_, 0), 0)
                for group_id_ in scoped_group_ids
            )

        return {
            "schedule": schedule,
            "entries": entries,
            "summary": summary,
            "subject_rows": sorted(subject_rows, key=lambda item: (item["status"] != "Не размещено", item["subject"])),
            "hard_conflicts": hard_conflicts,
            "unscheduled_conflicts": unscheduled_conflicts,
            "warnings": warning_items,
            "normalization_issues": normalization_issues,
            "teacher_balance_rows": balance_rows,
        }

    def upsert_online_slot(
        self,
        session: Session,
        slot_id: int | None,
        *,
        label: str,
        day_of_week: int,
        start_time: str,
        end_time: str,
        is_active: bool,
        order_index: int,
    ) -> OnlineSlot:
        slot = session.get(OnlineSlot, slot_id) if slot_id else None
        if slot is None:
            slot = OnlineSlot(
                label=label,
                day_of_week=day_of_week,
                start_time=start_time,
                end_time=end_time,
                is_active=is_active,
                order_index=order_index,
            )
        else:
            slot.label = label
            slot.day_of_week = day_of_week
            slot.start_time = start_time
            slot.end_time = end_time
            slot.is_active = is_active
            slot.order_index = order_index
        session.add(slot)
        session.commit()
        session.refresh(slot)
        return slot

    def _expected_subject_rows(self, session: Session, semester: int, group_ids: list[int]) -> list[dict[str, object]]:
        subject_map = {subject.id: subject for subject in session.exec(select(Subject)).all()}
        rows: list[dict[str, object]] = []
        weekly_rows = self.weekly_workload_service.active_rows(session, semester=semester, group_ids=group_ids)
        if weekly_rows:
            for row in weekly_rows:
                subject = subject_map.get(row.subject_id)
                excluded_status = ""
                reason = ""
                if row.is_practice or row.load_category in {"practice", "study_practice", "industrial_practice"}:
                    excluded_status = "Исключено из обычной сетки как практика"
                    reason = "Практика учитывается отдельно и не попадает в обычную сетку пар."
                elif row.is_facultative:
                    excluded_status = "Исключено как факультатив (если не включено)"
                    reason = "Факультатив не включен в обычную генерацию по умолчанию."
                rows.append(
                    {
                        "group_id": row.group_id,
                        "subject_id": row.subject_id,
                        "subject_label": f"{subject.name if subject else row.subject_id}{f' (подгруппа {row.subgroup_code})' if row.subgroup_code else ''}",
                        "subgroup_code": row.subgroup_code or "",
                        "expected_pairs": self._expected_pairs_from_weekly(row),
                        "assignment_state": row.assignment_state,
                        "excluded_status": excluded_status,
                        "reason": reason,
                    }
                )
            return rows

        curriculum_rows = session.exec(
            select(CurriculumLoad).where(
                CurriculumLoad.semester == semester,
                CurriculumLoad.group_id.in_(group_ids),
            )
        ).all()
        for row in curriculum_rows:
            subject = subject_map.get(row.subject_id)
            rows.append(
                {
                    "group_id": row.group_id,
                    "subject_id": row.subject_id,
                    "subject_label": subject.name if subject else str(row.subject_id),
                    "subgroup_code": "",
                    "expected_pairs": max(int(round(row.total_hours / 2)), 1),
                    "assignment_state": "manual",
                    "excluded_status": "",
                    "reason": "",
                }
            )
        return rows

    @staticmethod
    def _expected_pairs_from_weekly(row: WeeklyLoad) -> int:
        weeks = max(int(row.study_weeks or 0), 1)
        if row.total_hours > 0:
            return max(int(round(row.total_hours / 2)), 1)
        if row.weekly_pairs > 0:
            return max(int(round(row.weekly_pairs * weeks)), 1)
        return 0

    def _group_regular_capacity(self, session: Session, group_ids: list[int], semester: int) -> dict[int, int]:
        groups = {group.id: group for group in session.exec(select(Group).where(Group.id.in_(group_ids))).all()} if group_ids else {}
        online_slot_count = len(self.online_slot_service.active_slots(session))
        result: dict[int, int] = {}
        for group_id in group_ids:
            group = groups.get(group_id)
            if group is None:
                continue
            study_weeks = len(
                session.exec(
                    select(AcademicPeriod).where(
                        AcademicPeriod.group_id == group_id,
                        AcademicPeriod.semester == semester,
                        AcademicPeriod.is_schedulable.is_(True),
                    )
                ).all()
            )
            result[group_id] = study_weeks * (len(allowed_pairs_for_shift(group.shift)) * 5 + online_slot_count)
        return result

    def _row_reason(self, row: dict[str, object], regular_capacities: dict[int, int]) -> str:
        assignment_state = str(row["assignment_state"])
        if assignment_state == "vacancy":
            return "Для этой нагрузки не назначен преподаватель."
        if assignment_state == "multi_teacher":
            return "В исходной строке указано несколько преподавателей, требуется уточнение."
        if assignment_state in {"unresolved_manual_review", "candidate_pool"}:
            return "Строка требует ручной проверки закрепления преподавателя."
        if regular_capacities.get(int(row["group_id"]), 0) <= 0:
            return "Для группы не найдено учебных недель или доступной вместимости."
        return "Не хватило доступных слотов без нарушения жёстких ограничений."

    @staticmethod
    def _normalization_message(row: dict[str, object]) -> str:
        assignment_state = str(row["assignment_state"])
        if assignment_state == "vacancy":
            return f"По предмету «{row['subject_label']}» в исходном файле указана вакансия."
        if assignment_state == "multi_teacher":
            return f"По предмету «{row['subject_label']}» найдено несколько преподавателей в одной строке."
        return f"По предмету «{row['subject_label']}» требуется ручная проверка нормализации."

    def _group_teacher_links(self, session: Session, group_ids: list[int], semester: int) -> list[dict[str, int]]:
        rows = self.weekly_workload_service.active_rows(session, semester=semester, group_ids=group_ids)
        result: list[dict[str, int]] = []
        for row in rows:
            teacher_id = row.resolved_teacher_id or row.fixed_teacher_id
            if teacher_id:
                result.append({"teacher_id": teacher_id})
            for raw_id in (row.candidate_teacher_ids or "").split(","):
                if raw_id:
                    result.append({"teacher_id": int(raw_id)})
        return result

    @staticmethod
    def _conflict_matches_groups(conflict: Conflict, group_ids: list[int], entry_map: dict[int, ScheduleEntry]) -> bool:
        if not group_ids:
            return True
        details = {}
        if conflict.details_json:
            try:
                details = json.loads(conflict.details_json)
            except json.JSONDecodeError:
                details = {}
        if details.get("group_id") in group_ids:
            return True
        related_ids = [int(item) for item in conflict.related_entry_ids.split(",") if item]
        return any(entry_map.get(entry_id) and entry_map[entry_id].group_id in group_ids for entry_id in related_ids)

    @staticmethod
    def _schedule_group_ids(session: Session, schedule: Schedule) -> list[int]:
        codes = [item.strip() for item in (schedule.group_scope or "").split(",") if item.strip()]
        if codes:
            return [group.id or 0 for group in session.exec(select(Group).where(Group.code.in_(codes))).all()]
        rows = session.exec(select(ScheduleEntry.group_id).where(ScheduleEntry.schedule_id == schedule.id).distinct()).all()
        return [row[0] if isinstance(row, tuple) else int(row) for row in rows]

    def _provider(self, session: Session):
        ai_provider = self._setting(session, "ai_provider") or settings.ai_provider
        gemini_api_key = self._setting(session, "gemini_api_key") or settings.gemini_api_key
        if ai_provider == "gemini":
            return GeminiExplanationProvider(gemini_api_key)
        return DummyExplanationProvider()

    def _validate_group_payload(self, session: Session, payload: dict, current_group_id: int | None = None) -> None:
        code = (payload.get("code") or "").strip()
        if not code:
            raise ValueError("Код группы обязателен.")
        if int(payload.get("semester") or 0) <= 0:
            raise ValueError("Нужно указать корректный семестр.")
        if int(payload.get("student_count") or -1) < 0:
            raise ValueError("Количество студентов должно быть неотрицательным числом.")
        if int(payload.get("home_department_id") or 0) <= 0:
            raise ValueError("Нужно указать отделение группы.")
        existing = session.exec(select(Group).where(Group.code == code)).first()
        if existing is not None and existing.id != current_group_id:
            raise ValueError("Группа с таким кодом уже существует.")

    def _validate_subject_payload(self, session: Session, payload: dict, current_subject_id: int | None = None) -> None:
        code = (payload.get("code") or "").strip()
        name = (payload.get("name") or "").strip()
        if not code or not name:
            raise ValueError("Нужно указать код и название предмета.")
        if int(payload.get("owner_department_id") or 0) <= 0:
            raise ValueError("Нужно указать отделение-владелец предмета.")
        existing = session.exec(select(Subject).where(Subject.code == code)).first()
        if existing is not None and existing.id != current_subject_id:
            raise ValueError("Предмет с таким кодом уже существует.")

    @staticmethod
    def _validate_load_payload(payload: dict) -> None:
        if not payload.get("group_id") or not payload.get("subject_id"):
            raise ValueError("Нужно указать группу и предмет.")
        if int(payload.get("semester") or 0) <= 0:
            raise ValueError("Нужно указать корректный семестр.")
        if int(payload.get("study_weeks") or 0) <= 0:
            raise ValueError("Учебных недель должно быть больше нуля.")
        if float(payload.get("hours_per_week") or 0) < 0 or float(payload.get("pairs_per_week") or 0) < 0:
            raise ValueError("Часов и пар в неделю не может быть меньше нуля.")
        if int(payload.get("total_hours") or 0) < 0:
            raise ValueError("Всего часов не может быть меньше нуля.")

    def _validate_slot_rules(
        self,
        session: Session,
        group: Group | None,
        entry: ScheduleEntry,
        requested_day_of_week: int | None = None,
    ) -> None:
        if entry.lesson_mode == LESSON_MODE_ONLINE:
            if entry.slot_category != SLOT_CATEGORY_ONLINE_EXTRA or entry.pair_number != 0:
                raise ValueError("Онлайн-занятия можно ставить только в отдельные онлайн-слоты.")
            if requested_day_of_week is not None and requested_day_of_week not in ONLINE_ALLOWED_DAYS:
                raise ValueError("Онлайн-занятия доступны только в среду, четверг и пятницу.")
            slot = self.online_slot_service.slot_map(session).get(entry.online_slot_number or 0)
            if slot is None or not slot.is_active:
                raise ValueError("Онлайн-занятия можно ставить только в отдельные онлайн-слоты.")
            if requested_day_of_week is not None and requested_day_of_week != slot.day_of_week:
                raise ValueError("Онлайн-занятия можно ставить только в отдельные онлайн-слоты.")
            if entry.day_of_week != slot.day_of_week or entry.day_of_week not in ONLINE_ALLOWED_DAYS:
                raise ValueError("Онлайн-занятия можно ставить только в отдельные онлайн-слоты.")
            return
        if entry.slot_category != SLOT_CATEGORY_REGULAR:
            raise ValueError("Очные занятия нельзя ставить в онлайн-слоты.")
        if entry.online_slot_number is not None:
            raise ValueError("Очные занятия нельзя ставить в онлайн-слоты.")
        if group is None or pair_allowed_for_shift(group.shift, entry.pair_number):
            return
        if group.shift == SHIFT_MORNING:
            raise ValueError("Для утренней смены доступны только пары 1–3.")
        if group.shift == SHIFT_AFTERNOON:
            raise ValueError("Для послеобеденной смены доступны только пары 4–6.")
        raise ValueError("Выбранная пара не соответствует смене группы.")

    def _validate_teacher_assignment(self, session: Session, entry: ScheduleEntry) -> None:
        fixed = session.exec(
            select(GroupSubjectTeacher).where(
                GroupSubjectTeacher.group_id == entry.group_id,
                GroupSubjectTeacher.subject_id == entry.subject_id,
            )
        ).all()
        if any(item.teacher_id == entry.teacher_id for item in fixed):
            return
        allowed = session.exec(
            select(TeacherSubject).where(
                TeacherSubject.teacher_id == entry.teacher_id,
                TeacherSubject.subject_id == entry.subject_id,
                TeacherSubject.can_teach.is_(True),
            )
        ).first()
        if allowed is None:
            raise ValueError("Преподаватель не может вести этот предмет для выбранной группы.")

    def _validate_slot_availability(self, session: Session, entry: ScheduleEntry) -> None:
        others = session.exec(
            select(ScheduleEntry).where(
                ScheduleEntry.schedule_id == entry.schedule_id,
                ScheduleEntry.id != entry.id,
            )
        ).all()
        for other in others:
            if other.day_of_week != entry.day_of_week:
                continue
            if not scopes_overlap(other.week_scope, entry.week_scope):
                continue
            if not intervals_overlap(other.start_time, other.end_time, entry.start_time, entry.end_time):
                continue
            same_group = other.group_id == entry.group_id
            subgroup_overlap = not (
                other.subgroup_code
                and entry.subgroup_code
                and other.subgroup_code != entry.subgroup_code
            )
            if same_group and subgroup_overlap:
                raise ValueError("Группа уже занята в это время.")
            if other.teacher_id == entry.teacher_id:
                if other.lesson_mode != entry.lesson_mode:
                    raise ValueError("Конфликт онлайн- и очного занятия у преподавателя.")
                raise ValueError("Преподаватель уже занят в это время.")
            if room_required(other.delivery_mode) and room_required(entry.delivery_mode):
                if entry.room_id is not None and other.room_id == entry.room_id:
                    raise ValueError("Аудитория уже занята в это время.")

    def _find_available_room(self, session: Session, entry: ScheduleEntry) -> Room | None:
        rooms = session.exec(select(Room).order_by(Room.code)).all()
        others = session.exec(
            select(ScheduleEntry).where(
                ScheduleEntry.schedule_id == entry.schedule_id,
                ScheduleEntry.id != entry.id,
                ScheduleEntry.lesson_mode == LESSON_MODE_REGULAR,
                ScheduleEntry.day_of_week == entry.day_of_week,
                ScheduleEntry.pair_number == entry.pair_number,
            )
        ).all()
        for room in rooms:
            room_busy = False
            for other in others:
                if other.room_id != room.id or not room_required(other.delivery_mode):
                    continue
                if scopes_overlap(other.week_scope, entry.week_scope):
                    room_busy = True
                    break
            if not room_busy:
                return room
        return None

    @staticmethod
    def _setting(session: Session, key: str) -> str:
        setting = session.exec(select(AppSetting).where(AppSetting.key == key)).first()
        return setting.value if setting else ""
