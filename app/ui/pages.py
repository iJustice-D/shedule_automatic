from __future__ import annotations

from contextlib import contextmanager
from typing import Any

from fastapi import FastAPI
from nicegui import ui
from sqlmodel import Session, select

from app.core.config import settings
from app.core.constants import PERIOD_TYPES
from app.core.timetable import (
    DAYS,
    DELIVERY_MODES,
    LESSON_MODE_ONLINE,
    LESSON_MODE_REGULAR,
    LESSON_MODES,
    ONLINE_ALLOWED_DAYS,
    PAIR_NUMBERS,
    SHIFT_VALUES,
    allowed_pairs_for_shift,
    day_label,
    delivery_mode_label,
    online_slot_day,
    online_slot_label,
    online_slot_numbers,
    pair_label,
    pair_time_range,
    shift_label,
    visible_pairs_for_view,
)
from app.db.session import engine
from app.models import AcademicPeriod, AppSetting, Conflict, CurriculumLoad, Department, Group, OnlinePolicy, Room, Schedule, ScheduleEntry, Subject, Suggestion, Teacher
from app.services.timetable_service import TimetableService
from app.ui.i18n import t


service = TimetableService()
LANG = "ru"


def tr(key: str, **kwargs: object) -> str:
    return t(key, lang=LANG, **kwargs)


def required_label(label: str) -> str:
    return f"{label} *"


def normalized_text(value: Any) -> str:
    return str(value or "").strip()


def add_help(element, hint: str | None = None, *, tooltip: str | None = None) -> None:
    help_text = tooltip or hint
    if help_text:
        element.tooltip(help_text)
    if hint:
        ui.label(hint).classes("form-hint")


def register_ui(app: FastAPI) -> None:
    ui.run_with(app, storage_secret=settings.secret_key, title=tr("app.title"), favicon="calendar_month")


def inject_styles() -> None:
    ui.add_head_html(
        """
        <link rel="preconnect" href="https://fonts.googleapis.com">
        <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
        <link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;700&display=swap" rel="stylesheet">
        <style>
          :root {
            --canvas: #f5efde;
            --ink: #1f2933;
            --accent: #cc7a00;
            --panel: #fffaf0;
            --line: #d4c6a0;
            --muted: #6b7280;
            --online: #d8f2ff;
            --offline: #fff4dd;
          }
          body, .nicegui-content { background: linear-gradient(180deg, #f4eed9 0%, #efe7ce 100%); color: var(--ink); font-family: 'Space Grotesk', sans-serif; }
          .app-shell { max-width: 1450px; margin: 0 auto; width: 100%; padding: 1rem 1.25rem 3rem; }
          .hero-card { background: radial-gradient(circle at top right, #ffe8b8 0%, #fff8eb 55%, #f6edd7 100%); border: 1px solid var(--line); border-radius: 24px; }
          .panel-card { background: var(--panel); border: 1px solid var(--line); border-radius: 18px; }
          .grid-cell { min-height: 145px; }
          .entry-chip { width: 100%; justify-content: flex-start; text-align: left; white-space: pre-wrap; }
          .entry-online { background: var(--online); }
          .entry-offline { background: var(--offline); }
          .entry-hybrid { background: #efe5ff; }
          .form-hint { color: var(--muted); font-size: 0.8rem; line-height: 1.35; margin-top: -0.2rem; white-space: normal; }
          .q-field__label { white-space: normal; line-height: 1.15; max-width: calc(100% - 32px); }
          .q-field__bottom { padding-top: 0.25rem; }
        </style>
        """
    )


@contextmanager
def page_shell(title: str):
    inject_styles()
    with ui.left_drawer(value=True).classes("bg-[#2f4858] text-white"):
        ui.label(tr("app.title")).classes("text-lg font-bold mb-4")
        nav_items = [
            (tr("nav.dashboard"), "/"),
            (tr("nav.groups"), "/groups"),
            (tr("nav.teachers"), "/teachers"),
            (tr("nav.subjects"), "/subjects"),
            (tr("nav.calendar"), "/calendar"),
            (tr("nav.curriculum"), "/curriculum"),
            (tr("nav.generator"), "/generator"),
            (tr("nav.editor"), "/editor"),
            (tr("nav.conflicts"), "/conflicts"),
            (tr("nav.export"), "/export"),
            (tr("nav.settings"), "/settings"),
        ]
        for label, link in nav_items:
            ui.link(label, link).classes("block py-1 text-white")
    with ui.header().classes("bg-transparent"):
        ui.label(title).classes("text-2xl font-bold text-[#1f2933]")
    with ui.column().classes("app-shell"):
        yield


def session_scope() -> Session:
    return Session(engine)


def selection_options(rows: list, label_field: str = "code") -> dict[int, str]:
    return {row.id or 0: getattr(row, label_field) for row in rows}


def shift_options(include_all: bool = False) -> dict[str, str]:
    options = {shift: shift_label(shift, lang=LANG) for shift in SHIFT_VALUES}
    if include_all:
        return {"all": tr("common.all"), **options}
    return options


def delivery_options() -> dict[str, str]:
    return {mode: delivery_mode_label(mode, lang=LANG) for mode in DELIVERY_MODES}


def regular_delivery_options() -> dict[str, str]:
    return {
        "offline": delivery_mode_label("offline", lang=LANG),
        "hybrid": delivery_mode_label("hybrid", lang=LANG),
    }


def lesson_mode_options() -> dict[str, str]:
    return {mode: tr(f"lesson_mode.{mode}") for mode in LESSON_MODES}


def period_options() -> dict[str, str]:
    return {period_type: tr(f"period.{period_type}") for period_type in PERIOD_TYPES}


def bool_label(value: bool) -> str:
    return tr("common.yes") if value else tr("common.no")


def severity_label(value: str) -> str:
    return tr(f"severity.{value}")


def teacher_filter_options(rows: list[Teacher]) -> dict[int, str]:
    return {0: tr("common.all"), **selection_options(rows, "full_name")}


def load_online_slot_labels(session: Session) -> dict[int, str]:
    settings_map = {item.key: item.value for item in session.exec(select(AppSetting)).all()}
    return {
        slot: settings_map.get(f"online_slot_{slot}_label") or online_slot_label(slot, lang=LANG)
        for slot in online_slot_numbers()
    }


def entry_caption(entry: ScheduleEntry, subjects: dict[int, Subject], teachers: dict[int, Teacher], rooms: dict[int, Room]) -> str:
    teacher = teachers[entry.teacher_id].editable_name or teachers[entry.teacher_id].full_name
    room_text = rooms[entry.room_id].code if entry.room_id and entry.room_id in rooms else tr("editor.remove_room")
    return (
        f"{subjects[entry.subject_id].name}\n"
        f"{teacher}\n"
        f"{delivery_mode_label(entry.delivery_mode, lang=LANG)}\n"
        f"{room_text}"
    )


