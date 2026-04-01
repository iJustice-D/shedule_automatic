from __future__ import annotations

import requests
from sqlmodel import select

from app.models import ScheduleEntry, Teacher
from app.services.timetable_service import TimetableService
from app.tests.test_scheduler_and_conflicts import build_session


def test_ai_connection_reports_disabled_mode() -> None:
    session = build_session()
    service = TimetableService()
    service.save_ai_settings(session, enabled=False, api_key="", model="gemini-2.5-flash", timeout=10)

    result = service.test_ai_connection(session)

    assert result.success is False
    assert result.message == "ИИ отключен в настройках"


def test_ai_connection_reports_missing_key_for_enabled_gemini() -> None:
    session = build_session()
    service = TimetableService()
    service.save_ai_settings(session, enabled=True, api_key="", model="gemini-2.5-flash", timeout=10)

    result = service.test_ai_connection(session)

    assert result.success is False
    assert result.message == "API-ключ не указан"


def test_schedule_summary_falls_back_when_gemini_request_fails(monkeypatch) -> None:
    session = build_session()
    service = TimetableService()
    schedule = service.generate_schedule(session, semester=3, group_codes=["ETB-1124-1"], name="AI summary")
    service.save_ai_settings(session, enabled=True, api_key="test-key", model="gemini-2.5-flash", timeout=5)

    def _raise_request_error(*args, **kwargs):
        raise requests.RequestException("boom")

    monkeypatch.setattr("app.services.ai.gemini_provider.requests.post", _raise_request_error)

    result = service.test_ai_connection(session)
    summary = service.summarize_schedule(session, schedule.id or 0)

    assert result.success is False
    assert result.message == "Не удалось подключиться к Gemini"
    assert "Не удалось подключиться к Gemini. Используется стандартный режим без ИИ." in summary


def test_inline_teacher_rename_updates_only_display_name() -> None:
    session = build_session()
    service = TimetableService()
    schedule = service.generate_schedule(session, semester=3, group_codes=["ETB-1124-1"], name="Rename display only")
    entry = session.exec(select(ScheduleEntry).where(ScheduleEntry.schedule_id == schedule.id)).first()
    assert entry is not None
    teacher_before = session.get(Teacher, entry.teacher_id)
    assert teacher_before is not None

    updated = service.update_entry(
        session,
        entry.id or 0,
        {
            "rename_teacher_to": "Тестовое отображаемое имя",
            "locked": not entry.locked,
        },
    )

    teacher_after = session.get(Teacher, updated.teacher_id)
    assert teacher_after is not None
    assert teacher_after.editable_name == "Тестовое отображаемое имя"
    assert teacher_after.full_name == teacher_before.full_name
    assert teacher_after.short_name == teacher_before.short_name
