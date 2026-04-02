from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import pandas as pd


SEMESTER_COLUMNS = {
    1: 13,
    2: 14,
    3: 15,
    4: 16,
    5: 17,
    6: 18,
}


@dataclass(slots=True)
class ImportedCurriculumLoad:
    group_code: str
    subject_code: str
    subject_name: str
    semester: int
    schedulable_hours: int
    raw_total_hours: int
    theory_hours: int
    lab_hours: int
    practice_hours: int
    control_form: str
    lesson_type: str
    requires_special_room: bool
    can_be_online: bool
    default_delivery_mode: str
    warnings: list[str] = field(default_factory=list)

    @property
    def module_code(self) -> str:
        return self.subject_code.rsplit("-S", 1)[0]

    @property
    def module_name(self) -> str:
        return self.subject_name


@dataclass(slots=True)
class CurriculumImportPreview:
    file_name: str
    file_path: str
    detected_group_code: str
    detected_specialty: str
    detected_qualification: str
    sheet_name: str
    semester_totals: dict[int, dict[str, int]]
    rows: list[ImportedCurriculumLoad]
    warnings: list[str] = field(default_factory=list)


class CurriculumXlsImporter:
    def preview_group_loads(
        self,
        path: Path,
        group_code: str = "ETB-1124-1",
        semesters: Iterable[int] = (3, 4),
    ) -> CurriculumImportPreview:
        workbook = pd.ExcelFile(path)
        sheet_name = self._detect_plan_sheet(workbook)
        df = pd.read_excel(path, sheet_name=sheet_name, header=None)

        specialty = self._extract_cover_value(workbook, "Мамандығы")
        qualification = self._extract_cover_value(workbook, "Біліктілігі")
        detected_group = self._detect_group_code(path, fallback=group_code)

        rows: list[ImportedCurriculumLoad] = []
        warnings: list[str] = []
        semester_totals = {
            semester: {"raw_hours": 0, "schedulable_hours": 0}
            for semester in semesters
        }

        for _, row in df.iterrows():
            code = self._cell_text(row.get(1))
            name = self._cell_text(row.get(2))
            if not self._is_schedulable_module(code, name):
                continue

            total_hours = self._to_int(row.get(8))
            theory_hours = self._to_int(row.get(9))
            lab_hours = self._to_int(row.get(10))
            practice_hours = self._to_int(row.get(12))
            classroom_total = theory_hours + lab_hours
            if classroom_total == 0:
                classroom_total = max(total_hours - practice_hours, 0)

            semester_total = sum(self._to_int(row.get(SEMESTER_COLUMNS[semester])) for semester in range(1, 7))
            if semester_total == 0:
                semester_total = total_hours

            for semester in semesters:
                semester_hours = self._to_int(row.get(SEMESTER_COLUMNS[semester]))
                if semester_hours <= 0:
                    continue
                ratio = semester_hours / semester_total if semester_total else 0
                schedulable_hours = self._round_to_even(classroom_total * ratio if classroom_total else semester_hours)
                row_warnings: list[str] = []
                if schedulable_hours <= 0:
                    row_warnings.append("Не удалось определить аудиторную часть нагрузки, проверьте строку вручную.")
                    schedulable_hours = self._round_to_even(semester_hours)
                imported = ImportedCurriculumLoad(
                    group_code=detected_group,
                    subject_code=f"{self._normalize_code(code)}-S{semester}",
                    subject_name=name,
                    semester=semester,
                    schedulable_hours=schedulable_hours,
                    raw_total_hours=semester_hours,
                    theory_hours=self._round_to_even(theory_hours * ratio if semester_total else theory_hours),
                    lab_hours=self._round_to_even(lab_hours * ratio if semester_total else lab_hours),
                    practice_hours=self._round_to_even(practice_hours * ratio if semester_total else practice_hours),
                    control_form=self._detect_control_form(row),
                    lesson_type=self._detect_lesson_type(theory_hours, lab_hours),
                    requires_special_room=self._requires_special_room(name),
                    can_be_online=self._can_be_online(name, theory_hours, lab_hours),
                    default_delivery_mode="offline",
                    warnings=row_warnings,
                )
                rows.append(imported)
                semester_totals[semester]["raw_hours"] += imported.raw_total_hours
                semester_totals[semester]["schedulable_hours"] += imported.schedulable_hours

        if not rows:
            warnings.append("Не удалось найти строки БМ/КМ с часами для семестров 3 и 4.")

        return CurriculumImportPreview(
            file_name=path.name,
            file_path=str(path),
            detected_group_code=detected_group,
            detected_specialty=specialty,
            detected_qualification=qualification,
            sheet_name=sheet_name,
            semester_totals=semester_totals,
            rows=rows,
            warnings=warnings,
        )

    def import_group_loads(
        self,
        path: Path,
        semesters: Iterable[int] = (3, 4),
    ) -> list[ImportedCurriculumLoad]:
        return self.preview_group_loads(path, semesters=semesters).rows

    @staticmethod
    def _detect_plan_sheet(workbook: pd.ExcelFile) -> str:
        for sheet_name in workbook.sheet_names:
            df = workbook.parse(sheet_name=sheet_name, header=None, nrows=12)
            flattened = " ".join(CurriculumXlsImporter._cell_text(value) for value in df.fillna("").to_numpy().flatten())
            if "Модульдердің/пәндердің" in flattened and "сем." in flattened:
                return sheet_name
        return workbook.sheet_names[1] if len(workbook.sheet_names) > 1 else workbook.sheet_names[0]

    @staticmethod
    def _extract_cover_value(workbook: pd.ExcelFile, marker: str) -> str:
        first_sheet = workbook.sheet_names[0]
        df = workbook.parse(sheet_name=first_sheet, header=None)
        for row in df.itertuples(index=False):
            cells = [CurriculumXlsImporter._cell_text(value) for value in row if CurriculumXlsImporter._cell_text(value)]
            joined = " ".join(cells)
            if marker in joined:
                return joined
        return ""

    @staticmethod
    def _detect_group_code(path: Path, fallback: str) -> str:
        stem = path.stem.upper().replace("_", " ")
        if "ЕТБ-1124" in stem or "ETB-1124" in stem:
            return "ETB-1124-1"
        return fallback

    @staticmethod
    def _cell_text(value: object) -> str:
        if pd.isna(value):
            return ""
        return str(value).replace("\n", " ").strip()

    @staticmethod
    def _to_int(value: object) -> int:
        if pd.isna(value):
            return 0
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _round_to_even(value: float) -> int:
        rounded = int(round(value))
        if rounded <= 0:
            return 0
        if rounded % 2:
            rounded += 1
        return rounded

    @staticmethod
    def _normalize_code(code: str) -> str:
        return code.replace(" ", "").upper()

    @staticmethod
    def _is_schedulable_module(code: str, name: str) -> bool:
        normalized_code = code.replace(" ", "").upper()
        if normalized_code in {"БМ00", "КМ00"}:
            return False
        if not normalized_code.startswith(("БМ", "КМ", "BM", "KM")):
            return False
        lowered = name.lower()
        excluded = ("аттестация", "қортынды", "қорытынды", "аралық")
        return not any(word in lowered for word in excluded)

    @staticmethod
    def _detect_control_form(row: pd.Series) -> str:
        items: list[str] = []
        exams = CurriculumXlsImporter._cell_text(row.get(3))
        tests = CurriculumXlsImporter._cell_text(row.get(4))
        course_project = CurriculumXlsImporter._cell_text(row.get(5))
        control_work = CurriculumXlsImporter._cell_text(row.get(6))
        if exams:
            items.append("экзамен")
        if tests:
            items.append("зачёт")
        if course_project:
            items.append("курсовой проект")
        if control_work:
            items.append("контрольная работа")
        return ", ".join(items) if items else "не указано"

    @staticmethod
    def _detect_lesson_type(theory_hours: int, practice_hours: int) -> str:
        if theory_hours and practice_hours:
            return "mixed"
        if practice_hours:
            return "practice"
        return "lecture"

    @staticmethod
    def _requires_special_room(name: str) -> bool:
        lowered = name.lower()
        keywords = ("веб", "web", "мобиль", "бұлт", "бизнес-талдау", "orm", "мәліметтер базасы")
        return any(keyword in lowered for keyword in keywords)

    @staticmethod
    def _can_be_online(name: str, theory_hours: int, practice_hours: int) -> bool:
        lowered = name.lower()
        theory_like_keywords = (
            "эконом",
            "business",
            "талдау",
            "analysis",
            "english",
            "әлеумет",
            "құқық",
            "теория",
        )
        if any(keyword in lowered for keyword in theory_like_keywords):
            return True
        return theory_hours >= practice_hours
