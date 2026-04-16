from __future__ import annotations

import json
from pathlib import Path

from sqlmodel import Session, SQLModel, create_engine, select

from app.core.timetable import LESSON_MODE_ONLINE, LESSON_MODE_REGULAR, ONLINE_ALLOWED_DAYS, visible_pairs_for_view
from app.core.week_scope import format_week_scope
from app.models import Conflict, CurriculumLoad, Group, GroupSubjectTeacher, ScheduleEntry, Subject, Teacher, TeacherSubject, Timeslot
from app.services.exporters.context import build_schedule_context
from app.services.exporters.pdf_exporter import PdfExporter
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


def test_week_scope_formatting_is_human_readable() -> None:
    assert format_week_scope("weeks:23,24,25,27,28,30") == "23–25, 27–28, 30"
    assert format_week_scope("all") == "Все учебные недели"


def test_pdf_export_uses_unicode_font_and_creates_file(tmp_path) -> None:
    session = build_session()
    service = TimetableService()
    schedule = service.generate_schedule(session, semester=3, group_codes=["ETB-2202"], name="PDF export")
    exporter = PdfExporter()
    assert exporter._ensure_font() != "Helvetica"
    output = exporter.export(session, schedule.id or 0, tmp_path / "unicode_test.pdf")
    assert output.exists()
    content = output.read_bytes()
    assert len(content) > 0


def test_manual_curriculum_load_is_used_by_generator() -> None:
    session = build_session()
    service = TimetableService()
    group = session.exec(select(Group).where(Group.code == "ETB-2202")).first()
    teacher = session.exec(select(Teacher).order_by(Teacher.id)).first()
    assert group is not None
    assert teacher is not None
    subject = Subject(
        code="MANUAL-SUB-1",
        name="Ручной модуль для генератора",
        owner_department_id=group.home_department_id,
        lesson_type="lecture",
        requires_special_room=False,
        can_be_online=False,
        default_delivery_mode="offline",
    )
    session.add(subject)
    session.commit()
    session.refresh(subject)
    session.add(
        TeacherSubject(
            teacher_id=teacher.id or 0,
            subject_id=subject.id or 0,
            can_teach=True,
            priority=1,
        )
    )
    session.add(
        GroupSubjectTeacher(
            group_id=group.id or 0,
            subject_id=subject.id or 0,
            teacher_id=teacher.id or 0,
            fixed=True,
        )
    )
    session.add(
        CurriculumLoad(
            group_id=group.id or 0,
            subject_id=subject.id or 0,
            semester=3,
            total_hours=32,
            study_weeks=16,
            hours_per_week=2.0,
            pairs_per_week=1.0,
            lesson_type="lecture",
            delivery_mode="offline",
            raw_total_hours=32,
            practice_hours=0,
            source_code=subject.code,
            source_type="manual",
            note="Ручной ввод для теста.",
        )
    )
    session.commit()

    schedule = service.generate_schedule(session, semester=3, group_codes=["ETB-2202"], name="Manual load")
    manual_entries = session.exec(
        select(ScheduleEntry).where(
            ScheduleEntry.schedule_id == schedule.id,
            ScheduleEntry.subject_id == subject.id,
        )
    ).all()
    assert manual_entries


def test_result_diagnostics_are_scoped_to_selected_group_and_semester() -> None:
    session = build_session()
    service = TimetableService()
    service.import_weekly_workload(
        session,
        BASE_DIR / "data" / "2025-2026 ПРОГРАММИСТТЕР_ИНКАР (1) (2) (1).docx",
        calendar_path=BASE_DIR / "data" / "Үрдіс 2025-2026 оқу жылы соңғысы (1).pdf",
        curriculum_path=BASE_DIR / "data" / "Оқу жоспар_ЕТБ-1124.xls",
    )
    group = session.exec(select(Group).where(Group.code == "ETB-1124-1")).first()
    assert group is not None
    schedule = service.generate_schedule(session, semester=4, group_codes=["ETB-1124-1"], name="Scoped semester 4")
    diagnostics = service.result_diagnostics(session, schedule.id or 0, group.id or 0)

    assert diagnostics["summary"]["selected_group"] == "ETB-1124-1"
    assert diagnostics["summary"]["selected_semester"] == 4
    assert diagnostics["subject_rows"]
    assert all(row["group_id"] == (group.id or 0) for row in diagnostics["subject_rows"])
    for conflict in diagnostics["hard_conflicts"] + diagnostics["unscheduled_conflicts"]:
        if conflict.details_json:
            details = json.loads(conflict.details_json)
            if "group_id" in details:
                assert details["group_id"] == (group.id or 0)


def test_subject_completeness_never_silently_loses_source_rows() -> None:
    session = build_session()
    service = TimetableService()
    service.import_weekly_workload(
        session,
        BASE_DIR / "data" / "2025-2026 ПРОГРАММИСТТЕР_ИНКАР (1) (2) (1).docx",
        calendar_path=BASE_DIR / "data" / "Үрдіс 2025-2026 оқу жылы соңғысы (1).pdf",
        curriculum_path=BASE_DIR / "data" / "Оқу жоспар_ЕТБ-1124.xls",
        group_codes=["ETB-1124-1"],
    )
    group = session.exec(select(Group).where(Group.code == "ETB-1124-1")).first()
    assert group is not None
    schedule = service.generate_schedule(session, semester=4, group_codes=["ETB-1124-1"], name="Completeness semester 4")
    diagnostics = service.result_diagnostics(session, schedule.id or 0, group.id or 0)
    statuses = {row["status"] for row in diagnostics["subject_rows"]}

    assert diagnostics["summary"]["expected_subjects_count"] == len(diagnostics["subject_rows"])
    assert statuses
    assert statuses & {
        "Полностью размещено",
        "Частично размещено",
        "Не размещено",
        "Исключено как факультатив (если не включено)",
        "Исключено из обычной сетки как практика",
        "Требуется уточнение преподавателя",
    }