def online_entry_caption(
    entry: ScheduleEntry,
    subjects: dict[int, Subject],
    teachers: dict[int, Teacher],
    slot_labels: dict[int, str],
) -> str:
    teacher = teachers[entry.teacher_id].editable_name or teachers[entry.teacher_id].full_name
    return (
        f"{slot_labels.get(entry.online_slot_number or 1, online_slot_label(entry.online_slot_number or 1, lang=LANG))}\n"
        f"{subjects[entry.subject_id].name}\n"
        f"{teacher}\n"
        f"{tr('lesson_mode.online')}"
    )


def button_class_for_entry(entry: ScheduleEntry) -> str:
    suffix = "online" if entry.delivery_mode == "online" else "hybrid" if entry.delivery_mode == "hybrid" else "offline"
    return f"entry-chip entry-{suffix} text-left text-xs"


@ui.page("/")
def dashboard_page() -> None:
    with page_shell(tr("page.dashboard")):
        with session_scope() as session:
            groups = session.exec(select(Group)).all()
            teachers = session.exec(select(Teacher)).all()
            schedules = session.exec(select(Schedule)).all()
            conflicts = session.exec(select(Conflict)).all()
        with ui.card().classes("hero-card w-full p-6"):
            ui.label(tr("dashboard.hero_title")).classes("text-3xl font-bold")
            ui.label(tr("dashboard.hero_text")).classes("text-base text-[#374151]")
        with ui.row().classes("w-full gap-4 mt-4"):
            for label, value in [
                (tr("dashboard.groups"), len(groups)),
                (tr("dashboard.teachers"), len(teachers)),
                (tr("dashboard.schedules"), len(schedules)),
                (tr("dashboard.conflicts"), len(conflicts)),
            ]:
                with ui.card().classes("panel-card p-5 min-w-[180px]"):
                    ui.label(label).classes("text-sm text-[#6b7280]")
                    ui.label(str(value)).classes("text-3xl font-bold text-[#1f2933]")
        with ui.row().classes("gap-3 mt-3"):
            ui.button(tr("dashboard.open_generator"), on_click=lambda: ui.navigate.to("/generator")).props("color=amber-8")
            ui.button(tr("dashboard.open_editor"), on_click=lambda: ui.navigate.to("/editor")).props("outline color=dark")
            ui.button(tr("dashboard.open_conflicts"), on_click=lambda: ui.navigate.to("/conflicts")).props("outline color=dark")


@ui.page("/groups")
def groups_page() -> None:
    with page_shell(tr("page.groups")):
        with session_scope() as session:
            rows = session.exec(select(Group).order_by(Group.code)).all()
        ui.table(
            columns=[
                {"name": "code", "label": tr("common.code"), "field": "code"},
                {"name": "name", "label": tr("common.name"), "field": "name"},
                {"name": "course", "label": tr("common.course"), "field": "course"},
                {"name": "year", "label": tr("groups.year"), "field": "year"},
                {"name": "semester", "label": tr("common.semester"), "field": "semester"},
                {"name": "student_count", "label": tr("common.students"), "field": "student_count"},
                {"name": "shift", "label": tr("common.shift"), "field": "shift"},
            ],
            rows=[
                {
                    **row.model_dump(),
                    "shift": shift_label(row.shift, lang=LANG),
                }
                for row in rows
            ],
            row_key="id",
        ).classes("panel-card w-full")


@ui.page("/teachers")
def teachers_page() -> None:
    with page_shell(tr("page.teachers")):
        with session_scope() as session:
            departments = session.exec(select(Department).order_by(Department.code)).all()
        state = {"selected_teacher_id": None}

        @ui.refreshable
        def render_management() -> None:
            with session_scope() as session:
                rows = session.exec(select(Teacher).order_by(Teacher.full_name)).all()
            if rows and state["selected_teacher_id"] is None:
                state["selected_teacher_id"] = rows[0].id
            ui.table(
                columns=[
                    {"name": "full_name", "label": tr("teachers.full_name"), "field": "full_name"},
                    {"name": "short_name", "label": tr("teachers.short_name"), "field": "short_name"},
                    {"name": "editable_name", "label": tr("teachers.editable_name"), "field": "editable_name"},
                    {"name": "max_weekly_pairs", "label": tr("teachers.max_weekly_pairs"), "field": "max_weekly_pairs"},
                ],
                rows=[row.model_dump() for row in rows],
                row_key="id",
            ).classes("panel-card w-full")
            ui.select(
                selection_options(rows, "full_name"),
                value=state["selected_teacher_id"],
                label=tr("teachers.teacher_to_rename"),
                on_change=lambda event: state.update({"selected_teacher_id": event.value}),
            ).classes("w-full max-w-[34rem]").tooltip(tr("teachers.rename_teacher_hint"))

        render_management()
        with ui.row().classes("gap-3 mt-3"):
            with ui.dialog() as create_dialog, ui.card().classes("panel-card p-6 min-w-[640px] max-w-[92vw]"):
                ui.label(tr("teachers.create_demo_teacher")).classes("text-lg font-semibold")
                ui.label(tr("common.required_note")).classes("form-hint")
                full_name = ui.input(
                    required_label(tr("teachers.full_name")),
                    placeholder=tr("teachers.full_name_placeholder"),
                    validation=lambda value: None if normalized_text(value) else tr("teachers.validation_full_name"),
                ).classes("w-full")
                add_help(full_name, tr("teachers.full_name_hint"))
                short_name = ui.input(
                    required_label(tr("teachers.short_name")),
                    placeholder=tr("teachers.short_name_placeholder"),
                    validation=lambda value: None if normalized_text(value) else tr("teachers.validation_short_name"),
                ).classes("w-full")
                add_help(short_name, tr("teachers.short_name_hint"))
                department = ui.select(
                    selection_options(departments, "code"),
                    label=required_label(tr("teachers.department")),
                    validation=lambda value: None if value else tr("teachers.validation_department"),
                ).classes("w-full")
                add_help(department, tr("teachers.department_hint"))
                max_pairs = ui.number(
                    required_label(tr("teachers.max_weekly_pairs")),
                    placeholder=tr("teachers.max_weekly_pairs_placeholder"),
                    value=20,
                    min=1,
                    max=60,
                    step=1,
                    validation=lambda value: None if value is not None and 1 <= int(value) <= 60 else tr("teachers.validation_max_weekly_pairs"),
                ).classes("w-full")
                add_help(max_pairs, tr("teachers.max_weekly_pairs_hint"))

                def _submit_teacher_dialog() -> None:
                    is_valid = all(field.validate() for field in (full_name, short_name, department, max_pairs))
                    if not is_valid:
                        return
                    if _create_teacher(full_name.value, short_name.value, department.value, int(max_pairs.value or 20)):
                        create_dialog.close()

                with ui.row().classes("justify-end gap-2 mt-2"):
                    ui.button(tr("common.cancel"), on_click=create_dialog.close).props("flat color=dark")
                    ui.button(tr("common.save"), on_click=_submit_teacher_dialog).props("color=amber-8")
            ui.button(tr("common.create"), on_click=create_dialog.open).props("color=amber-8")
            rename_input = ui.input(
                tr("teachers.rename_teacher"),
                placeholder=tr("teachers.rename_teacher_placeholder"),
            ).classes("w-[28rem]")
            add_help(rename_input, tr("teachers.rename_teacher_hint"))
            ui.button(
                tr("common.rename"),
                on_click=lambda: _rename_selected_teacher(state["selected_teacher_id"], rename_input.value),
            ).props("outline color=dark")

        def _create_teacher(full_name: str, short_name: str, department_id: int | None, max_pairs: int) -> bool:
            with session_scope() as session:
                try:
                    service.create_teacher(session, normalized_text(full_name), normalized_text(short_name), department_id or 0, max_pairs)
                except ValueError as exc:
                    ui.notify(str(exc), color="negative")
                    return False
            ui.notify(tr("teachers.teacher_created"), color="positive")
            render_management.refresh()
            return True

        def _rename_selected_teacher(teacher_id: int | None, new_name: str) -> None:
            if not teacher_id or not normalized_text(new_name):
                ui.notify(tr("common.required_fields"), color="negative")
                return
            with session_scope() as session:
                try:
                    service.rename_teacher(session, teacher_id, normalized_text(new_name))
                except ValueError as exc:
                    ui.notify(str(exc), color="negative")
                    return
            ui.notify(tr("teachers.teacher_renamed"), color="positive")
            render_management.refresh()


