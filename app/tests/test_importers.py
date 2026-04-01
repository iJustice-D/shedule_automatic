from __future__ import annotations

from pathlib import Path

from app.services.importers.academic_calendar_pdf import AcademicCalendarPdfImporter
from app.services.importers.curriculum_xls import CurriculumXlsImporter


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
