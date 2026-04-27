from __future__ import annotations

import json
from pathlib import Path

from sqlmodel import Session, SQLModel, create_engine, select

from app.core.timetable import LESSON_MODE_ONLINE, LESSON_MODE_REGULAR, ONLINE_ALLOWED_DAYS, visible_pairs_for_view
from app.core.week_scope import format_week_scope
from app.models import Conflict, CurriculumLoad, GenerationJob, Group, GroupSubjectTeacher, Schedule, ScheduleEntry, Subject, Teacher, TeacherSubject, Timeslot
from app.services.scheduler.normalizer import WorkloadNormalizer
from app.services.exporters.context import build_schedule_context
from app.services.exporters.pdf_exporter import PdfExporter
from app.services.seeding import Seeder
from app.services.timetable_service import TimetableService
from app.services.weekly_workload import WeeklyWorkloadService
from app.ui.pages import slot_has_week_conflict


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


def test_semester3_normalization_uses_nine_study_weeks_and_marks_ambiguous_teacher_rows() -> None:
    session = build_session()
    WeeklyWorkloadService().import_docx(
        session,
        BASE_DIR / "data" / "2025-2026 ПРОГРАММИСТТЕР_ИНКАР (1) (2) (1).docx",
        calendar_path=BASE_DIR / "data" / "Үрдіс 2025-2026 оқу жылы соңғысы (1).pdf",
        curriculum_path=BASE_DIR / "data" / "Оқу жоспар_ЕТБ-1124.xls",
        target_group_codes=["ETB-1124-1"],
    )

    normalizer = WorkloadNormalizer()
    _, rows, requests = normalizer.normalize_scope(
        session,
        semester=3,
        group_codes=["ETB-1124-1"],
        include_facultatives=False,
    )

    economics_rows = [row for row in rows if "Экономиканың" in row.subject_name]
    assert len(economics_rows) == 1
    economics = economics_rows[0]
    assert economics.assignment_state == "multi_teacher_ambiguous"
    assert economics.study_weeks == [1, 2, 3, 4, 5, 6, 7, 8, 9]
    assert economics.normalization_issue == "Неоднозначное закрепление преподавателя по семестру."
    assert all(request.load_key != economics.load_key for request in requests)


def test_semester3_generation_reports_ambiguous_rows_as_unscheduled_not_teacher_overlap() -> None:
    session = build_session()
    WeeklyWorkloadService().import_docx(
        session,
        BASE_DIR / "data" / "2025-2026 ПРОГРАММИСТТЕР_ИНКАР (1) (2) (1).docx",
        calendar_path=BASE_DIR / "data" / "Үрдіс 2025-2026 оқу жылы соңғысы (1).pdf",
        curriculum_path=BASE_DIR / "data" / "Оқу жоспар_ЕТБ-1124.xls",
        target_group_codes=["ETB-1124-1"],
    )
    service = TimetableService()
    schedule = service.generate_schedule(session, semester=3, group_codes=["ETB-1124-1"], name="Semester 3 ETB")
    conflicts = session.exec(select(Conflict).where(Conflict.schedule_id == schedule.id)).all()

    assert not any(item.code in {"TEACHER-OVERLAP", "GROUP-OVERLAP"} for item in conflicts)
    assert any(
        item.code == "LOAD-MISSING" and "неоднозначное закрепление преподавателя" in item.message.lower()
        for item in conflicts
    )

    group = session.exec(select(Group).where(Group.code == "ETB-1124-1")).first()
    assert group is not None
    diagnostics = service.result_diagnostics(session, schedule.id or 0, group.id or 0)
    economics = next(row for row in diagnostics["subject_rows"] if "Экономиканың" in row["subject"])
    assert economics["status"] == "Требуется уточнение преподавателя"
    assert "Неоднозначное закрепление преподавателя" in economics["reason"]


def test_editor_conflict_badge_ignores_parallel_subgroups_with_same_weeks() -> None:
    session = build_session()
    WeeklyWorkloadService().import_docx(
        session,
        BASE_DIR / "data" / "2025-2026 ПРОГРАММИСТТЕР_ИНКАР (1) (2) (1).docx",
        calendar_path=BASE_DIR / "data" / "Үрдіс 2025-2026 оқу жылы соңғысы (1).pdf",
        curriculum_path=BASE_DIR / "data" / "Оқу жоспар_ЕТБ-1124.xls",
        target_group_codes=["ETB-1124-1"],
    )
    service = TimetableService()
    schedule = service.generate_schedule(session, semester=3, group_codes=["ETB-1124-1"], name="Semester 3 subgroup view")
    entries = session.exec(
        select(ScheduleEntry).where(
            ScheduleEntry.schedule_id == schedule.id,
            ScheduleEntry.day_of_week == 1,
            ScheduleEntry.pair_number == 1,
        )
    ).all()

    assert len(entries) >= 2
    assert {entry.subgroup_code for entry in entries} == {"A", "B"}
    assert slot_has_week_conflict(entries) is False


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


