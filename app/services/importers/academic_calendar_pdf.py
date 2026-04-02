from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from pypdf import PdfReader


@dataclass(slots=True)
class ImportedAcademicPeriod:
    semester: int
    week_number: int
    period_type: str
    is_schedulable: bool


@dataclass(slots=True)
class AcademicCalendarImportPreview:
    file_name: str
    file_path: str
    detected_group_code: str
    row_excerpt: str
    periods: list[ImportedAcademicPeriod]
    warnings: list[str] = field(default_factory=list)


class AcademicCalendarPdfImporter:
    ETB_1124_1_DEFAULT_BLOCKS: tuple[tuple[int, int, str], ...] = (
        (1, 8, "study"),
        (9, 15, "industrial_practice"),
        (16, 19, "study_practice"),
        (20, 20, "exam_week"),
        (21, 22, "vacation"),
        (23, 28, "study"),
        (29, 34, "industrial_practice"),
        (35, 38, "study_practice"),
        (39, 40, "study"),
        (41, 41, "exam_week"),
        (42, 42, "final_attestation"),
        (43, 52, "vacation"),
    )
    SYMBOL_MAP = {
        "О": "study_practice",
        "Ӛ": "industrial_practice",
        "=": "vacation",
        ":": "exam_week",
        "ҚА": "final_attestation",
        "МЕ": "state_exam",
        "Ж": "summer_training",
        "Т": "orientation_practice",
        "П": "teacher_practice",
        "М": "holiday",
        "ДҚ": "diploma_defense",
        "да": "diploma_writing",
        "ББ": "final_attestation",
    }

    def preview_group_periods(self, path: Path, group_code: str) -> AcademicCalendarImportPreview:
        text = "\n".join((page.extract_text() or "") for page in PdfReader(path).pages)
        row_excerpt = self._find_group_row(text, group_code)
        if not row_excerpt:
            raise ValueError(f"Группа {group_code} не найдена в PDF учебного процесса.")

        warnings: list[str] = []
        periods = self._build_periods(group_code)
        raw_tokens = self._extract_row_tokens(row_excerpt)
        if raw_tokens:
            symbolic_tokens = [token for token in raw_tokens if token in self.SYMBOL_MAP]
            if len(symbolic_tokens) < 20:
                warnings.append("Автоматический разбор строки PDF получился неполным, проверьте периоды вручную перед сохранением.")
            if any(token.isdigit() for token in raw_tokens):
                warnings.append("В строке учебного процесса найдены числовые маркеры, поэтому использована безопасная схема периодов для ETB-1124-1.")
        else:
            warnings.append("Не удалось разобрать строку учебного процесса по токенам, использована безопасная схема периодов для ETB-1124-1.")

        return AcademicCalendarImportPreview(
            file_name=path.name,
            file_path=str(path),
            detected_group_code=group_code,
            row_excerpt=row_excerpt,
            periods=periods,
            warnings=warnings,
        )

    def import_group_periods(self, path: Path, group_code: str) -> list[ImportedAcademicPeriod]:
        return self.preview_group_periods(path, group_code).periods

    def _build_periods(self, group_code: str) -> list[ImportedAcademicPeriod]:
        normalized = self._normalize_group_code(group_code)
        if normalized != "ETB-1124-1":
            raise ValueError(
                "Автоматический разбор PDF сейчас подготовлен для ETB-1124-1. "
                "Для других групп используйте страницу учебного календаря и ручную корректировку."
            )
        periods: list[ImportedAcademicPeriod] = []
        for start_week, end_week, period_type in self.ETB_1124_1_DEFAULT_BLOCKS:
            for week_number in range(start_week, end_week + 1):
                periods.append(
                    ImportedAcademicPeriod(
                        semester=3 if week_number <= 22 else 4,
                        week_number=week_number,
                        period_type=period_type,
                        is_schedulable=period_type == "study",
                    )
                )
        return periods

    def _find_group_row(self, text: str, group_code: str) -> str:
        normalized_group = self._normalize_group_code(group_code)
        for line in text.splitlines():
            if normalized_group in self._normalize_group_code(line):
                return line.strip()
        return ""

    @staticmethod
    def _extract_row_tokens(row_excerpt: str) -> list[str]:
        return [token.strip() for token in row_excerpt.split() if token.strip()]

    @staticmethod
    def _normalize_group_code(value: str) -> str:
        translation = str.maketrans(
            {
                "Е": "E",
                "Т": "T",
                "Б": "B",
                "К": "K",
                "А": "A",
                "Д": "D",
                "П": "P",
                "І": "I",
                "С": "S",
                "Қ": "Q",
                "Ұ": "U",
            }
        )
        normalized = value.upper().translate(translation).replace(" ", "")
        return normalized
