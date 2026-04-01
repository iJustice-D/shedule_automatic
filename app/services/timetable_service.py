from __future__ import annotations

import json
from pathlib import Path
from typing import Any

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
    day_label,
    delivery_mode_label,
    online_slot_matches_day,
    online_slot_label,
    online_slot_numbers,
    pair_allowed_for_shift,
    pair_label,
    room_required,
)
from app.core.week_scope import scopes_overlap
from app.models import AppSetting, ChangeLog, Conflict, Group, GroupSubjectTeacher, OnlinePolicy, Room, Schedule, ScheduleEntry, Subject, Suggestion, Teacher, TeacherSubject
from app.services.ai.base import AITestResult
from app.services.ai.dummy_provider import DummyExplanationProvider
from app.services.ai.factory import build_ai_settings, create_provider
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
            if teacher is not None:
                teacher.editable_name = str(payload["rename_teacher_to"]).strip()
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
        full_name = full_name.strip()
        short_name = (short_name or "").strip()
        if not full_name:
            raise ValueError("Поле «ФИО» обязательно")
        if not short_name:
            raise ValueError("Поле «Краткое имя» обязательно")
        if home_department_id < 1:
            raise ValueError("Выберите отдел преподавателя")
        if max_weekly_pairs < 1:
            raise ValueError("Укажите корректную недельную нагрузку")
        teacher = Teacher(
            full_name=full_name,
            short_name=short_name,
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
        name = name.strip()
        if not name:
            raise ValueError("Заполните обязательные поля.")
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
        try:
            return provider.explain_conflict(conflict, suggestions)
        except Exception:
            return self._fallback_provider("Не удалось подключиться к Gemini").explain_conflict(conflict, suggestions)

    def summarize_schedule(self, session: Session, schedule_id: int) -> str:
        schedule = session.get(Schedule, schedule_id)
        if schedule is None:
            raise ValueError("Расписание не найдено.")
        entries = session.exec(select(ScheduleEntry).where(ScheduleEntry.schedule_id == schedule.id)).all()
        conflicts = session.exec(select(Conflict).where(Conflict.schedule_id == schedule.id)).all()
        group_ids = sorted({entry.group_id for entry in entries})
        groups = [group for group in (session.get(Group, group_id) for group_id in group_ids) if group is not None]
        provider = self._provider(session)
        try:
            return provider.summarize_schedule(schedule, entries, conflicts, groups)
        except Exception:
            return self._fallback_provider("Не удалось подключиться к Gemini").summarize_schedule(schedule, entries, conflicts, groups)

    def explain_last_manual_edit(self, session: Session, schedule_id: int) -> str:
        change = session.exec(
            select(ChangeLog).where(
                ChangeLog.schedule_id == schedule_id,
                ChangeLog.action_type == "update_entry",
            ).order_by(ChangeLog.created_at.desc(), ChangeLog.id.desc())
        ).first()
        if change is None:
            return "Изменение пока не найдено."
        before = json.loads(change.before_json or "{}")
        after = json.loads(change.after_json or "{}")
        remaining_conflicts = session.exec(select(Conflict).where(Conflict.schedule_id == schedule_id)).all()
        provider = self._provider(session)
        before_view = self._entry_snapshot(session, before)
        after_view = self._entry_snapshot(session, after)
        try:
            return provider.explain_manual_edit(before_view, after_view, remaining_conflicts)
        except Exception:
            return self._fallback_provider("Не удалось подключиться к Gemini").explain_manual_edit(
                before_view,
                after_view,
                remaining_conflicts,
            )

    def save_ai_settings(
        self,
        session: Session,
        *,
        enabled: bool,
        api_key: str,
        model: str,
        timeout: int,
    ) -> None:
        payload = {
            "ai_enabled": "true" if enabled else "false",
            "ai_provider": "gemini",
            "gemini_api_key": api_key.strip(),
            "gemini_model": model.strip() or settings.gemini_model,
            "gemini_timeout": str(max(5, int(timeout))),
            "ui_language": "ru",
        }
        for key, value in payload.items():
            item = session.exec(select(AppSetting).where(AppSetting.key == key)).first()
            if item is None:
                item = AppSetting(key=key, value=value)
            else:
                item.value = value
            session.add(item)
        session.commit()

    def test_ai_connection(self, session: Session, overrides: dict[str, Any] | None = None) -> AITestResult:
        return self._provider(session, overrides).test_connection()

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

    def _provider(self, session: Session, overrides: dict[str, Any] | None = None):
        return create_provider(self._ai_settings(session, overrides))

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

    def _settings_map(self, session: Session) -> dict[str, str]:
        return {item.key: item.value for item in session.exec(select(AppSetting)).all()}

    def _ai_settings(self, session: Session, overrides: dict[str, Any] | None = None):
        values = self._settings_map(session)
        if overrides:
            values.update({key: value for key, value in overrides.items() if value is not None})
        return build_ai_settings(values)

    @staticmethod
    def _fallback_provider(reason: str) -> DummyExplanationProvider:
        return DummyExplanationProvider(
            connection_message=reason,
            fallback_notice=f"{reason}. Используется стандартный режим без ИИ",
        )

    def _online_slot_labels(self, session: Session) -> dict[int, str]:
        settings_map = self._settings_map(session)
        return {
            slot: settings_map.get(f"online_slot_{slot}_label") or online_slot_label(slot)
            for slot in online_slot_numbers()
        }

    def _entry_snapshot(self, session: Session, snapshot: dict[str, Any]) -> dict[str, str]:
        lesson_mode = snapshot.get("lesson_mode") or LESSON_MODE_REGULAR
        day_of_week = self._to_int(snapshot.get("day_of_week"))
        pair_number = self._to_int(snapshot.get("pair_number"))
        online_slot_number = self._to_int(snapshot.get("online_slot_number"))
        subject_id = self._to_int(snapshot.get("subject_id"))
        teacher_id = self._to_int(snapshot.get("teacher_id"))
        room_id = self._to_int(snapshot.get("room_id"))
        subject = session.get(Subject, subject_id) if subject_id else None
        teacher = session.get(Teacher, teacher_id) if teacher_id else None
        room = session.get(Room, room_id) if room_id else None
        slot_labels = self._online_slot_labels(session)

        if lesson_mode == LESSON_MODE_ONLINE:
            slot_text = slot_labels.get(online_slot_number or 1, online_slot_label(online_slot_number or 1))
            room_text = "Не требуется"
        else:
            slot_text = pair_label(pair_number) if pair_number else "Не указана"
            room_text = room.code if room is not None else "Не указана"

        return {
            "subject": subject.name if subject is not None else "Не указан",
            "lesson_type": "Онлайн" if lesson_mode == LESSON_MODE_ONLINE else "Очное занятие",
            "day": day_label(day_of_week) if day_of_week else "Не указан",
            "slot": slot_text,
            "teacher": (teacher.editable_name or teacher.full_name) if teacher is not None else "Не указан",
            "room": room_text,
            "delivery_mode": delivery_mode_label(snapshot.get("delivery_mode") or "offline"),
            "locked": "Да" if self._to_bool(snapshot.get("locked")) else "Нет",
        }

    @staticmethod
    def _to_int(value: Any) -> int | None:
        if value in (None, "", "null"):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _to_bool(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "on"}