def test_generation_job_creates_scoped_result_and_history() -> None:
    session = build_session()
    service = TimetableService()
    group = session.exec(select(Group).where(Group.code == "ETB-2202")).first()
    assert group is not None

    job = service.create_generation_job(session, group_id=group.id or 0, semester=3, requested_name="Job flow")
    finished = service.run_generation_job(job.id or 0)

    assert finished.status == "completed"
    assert finished.result_schedule_id is not None
    result_schedule = session.get(Schedule, finished.result_schedule_id)
    assert result_schedule is not None
    assert result_schedule.semester == 3
    assert result_schedule.group_scope == "ETB-2202"
    history = service.list_generation_jobs(session, group_id=group.id or 0, semester=3)
    assert history
    assert any(item.id == finished.id for item in history)


def test_generation_job_fails_without_workload() -> None:
    session = build_session()
    service = TimetableService()
    existing_group = session.exec(select(Group)).first()
    assert existing_group is not None
    empty_group = Group(
        code="EMPTY-TEST-1",
        name="EMPTY-TEST-1",
        home_department_id=existing_group.home_department_id,
        course=2,
        year=2,
        semester=4,
        student_count=20,
        shift="morning",
    )
    session.add(empty_group)
    session.commit()
    session.refresh(empty_group)

    job = service.create_generation_job(session, group_id=empty_group.id or 0, semester=4, requested_name="Should fail")
    finished = service.run_generation_job(job.id or 0)

    assert finished.status == "failed"
    assert finished.result_schedule_id is None
    assert finished.summary_message in {
        "Для выбранной группы нет учебной нагрузки.",
        "Невозможно запустить генерацию: отсутствуют нормализованные данные.",
    }


def test_all_groups_generation_job_creates_scoped_results_without_teacher_parallel() -> None:
    session = build_session()
    service = TimetableService()
    service.import_weekly_workload(
        session,
        BASE_DIR / "data" / "2025-2026 ПРОГРАММИСТТЕР_ИНКАР (1) (2) (1).docx",
        calendar_path=BASE_DIR / "data" / "Үрдіс 2025-2026 оқу жылы соңғысы (1).pdf",
        curriculum_path=BASE_DIR / "data" / "Оқу жоспар_ЕТБ-1124.xls",
        group_codes=["ETB-1124-1", "ETB-0924-1", "ETB-0924-2"],
    )

    job = service.create_generation_job(
        session,
        group_id=None,
        semester=4,
        requested_name="All groups sem4",
        run_scope="all_groups",
        group_codes=["ETB-1124-1", "ETB-0924-1", "ETB-0924-2"],
    )
    finished = service.run_generation_job(job.id or 0)

    assert finished.status == "completed"
    results = service.job_results(session, finished.id or 0)
    assert len(results) >= 2
    assert {item.group_scope for item in results} >= {"ETB-1124-1", "ETB-0924-1"}

    all_entries = []
    for schedule in results:
        all_entries.extend(session.exec(select(ScheduleEntry).where(ScheduleEntry.schedule_id == schedule.id)).all())

    for i, left in enumerate(all_entries):
        for right in all_entries[i + 1 :]:
            if left.teacher_id != right.teacher_id:
                continue
            if left.day_of_week != right.day_of_week:
                continue
            if not format_week_scope(left.week_scope) or not format_week_scope(right.week_scope):
                continue
            if left.start_time and right.start_time:
                same_time = left.start_time == right.start_time and left.end_time == right.end_time
            else:
                same_time = left.online_slot_number == right.online_slot_number and left.day_of_week == right.day_of_week
            if same_time:
                from app.core.week_scope import scopes_overlap

                assert not scopes_overlap(left.week_scope, right.week_scope)

    etb_group = session.exec(select(Group).where(Group.code == "ETB-1124-1")).first()
    assert etb_group is not None
    latest = service.latest_result_for_scope(session, group_id=etb_group.id or 0, semester=4)
    assert latest is not None
    assert latest.generation_job_id == (finished.id or 0)


def test_real_seed_hides_demo_groups_and_teachers_by_default() -> None:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    session = Session(engine)
    Seeder().seed(
        session,
        BASE_DIR / "data" / "Оқу жоспар_ЕТБ-1124.xls",
        BASE_DIR / "data" / "Үрдіс 2025-2026 оқу жылы соңғысы (1).pdf",
        BASE_DIR / "data" / "2025-2026 ПРОГРАММИСТТЕР_ИНКАР (1) (2) (1).docx",
    )
    service = TimetableService()
    groups = service.list_groups(session)
    teachers = service.list_teachers(session)

    assert all(group.code not in {"DTP-2201", "ETB-2202", "IS-2201"} for group in groups)
    assert all(teacher.full_name not in {"Maksat Nurpeisov", "Aliya Serik"} for teacher in teachers)


def test_duplicate_subject_name_is_rejected() -> None:
    session = build_session()
    service = TimetableService()
    existing = service.list_subjects(session)[0]

    try:
        service.create_subject(
            session,
            {
                "code": "DUPLICATE-SUBJECT-CODE",
                "name": f"  {existing.name}  ",
                "owner_department_id": existing.owner_department_id,
                "lesson_type": existing.lesson_type,
                "requires_special_room": existing.requires_special_room,
                "can_be_online": existing.can_be_online,
                "default_delivery_mode": existing.default_delivery_mode,
            },
        )
    except ValueError as exc:
        assert str(exc) == "Предмет с таким названием уже существует."
    else:
        raise AssertionError("Ожидалась ошибка дублирования предмета.")
