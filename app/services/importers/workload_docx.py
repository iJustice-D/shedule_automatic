from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Iterable

from docx import Document
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph


GROUP_PREFIX_MAP = {
    "ЕТБ": "ETB",
    "ДТП": "DTP",
    "ИС": "IS",
    "БҚ": "BQ",
    "БК": "BK",
}

SUBGROUP_MAP = {
    "A": "A",
    "А": "A",
    "B": "B",
    "Б": "B",
    "В": "B",
    "C": "C",
    "С": "C",
}

IMPORT_PRIORITY = {
    "regular": 100,
    "facultative": 40,
    "practice": 30,
    "study_practice": 30,
    "industrial_practice": 30,
}


@dataclass(slots=True)
class ImportedWeeklyLoadRow:
    group_code: str
    course: int | None
    semester: int
    source_semester_label: str
    subject_name: str
    load_category: str
    subgroup_code: str | None
    teacher_assignment_type: str
    assigned_teacher_names: list[str]
    weekly_hours: float
    weekly_pairs: float
    total_hours: int
    study_weeks: int
    is_facultative: bool
    is_practice: bool
    practice_type: str
    source_file: str
    raw_import_notes: str
    raw_teacher_names: str
    source_priority: int


class WeeklyWorkloadDocxImporter:
    HEADER_GROUP_RE = re.compile(r"(?P<group>[A-Za-zА-ЯӘІҢҒҮҰҚӨҺа-яәіңғүұқөһ]+\s*-\s*\d{4}-\d)")
    COURSE_RE = re.compile(r"(?P<roman>[IVXІ]+)\s*курс", re.IGNORECASE)
    HEADER_WEEKS_RE = re.compile(r"(\d+)\s*апта", re.IGNORECASE)
    NAME_RE = re.compile(r"[A-ZА-ЯӘІҢҒҮҰҚӨҺ][A-Za-zА-Яа-яӘәІіҢңҒғҮүҰұҚқӨөҺһ\-]+(?:\s+[A-ZА-ЯӘІҢҒҮҰҚӨҺ]\.){1,2}")

    def import_rows(
        self,
        path: Path,
        semester_map: tuple[int, int] = (3, 4),
        target_group_codes: Iterable[str] | None = None,
    ) -> list[ImportedWeeklyLoadRow]:
        document = Document(path)
        target_codes = set(target_group_codes or [])
        current_group_code: str | None = None
        current_course: int | None = None
        pending_category = "regular"
        rows: list[ImportedWeeklyLoadRow] = []

        for block_type, block in self._iter_blocks(document):
            if block_type == "paragraph":
                text = self._normalize_spaces(block.text)
                if not text:
                    continue
                parsed_group = self._parse_group_header(text)
                if parsed_group is not None:
                    current_group_code, current_course = parsed_group
                    pending_category = "regular"
                    continue
                lowered = text.lower()
                if "факультатив" in lowered:
                    pending_category = "facultative"
                elif "тәжірибе" in lowered:
                    pending_category = "practice"
                continue

            if current_group_code is None:
                continue
            if target_codes and current_group_code not in target_codes:
                continue
            if not self._is_load_table(block):
                continue
            rows.extend(
                self._parse_load_table(
                    block,
                    group_code=current_group_code,
                    course=current_course,
                    category=pending_category,
                    source_file=path.name,
                    semester_map=semester_map,
                )
            )
            pending_category = "regular"
        return rows

    def _parse_load_table(
        self,
        table: Table,
        *,
        group_code: str,
        course: int | None,
        category: str,
        source_file: str,
        semester_map: tuple[int, int],
    ) -> list[ImportedWeeklyLoadRow]:
        rows: list[ImportedWeeklyLoadRow] = []
        if len(table.rows) < 3:
            return rows
        header = [self._normalize_spaces(cell.text) for cell in table.rows[1].cells]
        subject_col = self._find_column(header, "пән аттары")
        total_col = self._find_column(header, "жалп")
        teacher_col = self._find_column(header, "оқытушының")
        sem_columns = self._semester_columns(header, semester_map)
        if subject_col is None or total_col is None or teacher_col is None or not sem_columns:
            return rows

        for row in table.rows[2:]:
            cells = [self._normalize_spaces(cell.text) for cell in row.cells]
            subject_text = cells[subject_col]
            if not subject_text or "барлығы" in subject_text.lower():
                continue
            raw_teacher_names = cells[teacher_col]
            subject_name, subgroup_code, load_category, is_facultative, is_practice, practice_type = self._subject_metadata(
                subject_text,
                category,
            )
            total_values = self._parse_numbers(cells[total_col])
            semester_values = {
                source_label: self._parse_numbers(cells[column_index])
                for source_label, (_semester_number, _study_weeks, column_index) in sem_columns.items()
            }
            teacher_units = self._split_teacher_units(raw_teacher_names)
            split_rows = self._expand_row_units(subject_name, subgroup_code, teacher_units, total_values, semester_values)

            for item in split_rows:
                row_subgroup = item["subgroup_code"] or subgroup_code
                assignment_state = item["assignment_state"]
                assigned_teacher_names = item["teacher_names"]
                raw_notes = item["raw_notes"]
                for source_label, (semester_number, study_weeks, _column_index) in sem_columns.items():
                    semester_hours = int(item["semester_hours"].get(source_label, 0) or 0)
                    if semester_hours <= 0:
                        continue
                    actual_study_weeks = study_weeks or 0
                    weekly_hours = round(semester_hours / actual_study_weeks, 2) if actual_study_weeks else float(semester_hours)
                    weekly_pairs = round(weekly_hours / 2, 2)
                    rows.append(
                        ImportedWeeklyLoadRow(
                            group_code=group_code,
                            course=course,
                            semester=semester_number,
                            source_semester_label=source_label,
                            subject_name=subject_name,
                            load_category=load_category,
                            subgroup_code=row_subgroup,
                            teacher_assignment_type=assignment_state,
                            assigned_teacher_names=assigned_teacher_names,
                            weekly_hours=weekly_hours,
                            weekly_pairs=weekly_pairs,
                            total_hours=semester_hours,
                            study_weeks=actual_study_weeks,
                            is_facultative=is_facultative,
                            is_practice=is_practice,
                            practice_type=practice_type,
                            source_file=source_file,
                            raw_import_notes=raw_notes,
                            raw_teacher_names=raw_teacher_names,
                            source_priority=IMPORT_PRIORITY.get(load_category, 50),
                        )
                    )
        return rows

    def _expand_row_units(
        self,
        subject_name: str,
        subgroup_code: str | None,
        teacher_units: list[dict[str, str]],
        total_values: list[int],
        semester_values: dict[str, list[int]],
    ) -> list[dict]:
        counts = [len(total_values), *(len(values) for values in semester_values.values() if values)]
        split_count = max(counts or [0])
        if split_count > 1 and split_count == len(teacher_units):
            rows: list[dict] = []
            for index, teacher_unit in enumerate(teacher_units):
                row_subgroup = subgroup_code or self._subgroup_from_index(index)
                rows.append(
                    {
                        "teacher_names": [teacher_unit["name"]] if teacher_unit["name"] else [],
                        "assignment_state": teacher_unit["state"],
                        "raw_notes": f"Автоматически разделено по преподавателям для предмета «{subject_name}».",
                        "subgroup_code": row_subgroup,
                        "semester_hours": {
                            label: self._pick_value(values, index)
                            for label, values in semester_values.items()
                        },
                    }
                )
            return rows

        teacher_names = [item["name"] for item in teacher_units if item["name"]]
        return [
            {
                "teacher_names": teacher_names,
                "assignment_state": self._overall_assignment_state(teacher_units),
                "raw_notes": "",
                "subgroup_code": subgroup_code,
                "semester_hours": {
                    label: sum(values)
                    for label, values in semester_values.items()
                },
            }
        ]

    @staticmethod
    def _pick_value(values: list[int], index: int) -> int:
        if not values:
            return 0
        if index < len(values):
            return values[index]
        return values[-1]

    @staticmethod
    def _subgroup_from_index(index: int) -> str | None:
        subgroup_codes = ("A", "B", "C")
        if index < len(subgroup_codes):
            return subgroup_codes[index]
        return None

    def _split_teacher_units(self, raw_teacher_names: str) -> list[dict[str, str]]:
        lowered = raw_teacher_names.lower()
        names = self.NAME_RE.findall(raw_teacher_names)
        units = [{"name": self._normalize_spaces(name), "state": "fixed"} for name in names]
        if "вакансия" in lowered:
            units.append({"name": "Вакансия", "state": "vacancy"})
        if "өндіріс жетекшісі" in lowered:
            units.append({"name": "Өндіріс жетекшісі", "state": "unresolved_manual_review"})
        if not units:
            units.append({"name": "", "state": "unresolved_manual_review"})
        return units

    @staticmethod
    def _overall_assignment_state(teacher_units: list[dict[str, str]]) -> str:
        states = {item["state"] for item in teacher_units}
        if states == {"fixed"} and len(teacher_units) == 1:
            return "fixed"
        if states == {"vacancy"}:
            return "vacancy"
        if "vacancy" in states or "unresolved_manual_review" in states:
            return "unresolved_manual_review"
        if len(teacher_units) > 1:
            return "multi_teacher_ambiguous"
        return next(iter(states), "unresolved_manual_review")

    def _subject_metadata(self, subject_text: str, category: str) -> tuple[str, str | None, str, bool, bool, str]:
        normalized = self._normalize_spaces(subject_text)
        subgroup_match = re.search(r"([AАBБВCС])[- ]*тобы", normalized, re.IGNORECASE)
        subgroup_code = None
        if subgroup_match:
            subgroup_code = SUBGROUP_MAP.get(subgroup_match.group(1).upper())
            normalized = re.sub(r"[AАBБВCС][-\s]*тобы", "", normalized, flags=re.IGNORECASE).strip()

        lowered = normalized.lower()
        load_category = "regular"
        is_practice = False
        is_facultative = category == "facultative"
        practice_type = ""
        if "өндірістік тәжірибе" in lowered:
            load_category = "industrial_practice"
            is_practice = True
            practice_type = "industrial_practice"
            normalized = re.sub("өндірістік тәжірибе", "", normalized, flags=re.IGNORECASE).strip()
        elif "оқу тәжірибесі" in lowered or "оқу тәжірибе" in lowered:
            load_category = "study_practice"
            is_practice = True
            practice_type = "study_practice"
            normalized = re.sub("оқу тәжірибес[іи]", "", normalized, flags=re.IGNORECASE).strip()
            normalized = re.sub("оқу тәжірибе", "", normalized, flags=re.IGNORECASE).strip()
        elif category == "practice":
            load_category = "practice"
            is_practice = True
            practice_type = "practice"
        elif category == "facultative":
            load_category = "facultative"
        normalized = self._normalize_spaces(normalized)
        return normalized, subgroup_code, load_category, is_facultative, is_practice, practice_type

    def _parse_group_header(self, text: str) -> tuple[str, int | None] | None:
        match = self.HEADER_GROUP_RE.search(text)
        if not match:
            return None
        raw_group_code = self._normalize_group_code(match.group("group"))
        course_match = self.COURSE_RE.search(text)
        course = self._roman_to_int(course_match.group("roman")) if course_match else None
        return raw_group_code, course

    @staticmethod
    def _normalize_group_code(raw_group_code: str) -> str:
        text = raw_group_code.replace(" ", "")
        prefix, suffix = text.split("-", 1)
        prefix = GROUP_PREFIX_MAP.get(prefix.upper(), WeeklyWorkloadDocxImporter._latinize_text(prefix))
        return f"{prefix}-{suffix}"

    @staticmethod
    def _latinize_text(value: str) -> str:
        char_map = {
            "А": "A",
            "В": "B",
            "Е": "E",
            "И": "I",
            "К": "K",
            "М": "M",
            "Н": "N",
            "О": "O",
            "Р": "R",
            "С": "S",
            "Т": "T",
            "Б": "B",
            "Д": "D",
            "П": "P",
            "Қ": "Q",
            "Ұ": "U",
            "Ү": "U",
        }
        return "".join(char_map.get(char.upper(), char.upper()) for char in value)

    def _semester_columns(self, header: list[str], semester_map: tuple[int, int]) -> dict[str, tuple[int, int, int]]:
        columns: dict[str, tuple[int, int, int]] = {}
        sem_index = 0
        for index, cell in enumerate(header):
            lowered = cell.lower()
            if "сем" not in lowered:
                continue
            if sem_index >= 2:
                break
            label = "I" if sem_index == 0 else "II"
            weeks_match = self.HEADER_WEEKS_RE.search(cell)
            weeks = int(weeks_match.group(1)) if weeks_match else 0
            columns[label] = (semester_map[sem_index], weeks, index)
            sem_index += 1
        return columns

    @staticmethod
    def _find_column(header: list[str], token: str) -> int | None:
        lowered_token = token.lower()
        for index, value in enumerate(header):
            if lowered_token in value.lower():
                return index
        return None

    @staticmethod
    def _parse_numbers(value: str) -> list[int]:
        return [int(number) for number in re.findall(r"\d+", value)]

    @staticmethod
    def _normalize_spaces(value: str) -> str:
        return " ".join(value.replace("\xa0", " ").split())

    @staticmethod
    def _is_load_table(table: Table) -> bool:
        if not table.rows:
            return False
        first_row = " | ".join(cell.text.replace("\n", " ").strip() for cell in table.rows[0].cells).lower()
        return "пән аттары" in first_row and ("апталық сағат" in first_row or "жалп" in first_row)

    @staticmethod
    def _iter_blocks(document: Document):
        for child in document.element.body.iterchildren():
            if child.tag == qn("w:p"):
                yield "paragraph", Paragraph(child, document)
            elif child.tag == qn("w:tbl"):
                yield "table", Table(child, document)

    @staticmethod
    def _roman_to_int(value: str | None) -> int | None:
        if not value:
            return None
        normalized = value.upper().replace("І", "I")
        mapping = {"I": 1, "II": 2, "III": 3, "IV": 4}
        return mapping.get(normalized)