@ui.page("/subjects")
def subjects_page() -> None:
    with page_shell(tr("page.subjects")):
        @ui.refreshable
        def render_subjects() -> None:
            with session_scope() as session:
                rows = session.exec(select(Subject).order_by(Subject.name)).all()
            ui.table(
                columns=[
                    {"name": "code", "label": tr("common.code"), "field": "code"},
                    {"name": "name", "label": tr("common.name"), "field": "name"},
                    {"name": "lesson_type", "label": tr("subjects.lesson_type"), "field": "lesson_type"},
                    {"name": "requires_special_room", "label": tr("subjects.special_room"), "field": "requires_special_room"},
                    {"name": "can_be_online", "label": tr("subjects.can_be_online"), "field": "can_be_online"},
                    {"name": "default_delivery_mode", "label": tr("subjects.default_delivery_mode"), "field": "default_delivery_mode"},
                ],
                rows=[
                    {
                        **row.model_dump(),
                        "requires_special_room": bool_label(row.requires_special_room),
                        "can_be_online": bool_label(row.can_be_online),
                        "default_delivery_mode": delivery_mode_label(row.default_delivery_mode, lang=LANG),
                    }
                    for row in rows
                ],
                row_key="id",
            ).classes("panel-card w-full")

        render_subjects()


@ui.page("/calendar")
def calendar_page() -> None:
    with page_shell(tr("page.calendar")):
        with session_scope() as session:
            groups = session.exec(select(Group).order_by(Group.code)).all()
        state = {"group_id": groups[0].id if groups else None, "semester": 3}

        @ui.refreshable
        def render_periods() -> None:
            with session_scope() as session:
                periods = session.exec(
                    select(AcademicPeriod).where(
                        AcademicPeriod.group_id == state["group_id"],
                        AcademicPeriod.semester == state["semester"],
                    ).order_by(AcademicPeriod.week_number)
                ).all()
            for period in periods:
                with ui.row().classes("panel-card w-full items-center p-3 gap-3"):
                    ui.label(f"{tr('common.week')} {period.week_number}").classes("w-24 font-semibold")
                    period_select = ui.select(period_options(), value=period.period_type, label=tr("calendar.period_type")).classes("w-80")
                    schedulable = ui.switch(tr("calendar.schedulable"), value=period.is_schedulable)
                    ui.button(
                        tr("common.save"),
                        on_click=lambda p=period, ps=period_select, sc=schedulable: _save_period(p.id or 0, ps.value, bool(sc.value)),
                    ).props("outline color=dark")

        with ui.row().classes("gap-3"):
            ui.select(
                selection_options(groups),
                value=state["group_id"],
                label=tr("common.group"),
                on_change=lambda event: (state.update({"group_id": event.value}), render_periods.refresh()),
            )
            ui.select(
                {3: f"{tr('common.semester')} 3", 4: f"{tr('common.semester')} 4"},
                value=state["semester"],
                label=tr("common.semester"),
                on_change=lambda event: (state.update({"semester": event.value}), render_periods.refresh()),
            )
        render_periods()

        def _save_period(period_id: int, period_type: str, schedulable: bool) -> None:
            with session_scope() as session:
                period = session.get(AcademicPeriod, period_id)
                period.period_type = period_type
                period.is_schedulable = schedulable
                session.add(period)
                session.commit()
            ui.notify(tr("calendar.period_updated"), color="positive")
            render_periods.refresh()


@ui.page("/curriculum")
def curriculum_page() -> None:
    with page_shell(tr("page.curriculum")):
        with session_scope() as session:
            groups = session.exec(select(Group).order_by(Group.code)).all()
        state = {"group_id": groups[0].id if groups else None, "semester": 3}

        @ui.refreshable
        def render_loads() -> None:
            with session_scope() as session:
                loads = session.exec(
                    select(CurriculumLoad).where(
                        CurriculumLoad.group_id == state["group_id"],
                        CurriculumLoad.semester == state["semester"],
                    )
                ).all()
                subjects = {subject.id: subject.name for subject in session.exec(select(Subject)).all()}
            rows = [
                {
                    "subject": subjects[load.subject_id],
                    "total_hours": load.total_hours,
                    "raw_total_hours": load.raw_total_hours,
                    "practice_hours": load.practice_hours,
                    "study_weeks": load.study_weeks,
                    "hours_per_week": load.hours_per_week,
                    "pairs_per_week": load.pairs_per_week,
                    "lesson_type": load.lesson_type,
                    "delivery_mode": delivery_mode_label(load.delivery_mode, lang=LANG),
                }
                for load in loads
            ]
            ui.table(
                columns=[
                    {"name": "subject", "label": tr("common.subject"), "field": "subject"},
                    {"name": "total_hours", "label": tr("curriculum.schedulable_hours"), "field": "total_hours"},
                    {"name": "raw_total_hours", "label": tr("curriculum.raw_hours"), "field": "raw_total_hours"},
                    {"name": "practice_hours", "label": tr("curriculum.practice_hours"), "field": "practice_hours"},
                    {"name": "study_weeks", "label": tr("curriculum.study_weeks"), "field": "study_weeks"},
                    {"name": "hours_per_week", "label": tr("curriculum.hours_per_week"), "field": "hours_per_week"},
                    {"name": "pairs_per_week", "label": tr("curriculum.pairs_per_week"), "field": "pairs_per_week"},
                    {"name": "lesson_type", "label": tr("subjects.lesson_type"), "field": "lesson_type"},
                    {"name": "delivery_mode", "label": tr("common.delivery_mode"), "field": "delivery_mode"},
                ],
                rows=rows,
                row_key="subject",
            ).classes("panel-card w-full")

        with ui.row().classes("gap-3"):
            ui.select(
                selection_options(groups),
                value=state["group_id"],
                label=tr("common.group"),
                on_change=lambda event: (state.update({"group_id": event.value}), render_loads.refresh()),
            )
            ui.select(
                {3: f"{tr('common.semester')} 3", 4: f"{tr('common.semester')} 4"},
                value=state["semester"],
                label=tr("common.semester"),
                on_change=lambda event: (state.update({"semester": event.value}), render_loads.refresh()),
            )
        render_loads()


