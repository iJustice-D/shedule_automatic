from __future__ import annotations

from pathlib import Path

from sqlmodel import Session, SQLModel, create_engine

from app.services.importers.academic_calendar_pdf import AcademicCalendarPdfImporter
from app.services.importers.curriculum_xls import CurriculumXlsImporter
from app.services.seeding import Seeder
from app.services.timetable_service import TimetableService


BASE_DIR = Path(__file__).resolve().parents[2]
CURRICULUM_PATH = next((BASE_DIR / "data").glob("*.xls"))
CALENDAR_PATH = next((BASE_DIR / "data").glob("*.pdf"))


def test_curriculum_importer_extracts_etb_semesters() -> None:
    importer = CurriculumXlsImporter()
    loads = importer.import_group_loads(CURRICULUM_PATH)
    assert any(load.subject_name == "Веб-сайтты  ақпарттық және техникалық қолдау" and load.semester == 3 for load in loads)
    assert any(load.subject_name == "Мобильді қосымшаларды әзірлеу" and load.semester == 4 for load in loads)
    assert all(load.schedulable_hours % 2 == 0 for load in loads)


def test_calendar_importer_builds_real_etb_week_map() -> None:
    importer = AcademicCalendarPdfImporter()
    periods = importer.import_group_periods(CALENDAR_PATH, "ETB-1124-1")
    sem3 = [item for item in periods if item.semester == 3 and item.is_schedulable]
    sem4 = [item for item in periods if item.semester == 4 and item.is_schedulable]
    assert len(sem3) == 8
    assert len(sem4) == 8
    assert any(item.week_number == 42 and item.period_type == "final_attestation" for item in periods)


def test_curriculum_preview_detects_specialty_and_semester_totals() -> None:
    preview = CurriculumXlsImporter().preview_group_loads(CURRICULUM_PATH)
    assert preview.detected_group_code == "ETB-1124-1"
    assert "06130100" in preview.detected_specialty
    assert preview.semester_totals[3]["schedulable_hours"] == 300
    assert preview.semester_totals[4]["schedulable_hours"] == 312


def test_calendar_preview_preserves_row_excerpt_and_warning() -> None:
    preview = AcademicCalendarPdfImporter().preview_group_periods(CALENDAR_PATH, "ETB-1124-1")
    assert "ЕТБ-1124-1" in preview.row_excerpt
    assert len(preview.periods) == 52
    assert any("числовые маркеры" in warning for warning in preview.warnings)


def test_generation_setup_uses_real_imported_study_weeks() -> None:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    session = Session(engine)
    Seeder().seed(session, CURRICULUM_PATH, CALENDAR_PATH)
    service = TimetableService()

    preview = service.build_generation_setup(session, 3, ["ETB-1124-1"])

    assert preview["rows"]
    assert any(row["module_code"] == "КМ05" and row["study_weeks_available"] == 8 for row in preview["rows"])
    assert any("ETB-1124-1" in warning for warning in preview["warnings"])
