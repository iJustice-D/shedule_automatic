from __future__ import annotations

from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from sqlmodel import Session

from app.core.timetable import DAYS, PAIR_NUMBERS
from app.services.exporters.context import build_schedule_context


class PdfExporter:
    FONT_NAME = "ScheduleUnicode"
    FONT_CANDIDATES = (
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/Supplemental/Times New Roman.ttf",
        "/System/Library/Fonts/Supplemental/Verdana.ttf",
        "/System/Library/Fonts/Supplemental/Tahoma.ttf",
    )

    def export(self, session: Session, schedule_id: int, output_path: Path) -> Path:
        context = build_schedule_context(session, schedule_id)
        font_name = self._ensure_font()
        styles = getSampleStyleSheet()
        for style_name in ("Normal", "Heading2", "Heading3", "BodyText"):
            styles[style_name].fontName = font_name
            styles[style_name].leading = max(styles[style_name].leading, 14)
        story = []
        for title, grid in context["group_grids"].items():
            story.append(Paragraph(f"<b>{escape(title)}</b>", styles["Heading2"]))
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
                        ("FONTNAME", (0, 0), (-1, -1), font_name),
                        ("GRID", (0, 0), (-1, -1), 0.5, colors.black),
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                        ("LEADING", (0, 0), (-1, -1), 12),
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
                        ("FONTNAME", (0, 0), (-1, -1), font_name),
                        ("GRID", (0, 0), (-1, -1), 0.5, colors.black),
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                        ("LEADING", (0, 0), (-1, -1), 12),
                    ]
                )
            )
            story.append(online_table)
            story.append(Spacer(1, 20))
        story.append(Paragraph("<b>Баланс нагрузки преподавателей</b>", styles["Heading2"]))
        balance_rows = context.get("teacher_balance_rows", [])
        balance_data = [["Преподаватель", "Семестр 3", "Семестр 4", "Отклонение", "На уточнении"]]
        if balance_rows:
            for item in balance_rows:
                balance_data.append(
                    [
                        str(item.get("teacher_name", "")),
                        str(item.get("semester_3_pairs", "")),
                        str(item.get("semester_4_pairs", "")),
                        str(item.get("normalized_balance_score", "")),
                        str(item.get("pending_rows", "")),
                    ]
                )
        else:
            balance_data.append(["Данные отсутствуют", "", "", "", ""])
        balance_table = Table(balance_data, repeatRows=1)
        balance_table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E6B800")),
                    ("FONTNAME", (0, 0), (-1, -1), font_name),
                    ("GRID", (0, 0), (-1, -1), 0.5, colors.black),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ]
            )
        )
        story.append(balance_table)
        story.append(Spacer(1, 12))
        story.append(Paragraph("<b>Вакансии и неразрешённые строки</b>", styles["Heading2"]))
        unresolved_rows = context.get("unresolved_weekly_rows_report", [])
        unresolved_data = [["Группа", "Семестр", "Предмет", "Подгруппа", "Состояние", "Преподаватели"]]
        if unresolved_rows:
            for item in unresolved_rows:
                unresolved_data.append(
                    [
                        str(item.get("group", "")),
                        str(item.get("semester", "")),
                        str(item.get("subject", "")),
                        str(item.get("subgroup", "")),
                        str(item.get("assignment_state", "")),
                        str(item.get("teacher_names", "")),
                    ]
                )
        else:
            unresolved_data.append(["Неразрешённых строк нет", "", "", "", "", ""])
        unresolved_table = Table(unresolved_data, repeatRows=1)
        unresolved_table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#FBD38D")),
                    ("FONTNAME", (0, 0), (-1, -1), font_name),
                    ("GRID", (0, 0), (-1, -1), 0.5, colors.black),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ]
            )
        )
        story.append(unresolved_table)
        doc = SimpleDocTemplate(str(output_path), pagesize=landscape(A4))
        doc.build(story)
        return output_path

    def _ensure_font(self) -> str:
        if self.FONT_NAME in pdfmetrics.getRegisteredFontNames():
            return self.FONT_NAME
        for candidate in self.FONT_CANDIDATES:
            path = Path(candidate)
            if not path.exists():
                continue
            pdfmetrics.registerFont(TTFont(self.FONT_NAME, str(path)))
            pdfmetrics.registerFontFamily(
                self.FONT_NAME,
                normal=self.FONT_NAME,
                bold=self.FONT_NAME,
                italic=self.FONT_NAME,
                boldItalic=self.FONT_NAME,
            )
            return self.FONT_NAME
        return "Helvetica"