@ui.page("/generator")
def generator_page() -> None:
    with page_shell(tr("page.generator")):
        with session_scope() as session:
            groups = session.exec(select(Group).order_by(Group.code)).all()
            schedules = session.exec(select(Schedule).order_by(Schedule.created_at.desc())).all()
        semester = ui.select({3: f"{tr('common.semester')} 3", 4: f"{tr('common.semester')} 4"}, value=3, label=tr("common.semester")).classes("w-56")
        selected_groups = ui.select(selection_options(groups), multiple=True, label=tr("nav.groups")).classes("w-full")
        schedule_name = ui.input(tr("generator.schedule_name"), value=f"{tr('common.schedule')} {tr('common.semester')} 3").classes("w-full")
        ui.button(
            tr("generator.generate"),
            on_click=lambda: _generate_schedule(int(semester.value), list(selected_groups.value or []), schedule_name.value),
        ).props("color=amber-8")
        ui.separator()
        ui.label(tr("generator.existing")).classes("text-lg font-semibold")
        ui.table(
            columns=[
                {"name": "name", "label": tr("common.name"), "field": "name"},
                {"name": "semester", "label": tr("common.semester"), "field": "semester"},
                {"name": "created_at", "label": tr("table.created_at"), "field": "created_at"},
            ],
            rows=[schedule.model_dump(mode="json") for schedule in schedules],
            row_key="id",
        ).classes("panel-card w-full")

        def _generate_schedule(selected_semester: int, group_ids: list[int], name: str) -> None:
            with session_scope() as session:
                target_codes = [session.get(Group, group_id).code for group_id in group_ids] if group_ids else None
                schedule = service.generate_schedule(
                    session,
                    semester=selected_semester,
                    group_codes=target_codes,
                    name=name or None,
                )
                conflicts = session.exec(select(Conflict).where(Conflict.schedule_id == schedule.id)).all()
            if any(conflict.severity == "hard" for conflict in conflicts):
                ui.notify(tr("generator.created_with_conflicts", name=schedule.name), color="warning", multi_line=True, timeout=7000)
            else:
                ui.notify(tr("generator.created", name=schedule.name), color="positive")
            ui.navigate.to("/editor")


