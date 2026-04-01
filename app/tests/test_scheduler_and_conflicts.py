from __future__ import annotations

from pathlib import Path

from sqlmodel import Session, SQLModel, create_engine, select

from app.core.timetable import LESSON_MODE_ONLINE, LESSON_MODE_REGULAR, ONLINE_ALLOWED_DAYS, visible_pairs_for_view
from app.models import Conflict, Group, ScheduleEntry, Timeslot
from app.services.exporters.context import build_schedule_context
from app.services.seeding import Seeder
from app.services.timetable_service import TimetableService


BASE_DIR = Path(__file__).resolve().parents[2]


def build_session() -> Session:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    session = Session(engine)
    Seeder().seed(
        session,
        BASE_DIR / "data" / "Оқу жоспар_ЕТБ-1124.xls",
        BASE_DIR / "data" / "Үрдіс 2025-2026 оқу жылы соңғысы (1).pdf",
    )
    return session


def test_schedule_generation_creates_entries_without_hard_conflicts() -> None:
    session = build_session()
    service = TimetableService()
    schedule = service.generate_schedule(session, semester=3, group_codes=["ETB-1124-1"], name="Test semester 3")
    entries = session.exec(select(ScheduleEntry).where(ScheduleEntry.schedule_id == schedule.id)).all()
    conflicts = session.exec(select(Conflict).where(Conflict.schedule_id == schedule.id)).all()
    assert entries
    hard_types = {item.type for item in conflicts if item.severity == "hard"}
    assert hard_types <= {"unscheduled_load"}


def test_timeslots_are_seeded_with_real_college_pairs() -> None:
    session = build_session()
    slots = session.exec(select(Timeslot).order_by(Timeslot.pair_number)).all()
    assert len(slots) == 6
    assert [(slot.pair_number, slot.shift, slot.start_time, slot.end_time) for slot in slots] == [
        (1, "morning", "08:00", "09:20"),
        (2, "morning", "09:40", "11:00"),
        (3, "morning", "11:10", "12:30"),
        (4, "afternoon", "13:30", "14:50"),
        (5, "afternoon", "15:10", "16:30"),
        (6, "afternoon", "16:40", "18:00"),
    ]


def test_generation_respects_group_shift_and_online_policy() -> None:
    session = build_session()
    service = TimetableService()

    morning_schedule = service.generate_schedule(session, semester=3, group_codes=["ETB-2202"], name="Shift test morning")
    morning_entries = session.exec(select(ScheduleEntry).where(ScheduleEntry.schedule_id == morning_schedule.id)).all()
    assert morning_entries
    regular_entries = [entry for entry in morning_entries if entry.lesson_mode == LESSON_MODE_REGULAR]
    online_entries = [entry for entry in morning_entries if entry.lesson_mode == LESSON_MODE_ONLINE]
    assert regular_entries
    assert {entry.shift for entry in regular_entries} == {"morning"}
    assert all(entry.pair_number in {1, 2, 3} for entry in regular_entries)
    assert len(online_entries) == 3
    assert all(entry.room_id is None and entry.pair_number == 0 for entry in online_entries)
    assert {entry.day_of_week for entry in online_entries} == set(ONLINE_ALLOWED_DAYS)

    afternoon_schedule = service.generate_schedule(session, semester=3, group_codes=["DTP-2201"], name="Shift test afternoon")
    afternoon_entries = session.exec(select(ScheduleEntry).where(ScheduleEntry.schedule_id == afternoon_schedule.id)).all()
    assert afternoon_entries
    regular_entries = [entry for entry in afternoon_entries if entry.lesson_mode == LESSON_MODE_REGULAR]
    online_entries = [entry for entry in afternoon_entries if entry.lesson_mode == LESSON_MODE_ONLINE]
    assert regular_entries
    assert {entry.shift for entry in regular_entries} == {"afternoon"}
    assert all(entry.pair_number in {4, 5, 6} for entry in regular_entries)
    assert all(entry.day_of_week in ONLINE_ALLOWED_DAYS and entry.pair_number == 0 for entry in online_entries)


def test_manual_edit_triggers_conflict_revalidation() -> None:
    session = build_session()
    service = TimetableService()
    schedule = service.generate_schedule(session, semester=3, group_codes=["ETB-1124-1"], name="Edit test")
    entries = session.exec(select(ScheduleEntry).where(ScheduleEntry.schedule_id == schedule.id)).all()
    assert len(entries) > 1
    first, second = entries[0], entries[1]
    try:
        service.update_entry(
            session,
            second.id or 0,
            {
                "day_of_week": first.day_of_week,
                "pair_number": first.pair_number,
                "room_id": first.room_id,
            },
        )
    except ValueError as exc:
        assert str(exc) == "Группа уже занята в это время."
    else:
        raise AssertionError("Ожидалась ошибка валидации при наложении занятий.")


