from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader


@dataclass(slots=True)
class ImportedAcademicPeriod:
    semester: int
    week_number: int
    period_type: str
    is_schedulable: bool


class AcademicCalendarPdfImporter:
    """PDF parsing is intentionally conservative.

    The real source PDF is used to verify the ETB-1124-1 row exists. The week
    sequence is then materialized from a manual mapping derived from that PDF.
    The admin UI allows corrections if a future file changes.
    """

    ETB_1124_1_WEEK_MAP: dict[int, str] = {
        1: "study",
        2: "study",
        3: "study",
        4: "study",
        5: "study",
        6: "study",
        7: "study",
        8: "study",
        9: "industrial_practice",
        10: "industrial_practice",
        11: "industrial_practice",
        12: "industrial_practice",
        13: "industrial_practice",
        14: "industrial_practice",
        15: "industrial_practice",
        16: "study_practice",
        17: "study_practice",
        18: "study_practice",
        19: "study_practice",
        20: "exam_week",
        21: "vacation",
        22: "vacation",
        23: "study",
        24: "study",
        25: "study",
        26: "study",
        27: "study",
        28: "study",
        29: "industrial_practice",
        30: "industrial_practice",
        31: "industrial_practice",
        32: "industrial_practice",
        33: "industrial_practice",
        34: "industrial_practice",
        35: "study_practice",
        36: "study_practice",
        37: "study_practice",
        38: "study_practice",
        39: "study",
        40: "study",
        41: "exam_week",
        42: "final_attestation",
        43: "vacation",
        44: "vacation",
        45: "vacation",
        46: "vacation",
        47: "vacation",
        48: "vacation",
        49: "vacation",
        50: "vacation",
        51: "vacation",
        52: "vacation",
    }

    def import_group_periods(self, path: Path, group_code: str) -> list[ImportedAcademicPeriod]:
        text = PdfReader(path).pages[0].extract_text()
        lookup_codes = {group_code, self._to_cyrillic_group_code(group_code)}
        if not any(code in text for code in lookup_codes):
            raise ValueError(f"Группа {group_code} не найдена в PDF учебного процесса.")
        if group_code != "ETB-1124-1":
            raise ValueError(
                "Автоматический разбор PDF преднастроен только для ETB-1124-1. "
                "Для других групп используйте страницу учебного календаря."
            )
        periods: list[ImportedAcademicPeriod] = []
        for week_number, period_type in self.ETB_1124_1_WEEK_MAP.items():
            semester = 3 if week_number <= 22 else 4
            periods.append(
                ImportedAcademicPeriod(
                    semester=semester,
                    week_number=week_number,
                    period_type=period_type,
                    is_schedulable=period_type == "study",
                )
            )
        return periods

    @staticmethod
    def _to_cyrillic_group_code(group_code: str) -> str:
        translation = str.maketrans({"E": "Е", "T": "Т", "B": "Б", "D": "Д", "P": "П", "I": "І", "S": "С"})
        return group_code.translate(translation)