@ui.page("/editor")
def editor_page() -> None:
    with page_shell(tr("page.editor")):
        with session_scope() as session:
            schedules = session.exec(select(Schedule).order_by(Schedule.created_at.desc())).all()
            groups = session.exec(select(Group).order_by(Group.code)).all()
            teachers = session.exec(select(Teacher).order_by(Teacher.full_name)).all()
            rooms = session.exec(select(Room).order_by(Room.code)).all()
        group_map = {group.id: group for group in groups}
        state = {
            "schedule_id": schedules[0].id if schedules else None,
            "view_mode": "group",
            "group_id": groups[0].id if groups else None,
            "teacher_id": 0,
            "shift_filter": "all",
        }

        @ui.refreshable
        def render_grid() -> None:
            if not state["schedule_id"]:
                ui.label(tr("editor.no_schedule"))
                return
            with session_scope() as session:
                entries = session.exec(select(ScheduleEntry).where(ScheduleEntry.schedule_id == state["schedule_id"])).all()
                subjects = {subject.id: subject for subject in session.exec(select(Subject)).all()}
                teacher_map = {teacher.id: teacher for teacher in session.exec(select(Teacher)).all()}
                room_map = {room.id: room for room in session.exec(select(Room)).all()}
                slot_labels = load_online_slot_labels(session)
            filtered = entries
            selected_group = group_map.get(state["group_id"])
            if state["view_mode"] == "group" and state["group_id"]:
                filtered = [entry for entry in filtered if entry.group_id == state["group_id"]]
            if state["view_mode"] == "teacher" and state["teacher_id"] not in (None, 0):
                filtered = [entry for entry in filtered if entry.teacher_id == state["teacher_id"]]
            regular_entries = [entry for entry in filtered if entry.lesson_mode != LESSON_MODE_ONLINE]
            online_entries = [entry for entry in filtered if entry.lesson_mode == LESSON_MODE_ONLINE]
            if state["view_mode"] != "group" and state["shift_filter"] != "all":
                regular_entries = [entry for entry in regular_entries if entry.shift == state["shift_filter"]]
            visible_pairs = visible_pairs_for_view(
                state["view_mode"],
                selected_group.shift if selected_group else None,
                state["shift_filter"],
            )

            with ui.column().classes("w-full gap-2 mt-3"):
                ui.label(tr("editor.main_schedule")).classes("text-lg font-semibold")
                if state["view_mode"] == "group" and selected_group:
                    ui.label(
                        tr("editor.selected_group_shift", shift=shift_label(selected_group.shift, lang=LANG))
                    ).classes("text-sm text-[#6b7280]")
                with ui.grid(columns=1 + len(visible_pairs)).classes("w-full gap-2"):
                    ui.card().classes("panel-card p-3").tight()
                    for pair_number in visible_pairs:
                        with ui.card().classes("panel-card p-3"):
                            ui.label(pair_label(pair_number, lang=LANG)).classes("font-bold")
                            ui.label(pair_time_range(pair_number, lang=LANG)).classes("text-xs")
                            ui.label(shift_label("morning" if pair_number <= 3 else "afternoon", lang=LANG)).classes("text-xs text-[#6b7280]")
                    for day_of_week in DAYS:
                        with ui.card().classes("panel-card p-3"):
                            ui.label(day_label(day_of_week, lang=LANG)).classes("font-bold")
                        for pair_number in visible_pairs:
                            with ui.card().classes("panel-card grid-cell p-2"):
                                slot_entries = [
                                    entry for entry in regular_entries if entry.day_of_week == day_of_week and entry.pair_number == pair_number
                                ]
                                if not slot_entries:
                                    ui.label(tr("common.free")).classes("text-xs text-[#6b7280]")
                                for entry in slot_entries:
                                    ui.button(
                                        entry_caption(entry, subjects, teacher_map, room_map),
                                        on_click=lambda e=entry: open_edit_dialog(e.id or 0),
                                    ).props("flat").classes(button_class_for_entry(entry))
                ui.label(tr("editor.online_schedule")).classes("text-lg font-semibold mt-4")
                if not online_entries:
                    ui.label(tr("editor.no_online_lessons")).classes("text-sm text-[#6b7280]")
                else:
                    with ui.row().classes("w-full gap-3"):
                        for day_of_week in ONLINE_ALLOWED_DAYS:
                            with ui.card().classes("panel-card p-4 grow"):
                                ui.label(day_label(day_of_week, lang=LANG)).classes("font-bold")
                                day_entries = [
                                    entry
                                    for entry in online_entries
                                    if entry.day_of_week == day_of_week
                                ]
                                if not day_entries:
                                    ui.label(tr("common.free")).classes("text-xs text-[#6b7280]")
                                for entry in sorted(day_entries, key=lambda item: item.online_slot_number or 0):
                                    ui.button(
                                        online_entry_caption(entry, subjects, teacher_map, slot_labels),
                                        on_click=lambda e=entry: open_edit_dialog(e.id or 0),
                                    ).props("flat").classes("entry-chip entry-online text-left text-xs")

        @ui.refreshable
        def render_feedback() -> None:
            if not state["schedule_id"]:
                return
            with session_scope() as session:
                conflicts = session.exec(select(Conflict).where(Conflict.schedule_id == state["schedule_id"])).all()
                suggestions = session.exec(
                    select(Suggestion).where(
                        Suggestion.conflict_id.in_([item.id for item in conflicts if item.id is not None])
                    )
                ).all()
            ui.label(tr("editor.after_edit_conflicts")).classes("text-lg font-semibold mt-4")
            if not conflicts:
                ui.label(tr("editor.no_conflicts"))
                return
            for conflict in conflicts:
                with ui.expansion(f"[{severity_label(conflict.severity)}] {conflict.message}", value=False).classes("panel-card w-full"):
                    for suggestion in sorted(
                        [item for item in suggestions if item.conflict_id == conflict.id],
                        key=lambda item: item.rank,
                    ):
                        ui.label(f"{suggestion.rank}. {suggestion.message}")
                    ui.button(
                        tr("conflicts.explain"),
                        on_click=lambda c=conflict.id: _show_conflict_explanation(c or 0),
                    ).props("outline color=dark").classes("mt-2").tooltip(tr("conflicts.explain_hint"))

        def _show_conflict_explanation(conflict_id: int) -> None:
            with session_scope() as session:
                message = service.explain_conflict(session, conflict_id)
            ui.notify(message, multi_line=True, timeout=10000)

        def open_edit_dialog(entry_id: int) -> None:
            with session_scope() as session:
                entry = session.get(ScheduleEntry, entry_id)
                group = session.get(Group, entry.group_id)
                teachers_local = session.exec(select(Teacher).order_by(Teacher.full_name)).all()
                rooms_local = session.exec(select(Room).order_by(Room.code)).all()
                slot_labels = load_online_slot_labels(session)
                schedule = session.get(Schedule, entry.schedule_id)
                loads = session.exec(
                    select(CurriculumLoad).where(
                        CurriculumLoad.group_id == entry.group_id,
                        CurriculumLoad.semester == schedule.semester,
                    )
                ).all()
                subjects_local = [session.get(Subject, load.subject_id) for load in loads]
            subject_options = {subject.id or 0: subject.name for subject in subjects_local if subject is not None}
            room_options = {0: tr("editor.remove_room"), **selection_options(rooms_local, "code")}
            regular_pair_options = {
                number: f"{pair_label(number, lang=LANG)} | {pair_time_range(number, lang=LANG)}"
                for number in allowed_pairs_for_shift(group.shift if group else entry.shift)
            }
            slot_options = {
                slot: f"{slot_labels[slot]} | {day_label(online_slot_day(slot), lang=LANG)}"
                for slot in online_slot_numbers()
            }

            def _required_value(value: Any, message: str) -> str | None:
                return None if value not in (None, "", 0) else message

            with ui.dialog() as dialog, ui.card().classes("panel-card p-6 min-w-[760px] max-w-[95vw]"):
                ui.label(tr("editor.edit_entry")).classes("text-lg font-semibold")
                ui.label(tr("common.required_note")).classes("form-hint")
                subject_id = ui.select(
                    subject_options,
                    value=entry.subject_id,
                    label=required_label(tr("editor.change_subject")),
                    validation=lambda value: _required_value(value, tr("editor.validation_subject")),
                ).classes("w-full")
                add_help(subject_id, tr("editor.subject_hint"))
                lesson_mode = ui.select(
                    lesson_mode_options(),
                    value=entry.lesson_mode,
                    label=required_label(tr("editor.change_lesson_mode")),
                    validation=lambda value: _required_value(value, tr("editor.validation_lesson_mode")),
                ).classes("w-full")
                add_help(lesson_mode, tr("editor.lesson_mode_hint"))
                day_of_week = ui.select(
                    {day: day_label(day, lang=LANG) for day in DAYS},
                    value=entry.day_of_week,
                    label=required_label(tr("editor.change_day")),
                    validation=lambda value: _required_value(value, tr("editor.validation_day")),
                ).classes("w-full")
                add_help(day_of_week, tr("editor.day_hint"))
                pair_number = ui.select(
                    regular_pair_options,
                    value=entry.pair_number,
                    label=tr("editor.change_pair"),
                    validation=lambda value: None if lesson_mode.value == LESSON_MODE_ONLINE or value else tr("editor.validation_pair"),
                ).classes("w-full")
                pair_number.tooltip(tr("editor.pair_hint"))
                pair_hint = ui.label(tr("editor.pair_hint")).classes("form-hint")
                online_slot_number = ui.select(
                    slot_options,
                    value=entry.online_slot_number or 1,
                    label=tr("editor.change_online_slot"),
                    validation=lambda value: None if lesson_mode.value != LESSON_MODE_ONLINE or value else tr("editor.validation_online_slot"),
                ).classes("w-full")
                online_slot_number.tooltip(tr("editor.online_slot_hint"))
                online_slot_hint = ui.label(tr("editor.online_slot_hint")).classes("form-hint")
                teacher_id = ui.select(
                    selection_options(teachers_local, "full_name"),
                    value=entry.teacher_id,
                    label=required_label(tr("editor.change_teacher")),
                    validation=lambda value: _required_value(value, tr("editor.validation_teacher")),
                ).classes("w-full")
                add_help(teacher_id, tr("editor.teacher_hint"))
                room_id = ui.select(
                    room_options,
                    value=entry.room_id or 0,
                    label=tr("editor.change_room"),
                    validation=lambda value: None if lesson_mode.value == LESSON_MODE_ONLINE or delivery_mode.value == "online" or value not in (None, 0) else tr("editor.validation_room"),
                ).classes("w-full")
                room_id.tooltip(tr("editor.room_hint"))
                room_hint = ui.label(tr("editor.room_hint")).classes("form-hint")
                delivery_mode = ui.select(
                    delivery_options(),
                    value=entry.delivery_mode,
                    label=required_label(tr("editor.change_mode")),
                ).classes("w-full")
                add_help(delivery_mode, tr("editor.mode_hint"))
                locked = ui.switch(tr("editor.lock"), value=entry.locked)
                add_help(locked, tr("editor.lock_hint"))
                rename_teacher = ui.input(
                    tr("editor.rename_current_teacher"),
                    placeholder=tr("editor.rename_current_teacher_placeholder"),
                ).classes("w-full")
                add_help(rename_teacher, tr("editor.rename_current_teacher_hint"))
                reassign_teacher = ui.select(
                    selection_options(teachers_local, "full_name"),
                    label=tr("editor.reassign_teacher"),
                ).classes("w-full")
                add_help(reassign_teacher, tr("editor.reassign_teacher_hint"))

                sync_state = {"active": False}

                def _sync_edit_form() -> None:
                    if sync_state["active"]:
                        return
                    sync_state["active"] = True
                    try:
                        is_online = lesson_mode.value == LESSON_MODE_ONLINE or delivery_mode.value == "online"
                        if is_online:
                            if lesson_mode.value != LESSON_MODE_ONLINE:
                                lesson_mode.set_value(LESSON_MODE_ONLINE)
                            if delivery_mode.value != "online":
                                delivery_mode.set_value("online")
                            if not online_slot_number.value:
                                online_slot_number.set_value(1)
                            day_of_week.set_value(online_slot_day(int(online_slot_number.value or 1)))
                            pair_number.set_visibility(False)
                            pair_hint.set_visibility(False)
                            pair_number.disable()
                            online_slot_number.set_visibility(True)
                            online_slot_hint.set_visibility(True)
                            online_slot_number.enable()
                            day_of_week.disable()
                            room_id.set_value(0)
                            room_id.disable()
                        else:
                            if lesson_mode.value != LESSON_MODE_REGULAR:
                                lesson_mode.set_value(LESSON_MODE_REGULAR)
                            if delivery_mode.value == "online":
                                delivery_mode.set_value("offline")
                            if pair_number.value not in regular_pair_options:
                                pair_number.set_value(next(iter(regular_pair_options)))
                            pair_number.set_visibility(True)
                            pair_hint.set_visibility(True)
                            pair_number.enable()
                            online_slot_number.set_visibility(False)
                            online_slot_hint.set_visibility(False)
                            online_slot_number.disable()
                            day_of_week.enable()
                            room_id.enable()
                    finally:
                        sync_state["active"] = False
                    for field in (subject_id, lesson_mode, day_of_week, pair_number, online_slot_number, teacher_id, room_id):
                        field.validate()

                lesson_mode.on_value_change(lambda _: _sync_edit_form())
                delivery_mode.on_value_change(lambda _: _sync_edit_form())
                online_slot_number.on_value_change(lambda _: _sync_edit_form())
                _sync_edit_form()

                with ui.row().classes("justify-end gap-2 mt-2"):
                    ui.button(tr("common.cancel"), on_click=dialog.close).props("flat color=dark")
                    ui.button(
                        tr("common.save"),
                        on_click=lambda: _save_entry(
                            entry_id,
                            _entry_payload(
                                subject_id=subject_id.value,
                                lesson_mode=lesson_mode.value,
                                day_of_week=day_of_week.value,
                                pair_number=pair_number.value,
                                online_slot_number=online_slot_number.value,
                                teacher_id=teacher_id.value,
                                room_id=room_id.value,
                                delivery_mode=delivery_mode.value,
                                locked=bool(locked.value),
                                rename_teacher_to=normalized_text(rename_teacher.value) or None,
                                reassign_teacher_id=reassign_teacher.value or None,
                            ),
                            dialog,
                            validators=[subject_id, lesson_mode, day_of_week, pair_number, online_slot_number, teacher_id, room_id],
                        ),
                    ).props("color=amber-8")
            dialog.open()

        def _entry_payload(
            subject_id: int | None,
            lesson_mode: str,
            day_of_week: int | None,
            pair_number: int | None,
            online_slot_number: int | None,
            teacher_id: int | None,
            room_id: int | None,
            delivery_mode: str,
            locked: bool,
            rename_teacher_to: str | None,
            reassign_teacher_id: int | None,
        ) -> dict:
            if lesson_mode == LESSON_MODE_ONLINE:
                slot_number = int(online_slot_number or 1)
                return {
                    "subject_id": subject_id,
                    "lesson_mode": LESSON_MODE_ONLINE,
                    "day_of_week": online_slot_day(slot_number),
                    "pair_number": 0,
                    "online_slot_number": slot_number,
                    "teacher_id": teacher_id,
                    "room_id": None,
                    "delivery_mode": "online",
                    "locked": locked,
                    "rename_teacher_to": rename_teacher_to,
                    "reassign_teacher_id": reassign_teacher_id,
                }
            return {
                "subject_id": subject_id,
                "lesson_mode": LESSON_MODE_REGULAR,
                "day_of_week": day_of_week,
                "pair_number": pair_number,
                "online_slot_number": None,
                "teacher_id": teacher_id,
                "room_id": None if room_id in (None, 0) else room_id,
                "delivery_mode": delivery_mode,
                "locked": locked,
                "rename_teacher_to": rename_teacher_to,
                "reassign_teacher_id": reassign_teacher_id,
            }

        def _save_entry(entry_id: int, payload: dict, dialog, validators: list) -> None:
            if not all(field.validate() for field in validators):
                return
            try:
                with session_scope() as session:
                    updated = service.update_entry(session, entry_id, payload)
                    manual_edit_message = service.explain_last_manual_edit(session, updated.schedule_id)
            except ValueError as exc:
                ui.notify(tr("editor.save_failed", message=str(exc)), color="negative", multi_line=True)
                return
            dialog.close()
            ui.notify(tr("editor.entry_updated"), color="positive")
            ui.notify(manual_edit_message, color="info", multi_line=True, timeout=9000)
            render_grid.refresh()
            render_feedback.refresh()

        def _show_schedule_summary() -> None:
            if not state["schedule_id"]:
                ui.notify(tr("notify.select_schedule"), color="negative")
                return
            with session_scope() as session:
                message = service.summarize_schedule(session, int(state["schedule_id"]))
            ui.notify(message, multi_line=True, timeout=10000)

        with ui.row().classes("gap-3 w-full"):
            ui.select(
                selection_options(schedules, "name"),
                value=state["schedule_id"],
                label=tr("common.schedule"),
                on_change=lambda event: (
                    state.update({"schedule_id": event.value}),
                    render_grid.refresh(),
                    render_feedback.refresh(),
                ),
            )
            ui.select(
                {"group": tr("editor.group_view"), "teacher": tr("editor.teacher_view")},
                value=state["view_mode"],
                label=tr("editor.view_mode"),
                on_change=lambda event: (state.update({"view_mode": event.value}), render_grid.refresh()),
            )
            ui.select(
                selection_options(groups),
                value=state["group_id"],
                label=tr("common.group"),
                on_change=lambda event: (state.update({"group_id": event.value}), render_grid.refresh()),
            )
            ui.select(
                teacher_filter_options(teachers),
                value=state["teacher_id"],
                label=tr("editor.teacher_filter"),
                on_change=lambda event: (state.update({"teacher_id": event.value}), render_grid.refresh()),
            )
            ui.select(
                shift_options(include_all=True),
                value=state["shift_filter"],
                label=tr("editor.shift_filter"),
                on_change=lambda event: (state.update({"shift_filter": event.value}), render_grid.refresh()),
            )
        ui.button(tr("editor.summary_button"), on_click=_show_schedule_summary).props("outline color=dark").classes("mt-3").tooltip(tr("editor.summary_hint"))

        render_grid()
        render_feedback()


