from __future__ import annotations

from pathlib import Path

from sqlmodel import Session, SQLModel, create_engine, select

from app.models import Conflict, ScheduleEntry, WeeklyLoad
from app.services.importers.academic_calendar_pdf import AcademicCalendarPdfImporter
from app.services.importers.curriculum_xls import CurriculumXlsImporter
from app.services.importers.workload_docx import WeeklyWorkloadDocxImporter
from app.services.seeding import Seeder
from app.services.timetable_service import TimetableService


BASE_DIR = Path(__file__).resolve().parents[2]


def test_curriculum_importer_extracts_etb_semesters() -> None:
    importer = CurriculumXlsImporter()
    loads = importer.import_group_loads(BASE_DIR / "data" / "Оқу жоспар_ЕТБ-1124.xls")
    assert any(load.subject_name == "Веб-сайтты  ақпарттық және техникалық қолдау" and load.semester == 3 for load in loads)
    assert any(load.subject_name == "Мобильді қосымшаларды әзірлеу" and load.semester == 4 for load in loads)
    assert all(load.schedulable_hours % 2 == 0 for load in loads)


def test_calendar_importer_builds_real_etb_week_map() -> None:
    importer = AcademicCalendarPdfImporter()
    periods = importer.import_group_periods(BASE_DIR / "data" / "Үрдіс 2025-2026 оқу жылы соңғысы (1).pdf", "ETB-1124-1")
    sem3 = [item for item in periods if item.semester == 3 and item.is_schedulable]
    sem4 = [item for item in periods if item.semester == 4 and item.is_schedulable]
    assert len(sem3) == 8
    assert len(sem4) == 8
    assert any(item.week_number == 42 and item.period_type == "final_attestation" for item in periods)


def test_weekly_workload_docx_importer_preserves_categories_and_assignments() -> None:
    importer = WeeklyWorkloadDocxImporter()
    rows = importer.import_rows(
        BASE_DIR / "data" / "2025-2026 ПРОГРАММИСТТЕР_ИНКАР (1) (2) (1).docx",
        target_group_codes=["ETB-1124-1"],
    )
    assert rows
    assert any(row.load_category == "regular" for row in rows)
    assert any(row.load_category == "facultative" for row in rows)
    assert any(row.is_practice for row in rows)
    assert any(row.subgroup_code in {"A", "B"} for row in rows)
    assert any(row.teacher_assignment_type == "vacancy" for row in rows)
    assert any(row.teacher_assignment_type in {"fixed", "multi_teacher", "unresolved_manual_review"} for row in rows)


def test_weekly_workload_rows_drive_generation_without_teacher_parallel_conflicts() -> None:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    session = Session(engine)
    Seeder().seed(
        session,
        BASE_DIR / "data" / "Оқу жоспар_ЕТБ-1124.xls",
        BASE_DIR / "data" / "Үрдіс 2025-2026 оқу жылы соңғысы (1).pdf",
    )
    service = TimetableService()
    imported_rows = service.import_weekly_workload(
        session,
        BASE_DIR / "data" / "2025-2026 ПРОГРАММИСТТЕР_ИНКАР (1) (2) (1).docx",
        calendar_path=BASE_DIR / "data" / "Үрдіс 2025-2026 оқу жылы соңғысы (1).pdf",
        curriculum_path=BASE_DIR / "data" / "Оқу жоспар_ЕТБ-1124.xls",
        group_codes=["ETB-1124-1"],
    )
    assert imported_rows
    assert session.exec(select(WeeklyLoad).where(WeeklyLoad.group_id.is_not(None))).all()

    schedule = service.generate_schedule(session, semester=3, group_codes=["ETB-1124-1"], name="DOCX weekly import")
    entries = session.exec(select(ScheduleEntry).where(ScheduleEntry.schedule_id == schedule.id)).all()
    conflicts = session.exec(select(Conflict).where(Conflict.schedule_id == schedule.id)).all()

    assert entries
    hard_types = {item.type for item in conflicts if item.severity == "hard"}
    assert "teacher_double_booked" not in hard_types
    assert "group_double_booked" not in hard_types