def test_online_edit_clears_room_and_keeps_revalidation() -> None:
    session = build_session()
    service = TimetableService()
    group = session.exec(select(Group).where(Group.code == "ETB-1124-1")).first()
    assert group is not None
    service.upsert_group_online_target(session, group.id or 0, 1)
    schedule = service.generate_schedule(session, semester=3, group_codes=["ETB-1124-1"], name="Online edit")
    entries = session.exec(select(ScheduleEntry).where(ScheduleEntry.schedule_id == schedule.id)).all()
    used_online_slots = {entry.online_slot_number for entry in entries if entry.lesson_mode == LESSON_MODE_ONLINE}
    free_online_slot = next(slot for slot in (1, 2, 3) if slot not in used_online_slots)
    entry = session.exec(
        select(ScheduleEntry).where(
            ScheduleEntry.schedule_id == schedule.id,
            ScheduleEntry.lesson_mode == LESSON_MODE_REGULAR,
        )
    ).first()
    assert entry is not None
    assert entry.room_id is not None

    updated = service.update_entry(
        session,
        entry.id or 0,
        {
            "lesson_mode": LESSON_MODE_ONLINE,
            "online_slot_number": free_online_slot,
            "day_of_week": 2 + free_online_slot,
            "pair_number": 0,
            "room_id": None,
        },
    )

    assert updated.lesson_mode == LESSON_MODE_ONLINE
    assert updated.delivery_mode == "online"
    assert updated.room_id is None
    assert updated.pair_number == 0
    assert updated.day_of_week == 2 + free_online_slot


def test_manual_edit_rejects_pair_outside_group_shift() -> None:
    session = build_session()
    service = TimetableService()
    schedule = service.generate_schedule(session, semester=3, group_codes=["ETB-2202"], name="Shift validation")
    entry = session.exec(select(ScheduleEntry).where(ScheduleEntry.schedule_id == schedule.id)).first()
    assert entry is not None

    try:
        service.update_entry(
            session,
            entry.id or 0,
            {
                "pair_number": 5,
            },
        )
    except ValueError as exc:
        assert str(exc) == "Для утренней смены доступны только пары 1–3."
    else:
        raise AssertionError("Ожидалась ошибка валидации смены.")


def test_visible_pairs_follow_selected_group_shift() -> None:
    assert visible_pairs_for_view("group", "morning", "all") == (1, 2, 3)
    assert visible_pairs_for_view("group", "afternoon", "all") == (4, 5, 6)
    assert visible_pairs_for_view("teacher", None, "all") == (1, 2, 3, 4, 5, 6)
    assert visible_pairs_for_view("teacher", None, "morning") == (1, 2, 3)


def test_online_lessons_are_separated_from_main_export_context() -> None:
    session = build_session()
    service = TimetableService()
    schedule = service.generate_schedule(session, semester=3, group_codes=["ETB-2202"], name="Export context")
    context = build_schedule_context(session, schedule.id or 0)
    online_rows = context["group_online_rows"]["ETB-2202"]
    assert online_rows
    assert any("Онлайн-слот" in row["online_slot"] for row in online_rows)
    regular_grid = context["group_grids"]["ETB-2202"]
    assert all("Онлайн-слот" not in cell for cell in regular_grid.values() if cell)


def test_online_edit_rejects_monday_slot() -> None:
    session = build_session()
    service = TimetableService()
    schedule = service.generate_schedule(session, semester=3, group_codes=["ETB-2202"], name="Online rules")
    entry = session.exec(select(ScheduleEntry).where(ScheduleEntry.schedule_id == schedule.id)).first()
    assert entry is not None
    try:
        service.update_entry(
            session,
            entry.id or 0,
            {
                "lesson_mode": LESSON_MODE_ONLINE,
                "day_of_week": 1,
                "pair_number": 0,
                "online_slot_number": 1,
            },
        )
    except ValueError as exc:
        assert str(exc) == "Онлайн-занятия доступны только в среду, четверг и пятницу."
    else:
        raise AssertionError("Ожидалась ошибка для онлайн-занятия в понедельник.")