@ui.page("/conflicts")
def conflicts_page() -> None:
    with page_shell(tr("page.conflicts")):
        with session_scope() as session:
            schedules = session.exec(select(Schedule).order_by(Schedule.created_at.desc())).all()
        state = {"schedule_id": schedules[0].id if schedules else None}

        @ui.refreshable
        def render_conflicts() -> None:
            if not state["schedule_id"]:
                return
            with session_scope() as session:
                conflicts = session.exec(select(Conflict).where(Conflict.schedule_id == state["schedule_id"])).all()
                suggestions = session.exec(
                    select(Suggestion).where(
                        Suggestion.conflict_id.in_([item.id for item in conflicts if item.id is not None])
                    )
                ).all()
            if not conflicts:
                ui.label(tr("conflicts.none"))
                return
            for conflict in conflicts:
                with ui.card().classes("panel-card w-full p-4"):
                    ui.label(f"[{severity_label(conflict.severity)}] {conflict.message}").classes("font-semibold")
                    for suggestion in sorted(
                        [item for item in suggestions if item.conflict_id == conflict.id],
                        key=lambda item: item.rank,
                    ):
                        ui.label(f"{suggestion.rank}. {suggestion.message}")
                    ui.button(
                        tr("conflicts.explain"),
                        on_click=lambda c=conflict.id: _show_explanation(c or 0),
                    ).props("outline color=dark").classes("mt-2").tooltip(tr("conflicts.explain_hint"))

        def _show_explanation(conflict_id: int) -> None:
            with session_scope() as session:
                message = service.explain_conflict(session, conflict_id)
            ui.notify(message, multi_line=True, timeout=8000)

        ui.select(
            selection_options(schedules, "name"),
            value=state["schedule_id"],
            label=tr("common.schedule"),
            on_change=lambda event: (state.update({"schedule_id": event.value}), render_conflicts.refresh()),
        )
        render_conflicts()


