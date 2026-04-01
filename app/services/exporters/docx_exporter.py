from __future__ import annotations

from pathlib import Path

from docx import Document
from sqlmodel import Session

from app.core.timetable import DAYS, PAIR_NUMBERS
from app.services.exporters.context import build_schedule_context


class DocxExporter:
    def export(self, session: Session, schedule_id: int, output_path: Path) -> Path:
        context = build_schedule_context(session, schedule_id)
        document = Document()
        for title, grid in context["group_grids"].items():
            document.add_heading(title, level=2)
            document.add_paragraph("Основное расписание")
            table = document.add_table(rows=1 + len(DAYS), cols=1 + len(PAIR_NUMBERS))
            table.cell(0, 0).text = "День / Пара"
            for column, pair_number in enumerate(PAIR_NUMBERS, start=1):
                table.cell(0, column).text = context["pair_headers"][pair_number]
            for row, (day_of_week, _day_key) in enumerate(DAYS.items(), start=1):
                table.cell(row, 0).text = context["day_labels"][day_of_week]
                for column, pair_number in enumerate(PAIR_NUMBERS, start=1):
                    table.cell(row, column).text = grid.get((day_of_week, pair_number), "")
            document.add_paragraph("Дополнительные онлайн-занятия")
            online_rows = context["group_online_rows"].get(title, [])
            online_table = document.add_table(rows=1 + max(len(online_rows), 1), cols=6)
            for column, label in enumerate(["День", "Онлайн-слот", "Предмет", "Преподаватель", "Формат", "Недели"]):
                online_table.cell(0, column).text = label
            if online_rows:
                for row, item in enumerate(online_rows, start=1):
                    online_table.cell(row, 0).text = item["day"]
                    online_table.cell(row, 1).text = item["online_slot"]
                    online_table.cell(row, 2).text = item["subject"]
                    online_table.cell(row, 3).text = item["teacher"]
                    online_table.cell(row, 4).text = item["format"]
                    online_table.cell(row, 5).text = item["weeks"]
            else:
                online_table.cell(1, 0).text = "Онлайн-занятия не назначены"
            document.add_paragraph("")
        document.save(output_path)
        return output_path
