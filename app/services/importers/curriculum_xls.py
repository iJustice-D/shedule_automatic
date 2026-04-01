from __future__ import annotations

from dataclasses import dataclass
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
    subject_code: str
    subject_name: str
    semester: int
    schedulable_hours: int
    raw_total_hours: int
    practice_hours: int
    lesson_type: str
    requires_special_room: bool
    can_be_online: bool
    default_delivery_mode: str


class CurriculumXlsImporter:
    sheet_name = "план_9 дизайн"

    def import_group_loads(
        self,
        path: Path,
        semesters: Iterable[int] = (3, 4),
    ) -> list[ImportedCurriculumLoad]:
        df = pd.read_excel(path, sheet_name=self.sheet_name, header=None)
        loads: list[ImportedCurriculumLoad] = []
        for _, row in df.iterrows():
            code = row.get(1)
            name = row.get(2)
            if pd.isna(code) or pd.isna(name):
                continue
            code = str(code).strip()
            name = str(name).strip()
            if not self._is_schedulable_subject(code, name):
                continue
            total_hours = self._to_int(row.get(8))
            theory_hours = self._to_int(row.get(9))
            practice_class_hours = self._to_int(row.get(10))
            industrial_hours = self._to_int(row.get(12))
            classroom_total = theory_hours + practice_class_hours
            if classroom_total == 0:
                classroom_total = max(total_hours - industrial_hours, 0)
            semester_total = sum(self._to_int(row.get(SEMESTER_COLUMNS[semester])) for semester in range(1, 7))
            if semester_total == 0:
                semester_total = total_hours
            for semester in semesters:
                sem_hours = self._to_int(row.get(SEMESTER_COLUMNS[semester]))
                if sem_hours <= 0:
                    continue
                ratio = sem_hours / semester_total if semester_total else 0
                schedulable_hours = self._round_to_even(classroom_total * ratio)
                loads.append(
                    ImportedCurriculumLoad(
                        subject_code=f"{code.replace(' ', '')}-S{semester}",
                        subject_name=name,
                        semester=semester,
                        schedulable_hours=schedulable_hours,
                        raw_total_hours=sem_hours,
                        practice_hours=self._round_to_even(industrial_hours * ratio),
                        lesson_type=self._detect_lesson_type(theory_hours, practice_class_hours),
                        requires_special_room=self._requires_special_room(name),
                        can_be_online=self._can_be_online(name, theory_hours, practice_class_hours),
                        default_delivery_mode="offline",
                    )
                )
        return loads

    @staticmethod
    def _to_int(value: object) -> int:
        if pd.isna(value):
            return 0
        return int(float(value))

    @staticmethod
    def _round_to_even(value: float) -> int:
        rounded = int(round(value))
        if rounded % 2:
            rounded += 1
        return max(rounded, 0)

    @staticmethod
    def _is_schedulable_subject(code: str, name: str) -> bool:
        if not (code.startswith("БМ") or code.startswith("КМ")):
            return False
        if code.endswith("00"):
            return False
        lowered = name.lower()
        excluded = ("аттестация", "барлығы", "қортынды", "қорытынды")
        return not any(word in lowered for word in excluded)

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
        keywords = ("веб", "сайт", "мобиль", "ақпарат", "бизнес", "бұлт", "деректер")
        return any(keyword in lowered for keyword in keywords)

    @staticmethod
    def _can_be_online(name: str, theory_hours: int, practice_hours: int) -> bool:
        lowered = name.lower()
        theory_like_keywords = (
            "эконом",
            "business",
            "талдау",
            "analysis",
            "interaction",
            "english",
            "құқық",
            "мәдениет",
            "әлеумет",
            "бизнес",
            "бұлт",
            "резерв",
            "қауіпсіз",
        )
        if any(keyword in lowered for keyword in theory_like_keywords):
            return True
        return theory_hours >= practice_hours