@ui.page("/export")
def export_page() -> None:
    with page_shell(tr("page.export")):
        with session_scope() as session:
            schedules = session.exec(select(Schedule).order_by(Schedule.created_at.desc())).all()
        schedule_id = ui.select(selection_options(schedules, "name"), value=schedules[0].id if schedules else None, label=tr("common.schedule"))
        links = ui.column()

        def _export(fmt: str) -> None:
            if not schedule_id.value:
                ui.notify(tr("notify.select_schedule"), color="negative")
                return
            with session_scope() as session:
                path = service.export_schedule(session, int(schedule_id.value), fmt)
            links.clear()
            with links:
                ui.link(tr("export.open", name=path.name), f"/exports/{path.name}", new_tab=True)
            ui.notify(tr("export.done", name=path.name), color="positive")

        with ui.row().classes("gap-3 mt-3"):
            ui.button(tr("export.xlsx"), on_click=lambda: _export("xlsx")).props("color=amber-8")
            ui.button(tr("export.pdf"), on_click=lambda: _export("pdf")).props("outline color=dark")
            ui.button(tr("export.docx"), on_click=lambda: _export("docx")).props("outline color=dark")


@ui.page("/settings")
def settings_page() -> None:
    with page_shell(tr("page.settings")):
        with session_scope() as session:
            current_settings = {item.key: item.value for item in session.exec(select(AppSetting)).all()}
            groups = session.exec(select(Group).order_by(Group.code)).all()
            subjects = session.exec(select(Subject).order_by(Subject.name)).all()
            course_policies = {
                policy.course: policy
                for policy in session.exec(
                    select(OnlinePolicy).where(
                        OnlinePolicy.group_id.is_(None),
                        OnlinePolicy.subject_id.is_(None),
                    )
                ).all()
                if policy.course is not None
            }
            group_policies = {
                policy.group_id: policy
                for policy in session.exec(
                    select(OnlinePolicy).where(
                        OnlinePolicy.group_id.is_not(None),
                        OnlinePolicy.subject_id.is_(None),
                    )
                ).all()
            }

        ui.label(tr("settings.ai_section")).classes("text-xl font-semibold")
        with ui.card().classes("panel-card p-4 w-full"):
            ai_enabled = ui.switch(
                tr("settings.ai_enabled"),
                value=current_settings.get("ai_enabled", "false").lower() == "true",
            )
            add_help(ai_enabled, tr("settings.ai_enabled_hint"))
            gemini_key = ui.input(
                tr("settings.gemini_key"),
                placeholder=tr("settings.gemini_key_placeholder"),
                password=True,
                password_toggle_button=True,
                value=current_settings.get("gemini_api_key", ""),
            ).classes("w-full")
            add_help(gemini_key, tr("settings.gemini_key_hint"))
            gemini_model = ui.input(
                tr("settings.gemini_model"),
                placeholder=tr("settings.gemini_model_placeholder"),
                value=current_settings.get("gemini_model", settings.gemini_model),
            ).classes("w-full")
            add_help(gemini_model, tr("settings.gemini_model_hint"))
            gemini_timeout = ui.number(
                tr("settings.gemini_timeout"),
                value=int(current_settings.get("gemini_timeout", settings.gemini_timeout)),
                min=5,
                max=120,
                step=1,
                validation=lambda value: None if value is not None and 5 <= int(value) <= 120 else tr("common.required_fields"),
            ).classes("w-full max-w-[18rem]")
            add_help(gemini_timeout, tr("settings.gemini_timeout_hint"))
            ui.label(tr("settings.ai_note")).classes("text-sm text-[#6b7280]")

            def _sync_ai_inputs() -> None:
                is_enabled = bool(ai_enabled.value)
                gemini_key.set_enabled(is_enabled)
                gemini_model.set_enabled(is_enabled)
                gemini_timeout.set_enabled(is_enabled)

            ai_enabled.on_value_change(lambda _: _sync_ai_inputs())
            _sync_ai_inputs()

            def _save_ai_settings() -> None:
                with session_scope() as session:
                    service.save_ai_settings(
                        session,
                        enabled=bool(ai_enabled.value),
                        api_key=gemini_key.value or "",
                        model=gemini_model.value or settings.gemini_model,
                        timeout=int(gemini_timeout.value or settings.gemini_timeout),
                    )
                ui.notify(tr("settings.saved"), color="positive")
                if not ai_enabled.value:
                    ui.notify(tr("settings.ai_disabled"), color="warning")
                elif not normalized_text(gemini_key.value):
                    ui.notify(tr("settings.standard_mode"), color="warning")

            def _test_ai_connection() -> None:
                with session_scope() as session:
                    result = service.test_ai_connection(
                        session,
                        {
                            "ai_enabled": bool(ai_enabled.value),
                            "ai_provider": "gemini",
                            "gemini_api_key": gemini_key.value or "",
                            "gemini_model": gemini_model.value or settings.gemini_model,
                            "gemini_timeout": int(gemini_timeout.value or settings.gemini_timeout),
                        },
                    )
                ui.notify(result.message, color="positive" if result.success else "negative")
                if not result.success and result.message != tr("settings.standard_mode"):
                    ui.notify(tr("settings.standard_mode"), color="warning")

            with ui.row().classes("gap-3 mt-2"):
                ui.button(tr("settings.save_settings"), on_click=_save_ai_settings).props("color=amber-8")
                ui.button(tr("settings.test_connection"), on_click=_test_ai_connection).props("outline color=dark")

        ui.label(tr("settings.online_policy")).classes("text-xl font-semibold mt-6")

        with ui.card().classes("panel-card p-4 w-full mt-4"):
            ui.label(tr("settings.course_target")).classes("text-lg font-semibold")
            ui.label(tr("settings.course_1_note")).classes("text-sm text-[#6b7280]")
            course_inputs = {
                course: ui.number(
                    f"{course} {tr('common.course')}",
                    value=(course_policies.get(course).target_online_lessons_per_week if course in course_policies else 0),
                    min=0,
                    max=6,
                ).classes("w-60")
                for course in (1, 2, 3, 4)
            }

            def _save_course_targets() -> None:
                with session_scope() as session:
                    for course, widget in course_inputs.items():
                        note = tr("settings.course_1_note") if course == 1 else ""
                        service.upsert_course_online_target(session, course, int(widget.value or 0), note=note)
                ui.notify(tr("settings.saved"), color="positive")

            ui.button(tr("common.save"), on_click=_save_course_targets).props("color=amber-8")

        with ui.card().classes("panel-card p-4 w-full mt-4"):
            ui.label(tr("settings.group_override")).classes("text-lg font-semibold")
            selected_group = ui.select(selection_options(groups), label=tr("common.group")).classes("w-72")
            override_target = ui.number(tr("settings.course_target"), value=3, min=0, max=6).classes("w-60")

            def _save_group_override() -> None:
                if not selected_group.value:
                    ui.notify(tr("common.required_fields"), color="negative")
                    return
                with session_scope() as session:
                    service.upsert_group_online_target(session, int(selected_group.value), int(override_target.value or 0))
                ui.notify(tr("settings.group_override_saved"), color="positive")

            ui.button(tr("common.save"), on_click=_save_group_override).props("color=amber-8")
            if group_policies:
                ui.separator()
                for group_id, policy in group_policies.items():
                    group = next((item for item in groups if item.id == group_id), None)
                    if group is None:
                        continue
                    ui.label(f"{group.code}: {policy.target_online_lessons_per_week}")

        with ui.card().classes("panel-card p-4 w-full mt-4"):
            ui.label(tr("settings.online_slots")).classes("text-lg font-semibold")
            slot_inputs = {
                slot: ui.input(
                    f"{tr('online_slot.label', slot=slot)}",
                    value=current_settings.get(f"online_slot_{slot}_label", tr("online_slot.label", slot=slot)),
                ).classes("w-full")
                for slot in online_slot_numbers()
            }

            def _save_online_slots() -> None:
                with session_scope() as session:
                    for slot, widget in slot_inputs.items():
                        item = session.exec(select(AppSetting).where(AppSetting.key == f"online_slot_{slot}_label")).first()
                        if item is None:
                            item = AppSetting(key=f"online_slot_{slot}_label", value=widget.value)
                        else:
                            item.value = widget.value
                        session.add(item)
                    session.commit()
                ui.notify(tr("settings.saved"), color="positive")

            ui.button(tr("common.save"), on_click=_save_online_slots).props("color=amber-8")

        @ui.refreshable
        def render_subject_policy() -> None:
            with session_scope() as session:
                rows = session.exec(select(Subject).order_by(Subject.name)).all()
            ui.label(tr("settings.subject_policy")).classes("text-lg font-semibold mt-4")
            for subject in rows:
                with ui.row().classes("panel-card w-full items-center p-3 gap-3"):
                    ui.label(subject.name).classes("grow")
                    switch = ui.switch(tr("subjects.can_be_online"), value=subject.can_be_online)
                    ui.button(
                        tr("common.save"),
                        on_click=lambda s=subject, sw=switch: _save_subject_online(s.id or 0, bool(sw.value)),
                    ).props("outline color=dark")

        def _save_subject_online(subject_id: int, allow_online: bool) -> None:
            with session_scope() as session:
                subject = session.get(Subject, subject_id)
                subject.can_be_online = allow_online
                session.add(subject)
                session.commit()
                service.upsert_subject_online_policy(session, subject_id, allow_online, note="Обновлено через UI.")
            ui.notify(tr("settings.saved"), color="positive")
            render_subject_policy.refresh()

        render_subject_policy()
