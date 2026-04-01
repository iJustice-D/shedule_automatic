from __future__ import annotations

from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from sqlmodel import Session

from app.core.timetable import DAYS, PAIR_NUMBERS
from app.services.exporters.context import build_schedule_context


class PdfExporter:
    def export(self, session: Session, schedule_id: int, output_path: Path) -> Path:
        context = build_schedule_context(session, schedule_id)
        styles = getSampleStyleSheet()
        story = []
        for title, grid in context["group_grids"].items():
            story.append(Paragraph(f"<b>{title}</b>", styles["Heading2"]))
            story.append(Paragraph("<b>Основное расписание</b>", styles["Heading3"]))
            data = [["День / Пара", *[context["pair_headers"][pair] for pair in PAIR_NUMBERS]]]
            for day_of_week, day_key in DAYS.items():
                row = [context["day_labels"][day_of_week]]
                for pair_number in PAIR_NUMBERS:
                    row.append(grid.get((day_of_week, pair_number), ""))
                data.append(row)
            table = Table(data, repeatRows=1)
            table.setStyle(
                TableStyle(
                    [
                        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E6B800")),
                        ("BACKGROUND", (0, 1), (0, -1), colors.HexColor("#E6B800")),
                        ("GRID", (0, 0), (-1, -1), 0.5, colors.black),
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ]
                )
            )
            story.append(table)
            story.append(Spacer(1, 10))
            story.append(Paragraph("<b>Дополнительные онлайн-занятия</b>", styles["Heading3"]))
            online_rows = context["group_online_rows"].get(title, [])
            online_data = [["День", "Онлайн-слот", "Предмет", "Преподаватель", "Формат", "Недели"]]
            if online_rows:
                for item in online_rows:
                    online_data.append([item["day"], item["online_slot"], item["subject"], item["teacher"], item["format"], item["weeks"]])
            else:
                online_data.append(["Онлайн-занятия не назначены", "", "", "", "", ""])
            online_table = Table(online_data, repeatRows=1)
            online_table.setStyle(
                TableStyle(
                    [
                        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#B7E3F9")),
                        ("GRID", (0, 0), (-1, -1), 0.5, colors.black),
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ]
                )
            )
            story.append(online_table)
            story.append(Spacer(1, 20))
        doc = SimpleDocTemplate(str(output_path), pagesize=landscape(A4))
        doc.build(story)
        return output_path
