from __future__ import annotations

import json
from pathlib import Path

from sqlmodel import Session, select

from app.core.config import settings
from app.core.timetable import (
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
    apply_timeslot_to_entry,
    online_slot_matches_day,
    online_slot_numbers,
    pair_allowed_for_shift,
    room_required,
)
from app.core.week_scope import scopes_overlap
from app.models import AppSetting, ChangeLog, Conflict, Group, GroupSubjectTeacher, OnlinePolicy, Room, Schedule, ScheduleEntry, Subject, Suggestion, Teacher, TeacherSubject
from app.services.ai.dummy import DummyExplanationProvider
from app.services.ai.gemini_provider import GeminiExplanationProvider
from app.services.conflicts.engine import ConflictEngine
from app.services.exporters.context import build_schedule_context
from app.services.exporters.docx_exporter import DocxExporter
from app.services.exporters.pdf_exporter import PdfExporter
from app.services.exporters.xlsx_exporter import XlsxExporter
from app.services.online_policy import OnlinePolicyService
from app.services.scheduler.generator import GreedyScheduleGenerator
from app.services.suggestions.engine import SuggestionEngine


class TimetableService:
    def __init__(self) -> None:
        self.generator = GreedyScheduleGenerator()
        self.conflict_engine = ConflictEngine()
        self.suggestion_engine = SuggestionEngine()
        self.xlsx_exporter = XlsxExporter()
        self.pdf_exporter = PdfExporter()
        self.docx_exporter = DocxExporter()
        self.online_policy_service = OnlinePolicyService()

    def generate_schedule(
        self,
        session: Session,
        semester: int,
        group_codes: list[str] | None = None,
        name: str | None = None,
    ) -> Schedule:
        schedule = self.generator.generate(session, semester=semester, group_codes=group_codes, schedule_name=name)
        self.revalidate_schedule(session, schedule.id or 0)
        return session.get(Schedule, schedule.id)

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
        self._validate_slot_rules(group, entry)
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

    def _provider(self, session: Session):
        ai_provider = self._setting(session, "ai_provider") or settings.ai_provider
        gemini_api_key = self._setting(session, "gemini_api_key") or settings.gemini_api_key
        if ai_provider == "gemini":
            return GeminiExplanationProvider(gemini_api_key)
        return DummyExplanationProvider()

    def _validate_slot_rules(self, group: Group | None, entry: ScheduleEntry) -> None:
        if entry.lesson_mode == LESSON_MODE_ONLINE:
            if entry.slot_category != SLOT_CATEGORY_ONLINE_EXTRA or entry.pair_number != 0:
                raise ValueError("Онлайн-занятия можно ставить только в отдельные онлайн-слоты.")
            if entry.day_of_week not in ONLINE_ALLOWED_DAYS:
                raise ValueError("Онлайн-занятия доступны только в среду, четверг и пятницу.")
            if entry.online_slot_number not in online_slot_numbers() or not online_slot_matches_day(entry.online_slot_number or 0, entry.day_of_week):
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
            if other.lesson_mode != entry.lesson_mode:
                continue
            if other.day_of_week != entry.day_of_week:
                continue
            if entry.lesson_mode == LESSON_MODE_REGULAR and other.pair_number != entry.pair_number:
                continue
            if entry.lesson_mode == LESSON_MODE_ONLINE and other.online_slot_number != entry.online_slot_number:
                continue
            if not scopes_overlap(other.week_scope, entry.week_scope):
                continue
            if other.group_id == entry.group_id:
                raise ValueError("Группа уже занята в это время.")
            if other.teacher_id == entry.teacher_id:
                raise ValueError("Преподаватель уже занят в это время.")
            if entry.lesson_mode == LESSON_MODE_REGULAR and room_required(other.delivery_mode) and room_required(entry.delivery_mode):
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
