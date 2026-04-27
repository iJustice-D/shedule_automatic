from __future__ import annotations

import asyncio
from contextlib import contextmanager
from itertools import combinations
from pathlib import Path

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
from app.core.week_scope import format_week_scope, scopes_overlap
from app.db.session import engine
from app.models import AcademicPeriod, AppSetting, Conflict, CurriculumLoad, Department, GenerationJob, Group, OnlinePolicy, Room, Schedule, ScheduleEntry, Subject, Suggestion, Teacher
from app.models import OnlineSlot, WeeklyLoad
from app.services.timetable_service import TimetableService
from app.ui.i18n import t


service = TimetableService()
LANG = "ru"
FAVICON_PATH = Path(__file__).resolve().parents[1] / "static" / "favicon.svg"


def tr(key: str, **kwargs: object) -> str:
    return t(key, lang=LANG, **kwargs)


def resolve_favicon() -> str | Path:
    if FAVICON_PATH.exists():
        return FAVICON_PATH
    return "📅"


def register_ui(app: FastAPI) -> None:
    ui.run_with(app, storage_secret=settings.secret_key, title=tr("app.title"), favicon=resolve_favicon())


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
          .entry-chip { width: 100%; justify-content: flex-start; text-align: left; white-space: pre-wrap; padding: 0.65rem 0.75rem; border-radius: 14px; border: 1px solid rgba(107, 114, 128, 0.25); line-height: 1.35; }
          .entry-online { background: var(--online); }
          .entry-offline { background: var(--offline); }
          .entry-hybrid { background: #efe5ff; }
          .slot-stack { display: flex; flex-direction: column; gap: 0.55rem; }
          .lesson-card { width: 100%; align-items: stretch; }
          .lesson-card .q-btn__content { display: block; width: 100%; text-align: left; align-items: flex-start; }
          .lesson-card-conflict { border: 2px solid #dc2626 !important; background: #fff1f2 !important; }
          .lesson-meta { font-size: 0.72rem; color: var(--muted); }
          .empty-slot { color: var(--muted); font-size: 0.78rem; padding-top: 0.35rem; }
          .badge-conflict { display: inline-block; background: #dc2626; color: white; border-radius: 999px; padding: 0.1rem 0.45rem; font-size: 0.68rem; font-weight: 700; margin-bottom: 0.25rem; }
          .badge-overlap { display: inline-block; background: #f59e0b; color: #1f2933; border-radius: 999px; padding: 0.1rem 0.45rem; font-size: 0.68rem; font-weight: 700; margin-left: 0.35rem; }
          .form-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 0.85rem; width: 100%; }
          .modal-text { white-space: pre-wrap; line-height: 1.55; }
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


def load_groups(session: Session, include_inactive: bool = False) -> list[Group]:
    return service.list_groups(session, include_inactive=include_inactive)


def load_teachers(session: Session, include_inactive: bool = False) -> list[Teacher]:
    return service.list_teachers(session, include_inactive=include_inactive)


def load_subjects(
    session: Session,
    include_inactive: bool = False,
    include_duplicates: bool = False,
) -> list[Subject]:
    return service.list_subjects(session, include_inactive=include_inactive, include_duplicates=include_duplicates)


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


def lesson_type_label(value: str) -> str:
    labels = {
        "mixed": tr("lesson_type.mixed"),
        "lecture": tr("lesson_type.lecture"),
        "practice": tr("lesson_type.practice"),
    }
    return labels.get(value, value)


def lesson_type_options() -> dict[str, str]:
    return {key: lesson_type_label(key) for key in ("mixed", "lecture", "practice")}


def source_type_label(value: str) -> str:
    mapping = {
        "imported": tr("curriculum.source_imported"),
        "manual": tr("curriculum.source_manual"),
        "demo": tr("curriculum.source_demo"),
    }
    return mapping.get(value, value)


def severity_label(value: str) -> str:
    return tr(f"severity.{value}")


def generation_status_label(value: str) -> str:
    labels = {
        "pending": tr("generator.status_pending"),
        "running": tr("generator.status_running"),
        "completed": tr("generator.status_completed"),
        "failed": tr("generator.status_failed"),
    }
    return labels.get(value, value)


def teacher_filter_options(rows: list[Teacher]) -> dict[int, str]:
    return {0: tr("common.all"), **selection_options(rows, "full_name")}


def load_online_slot_labels(session: Session) -> dict[int, str]:
    slots = session.exec(select(OnlineSlot).order_by(OnlineSlot.order_index, OnlineSlot.id)).all()
    if slots:
        return {slot.id or 0: slot.label for slot in slots}
    return {slot: online_slot_label(slot, lang=LANG) for slot in online_slot_numbers()}


def load_online_slots(session: Session) -> list[OnlineSlot]:
    slots = session.exec(select(OnlineSlot).order_by(OnlineSlot.order_index, OnlineSlot.id)).all()
    return slots


def week_scope_label(value: str) -> str:
    text = format_week_scope(value, all_weeks_label=tr("editor.all_weeks"))
    if text == tr("editor.all_weeks"):
        return text
    return f"{tr('editor.weeks')}: {text}"


def slot_has_week_conflict(entries: list[ScheduleEntry]) -> bool:
    for left, right in combinations(entries, 2):
        if not scopes_overlap(left.week_scope, right.week_scope):
            continue
        if left.subgroup_code and right.subgroup_code and left.subgroup_code != right.subgroup_code:
            continue
        return True
    return False


def entry_caption(entry: ScheduleEntry, subjects: dict[int, Subject], teachers: dict[int, Teacher], rooms: dict[int, Room]) -> str:
    teacher = teachers[entry.teacher_id].editable_name or teachers[entry.teacher_id].full_name
    room_text = rooms[entry.room_id].code if entry.room_id and entry.room_id in rooms else tr("editor.remove_room")
    subgroup_text = f"{tr('curriculum.subgroup')}: {entry.subgroup_code}\n" if entry.subgroup_code else ""
    return (
        f"{subjects[entry.subject_id].name}\n"
        f"{subgroup_text}"
        f"{teacher}\n"
        f"{delivery_mode_label(entry.delivery_mode, lang=LANG)}\n"
        f"{room_text}\n"
        f"{week_scope_label(entry.week_scope)}"
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
        f"{tr('lesson_mode.online')}\n"
        f"{week_scope_label(entry.week_scope)}"
    )


def button_class_for_entry(entry: ScheduleEntry, conflict: bool = False) -> str:
    suffix = "online" if entry.delivery_mode == "online" else "hybrid" if entry.delivery_mode == "hybrid" else "offline"
    extra = " lesson-card-conflict" if conflict else ""
    return f"entry-chip lesson-card entry-{suffix} text-left text-xs{extra}"


@ui.page("/")
def dashboard_page() -> None:
    with page_shell(tr("page.dashboard")):
        with session_scope() as session:
            groups = load_groups(session)
            teachers = load_teachers(session)
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
            departments = session.exec(select(Department).order_by(Department.code)).all()
        state = {"selected_group_id": None, "include_inactive": False}

        @ui.refreshable
        def render_groups() -> None:
            with session_scope() as session:
                rows = load_groups(session, include_inactive=bool(state["include_inactive"]))
                department_map = {item.id: item for item in session.exec(select(Department)).all()}
            if rows and state["selected_group_id"] not in {row.id for row in rows}:
                state["selected_group_id"] = rows[0].id
            if not rows:
                state["selected_group_id"] = None
            ui.table(
                columns=[
                    {"name": "code", "label": tr("groups.code"), "field": "code"},
                    {"name": "name", "label": tr("groups.name"), "field": "name"},
                    {"name": "course", "label": tr("common.course"), "field": "course"},
                    {"name": "year", "label": tr("groups.year"), "field": "year"},
                    {"name": "semester", "label": tr("common.semester"), "field": "semester"},
                    {"name": "student_count", "label": tr("groups.student_count"), "field": "student_count"},
                    {"name": "shift", "label": tr("common.shift"), "field": "shift"},
                    {"name": "department", "label": tr("common.department"), "field": "department"},
                ],
                rows=[
                    {
                        **row.model_dump(),
                        "shift": shift_label(row.shift, lang=LANG),
                        "department": department_map.get(row.home_department_id).code if row.home_department_id in department_map else tr("common.none"),
                    }
                    for row in rows
                ],
                row_key="id",
            ).classes("panel-card w-full")
            with ui.row().classes("gap-3 mt-3 items-end"):
                ui.switch(
                    tr("groups.show_archived"),
                    value=state["include_inactive"],
                    on_change=lambda event: (state.update({"include_inactive": bool(event.value)}), render_groups.refresh()),
                )
                ui.select(
                    selection_options(rows),
                    value=state["selected_group_id"],
                    label=tr("groups.choose"),
                    on_change=lambda event: state.update({"selected_group_id": event.value}),
                ).classes("w-72")
                ui.button(tr("groups.add"), on_click=lambda: open_group_dialog()).props("color=amber-8")
                ui.button(tr("groups.edit"), on_click=lambda: open_group_dialog(state["selected_group_id"])).props("outline color=dark")
                ui.button(tr("common.delete"), on_click=lambda: delete_group(state["selected_group_id"])).props("outline color=negative")

        def open_group_dialog(group_id: int | None = None) -> None:
            group = None
            if group_id:
                with session_scope() as session:
                    group = session.get(Group, group_id)
            with ui.dialog() as dialog, ui.card().classes("panel-card p-5 min-w-[760px]"):
                ui.label(tr("groups.edit") if group else tr("groups.add")).classes("text-lg font-semibold")
                with ui.column().classes("w-full gap-3"):
                    with ui.element("div").classes("form-grid"):
                        code = ui.input(tr("groups.code"), value=group.code if group else "")
                        name = ui.input(tr("groups.name"), value=group.name if group else "")
                        course = ui.number(tr("common.course"), value=group.course if group else 2, min=1, max=4, precision=0)
                        year = ui.number(tr("groups.year"), value=group.year if group else 2, min=1, max=4, precision=0)
                        semester = ui.number(tr("common.semester"), value=group.semester if group else 3, min=1, max=8, precision=0)
                        student_count = ui.number(tr("groups.student_count"), value=group.student_count if group else 25, min=0, precision=0)
                        shift = ui.select(shift_options(), value=group.shift if group else "morning", label=tr("common.shift"))
                        department = ui.select(
                            selection_options(departments, "code"),
                            value=group.home_department_id if group else (departments[0].id if departments else None),
                            label=tr("common.department"),
                        )
                with ui.row().classes("justify-end gap-2 w-full"):
                    ui.button(tr("common.cancel"), on_click=dialog.close).props("flat")
                    ui.button(
                        tr("common.save"),
                        on_click=lambda: save_group(
                            group_id,
                            {
                                "code": (code.value or "").strip(),
                                "name": (name.value or "").strip() or (code.value or "").strip(),
                                "home_department_id": int(department.value or 0),
                                "course": int(course.value or 1),
                                "year": int(year.value or 1),
                                "semester": int(semester.value or 1),
                                "student_count": int(student_count.value or 0),
                                "shift": shift.value or "morning",
                            },
                            dialog,
                        ),
                    ).props("color=amber-8")
            dialog.open()

        def save_group(group_id: int | None, payload: dict, dialog) -> None:
            try:
                with session_scope() as session:
                    if group_id:
                        service.update_group(session, group_id, payload)
                    else:
                        service.create_group(session, payload)
            except ValueError as exc:
                ui.notify(str(exc), color="negative")
                return
            dialog.close()
            ui.notify(tr("groups.updated") if group_id else tr("groups.created"), color="positive")
            render_groups.refresh()

        def delete_group(group_id: int | None) -> None:
            if not group_id:
                ui.notify(tr("common.required_fields"), color="negative")
                return
            try:
                with session_scope() as session:
                    service.delete_group(session, group_id)
            except ValueError as exc:
                ui.notify(str(exc), color="negative")
                return
            ui.notify(tr("groups.deleted"), color="positive")
            state["selected_group_id"] = None
            render_groups.refresh()

        render_groups()


@ui.page("/teachers")
def teachers_page() -> None:
    with page_shell(tr("page.teachers")):
        with session_scope() as session:
            departments = session.exec(select(Department).order_by(Department.code)).all()
        state = {"selected_teacher_id": None, "include_inactive": False}

        @ui.refreshable
        def render_management() -> None:
            with session_scope() as session:
                rows = load_teachers(session, include_inactive=bool(state["include_inactive"]))
            if rows and state["selected_teacher_id"] is None:
                state["selected_teacher_id"] = rows[0].id
            if not rows:
                state["selected_teacher_id"] = None
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
            with ui.row().classes("gap-3 items-end"):
                ui.switch(
                    tr("teachers.show_archived"),
                    value=state["include_inactive"],
                    on_change=lambda event: (state.update({"include_inactive": bool(event.value)}), render_management.refresh()),
                )
                ui.select(
                    selection_options(rows, "full_name"),
                    value=state["selected_teacher_id"],
                    label=tr("teachers.teacher_to_rename"),
                    on_change=lambda event: state.update({"selected_teacher_id": event.value}),
                ).classes("w-80")

        render_management()
        with ui.row().classes("gap-3 mt-3"):
            with ui.dialog() as create_dialog, ui.card().classes("panel-card p-4 min-w-[420px]"):
                ui.label(tr("teachers.create_demo_teacher")).classes("text-lg font-semibold")
                full_name = ui.input(tr("teachers.full_name"))
                short_name = ui.input(tr("teachers.short_name"))
                department = ui.select(selection_options(departments, "code"), label=tr("common.department"))
                max_pairs = ui.number(tr("teachers.max_weekly_pairs"), value=20, min=6, max=30)
                ui.button(
                    tr("common.save"),
                    on_click=lambda: (
                        _create_teacher(full_name.value, short_name.value, department.value, int(max_pairs.value or 20)),
                        create_dialog.close(),
                    ),
                ).props("color=amber-8")
            ui.button(tr("common.create"), on_click=create_dialog.open).props("color=amber-8")
            rename_input = ui.input(tr("teachers.rename_teacher"))
            ui.button(
                tr("common.rename"),
                on_click=lambda: _rename_selected_teacher(state["selected_teacher_id"], rename_input.value),
            ).props("outline color=dark")

        def _create_teacher(full_name: str, short_name: str, department_id: int | None, max_pairs: int) -> None:
            if not full_name or not department_id:
                ui.notify(tr("teachers.need_teacher_and_department"), color="negative")
                return
            with session_scope() as session:
                service.create_teacher(session, full_name, short_name, department_id, max_pairs)
            ui.notify(tr("teachers.teacher_created"), color="positive")
            render_management.refresh()

        def _rename_selected_teacher(teacher_id: int | None, new_name: str) -> None:
            if not teacher_id or not new_name:
                ui.notify(tr("common.required_fields"), color="negative")
                return
            with session_scope() as session:
                service.rename_teacher(session, teacher_id, new_name)
            ui.notify(tr("teachers.teacher_renamed"), color="positive")
            render_management.refresh()


@ui.page("/subjects")
def subjects_page() -> None:
    with page_shell(tr("page.subjects")):
        with session_scope() as session:
            departments = session.exec(select(Department).order_by(Department.code)).all()
        state = {"selected_subject_id": None, "include_inactive": False, "include_duplicates": False}

        @ui.refreshable
        def render_subjects() -> None:
            with session_scope() as session:
                rows = load_subjects(
                    session,
                    include_inactive=bool(state["include_inactive"]),
                    include_duplicates=bool(state["include_duplicates"]),
                )
                department_map = {item.id: item for item in session.exec(select(Department)).all()}
            if rows and state["selected_subject_id"] not in {row.id for row in rows}:
                state["selected_subject_id"] = rows[0].id
            if not rows:
                state["selected_subject_id"] = None
            ui.table(
                columns=[
                    {"name": "code", "label": tr("subjects.code"), "field": "code"},
                    {"name": "name", "label": tr("subjects.name"), "field": "name"},
                    {"name": "lesson_type", "label": tr("subjects.lesson_type"), "field": "lesson_type"},
                    {"name": "requires_special_room", "label": tr("subjects.special_room"), "field": "requires_special_room"},
                    {"name": "can_be_online", "label": tr("subjects.can_be_online"), "field": "can_be_online"},
                    {"name": "default_delivery_mode", "label": tr("subjects.default_delivery_mode"), "field": "default_delivery_mode"},
                    {"name": "department", "label": tr("common.department"), "field": "department"},
                ],
                rows=[
                    {
                        **row.model_dump(),
                        "lesson_type": lesson_type_label(row.lesson_type),
                        "requires_special_room": bool_label(row.requires_special_room),
                        "can_be_online": bool_label(row.can_be_online),
                        "default_delivery_mode": delivery_mode_label(row.default_delivery_mode, lang=LANG),
                        "department": department_map.get(row.owner_department_id).code if row.owner_department_id in department_map else tr("common.none"),
                    }
                    for row in rows
                ],
                row_key="id",
            ).classes("panel-card w-full")
            with ui.row().classes("gap-3 mt-3 items-end"):
                ui.switch(
                    tr("subjects.show_archived"),
                    value=state["include_inactive"],
                    on_change=lambda event: (state.update({"include_inactive": bool(event.value)}), render_subjects.refresh()),
                )
                ui.switch(
                    tr("subjects.show_duplicates"),
                    value=state["include_duplicates"],
                    on_change=lambda event: (state.update({"include_duplicates": bool(event.value)}), render_subjects.refresh()),
                )
                ui.select(
                    selection_options(rows, "name"),
                    value=state["selected_subject_id"],
                    label=tr("subjects.choose"),
                    on_change=lambda event: state.update({"selected_subject_id": event.value}),
                ).classes("w-80")
                ui.button(tr("subjects.add"), on_click=lambda: open_subject_dialog()).props("color=amber-8")
                ui.button(tr("subjects.edit"), on_click=lambda: open_subject_dialog(state["selected_subject_id"])).props("outline color=dark")
                ui.button(tr("common.delete"), on_click=lambda: delete_subject(state["selected_subject_id"])).props("outline color=negative")

        def open_subject_dialog(subject_id: int | None = None) -> None:
            subject = None
            if subject_id:
                with session_scope() as session:
                    subject = session.get(Subject, subject_id)
            with ui.dialog() as dialog, ui.card().classes("panel-card p-5 min-w-[780px]"):
                ui.label(tr("subjects.edit") if subject else tr("subjects.add")).classes("text-lg font-semibold")
                with ui.element("div").classes("form-grid"):
                    code = ui.input(tr("subjects.code"), value=subject.code if subject else "")
                    name = ui.input(tr("subjects.name"), value=subject.name if subject else "")
                    lesson_type = ui.select(lesson_type_options(), value=subject.lesson_type if subject else "mixed", label=tr("subjects.lesson_type"))
                    requires_special_room = ui.switch(tr("subjects.special_room"), value=subject.requires_special_room if subject else False)
                    can_be_online = ui.switch(tr("subjects.can_be_online"), value=subject.can_be_online if subject else False)
                    default_delivery_mode = ui.select(delivery_options(), value=subject.default_delivery_mode if subject else "offline", label=tr("subjects.default_delivery_mode"))
                    department = ui.select(
                        selection_options(departments, "code"),
                        value=subject.owner_department_id if subject else (departments[0].id if departments else None),
                        label=tr("common.department"),
                    )
                with ui.row().classes("justify-end gap-2 w-full mt-3"):
                    ui.button(tr("common.cancel"), on_click=dialog.close).props("flat")
                    ui.button(
                        tr("common.save"),
                        on_click=lambda: save_subject(
                            subject_id,
                            {
                                "code": (code.value or "").strip(),
                                "name": (name.value or "").strip(),
                                "owner_department_id": int(department.value or 0),
                                "lesson_type": lesson_type.value or "mixed",
                                "requires_special_room": bool(requires_special_room.value),
                                "can_be_online": bool(can_be_online.value),
                                "default_delivery_mode": default_delivery_mode.value or "offline",
                            },
                            dialog,
                        ),
                    ).props("color=amber-8")
            dialog.open()

        def save_subject(subject_id: int | None, payload: dict, dialog) -> None:
            try:
                with session_scope() as session:
                    if subject_id:
                        service.update_subject(session, subject_id, payload)
                    else:
                        service.create_subject(session, payload)
            except ValueError as exc:
                ui.notify(str(exc), color="negative")
                return
            dialog.close()
            ui.notify(tr("subjects.updated") if subject_id else tr("subjects.created"), color="positive")
            render_subjects.refresh()

        def delete_subject(subject_id: int | None) -> None:
            if not subject_id:
                ui.notify(tr("common.required_fields"), color="negative")
                return
            try:
                with session_scope() as session:
                    service.delete_subject(session, subject_id)
            except ValueError as exc:
                ui.notify(str(exc), color="negative")
                return
            ui.notify(tr("subjects.deleted"), color="positive")
            state["selected_subject_id"] = None
            render_subjects.refresh()

        render_subjects()


@ui.page("/calendar")
def calendar_page() -> None:
    with page_shell(tr("page.calendar")):
        with session_scope() as session:
            groups = load_groups(session)
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
            groups = load_groups(session)
            subjects = load_subjects(session)
        state = {
            "group_id": groups[0].id if groups else None,
            "semester": 3,
            "subject_filter": 0,
            "selected_load_id": None,
        }

        with ui.card().classes("panel-card p-4 w-full"):
            ui.label(tr("curriculum.weekly_import_title")).classes("text-lg font-semibold")
            workload_path = ui.input(
                tr("curriculum.weekly_import_path"),
                value=str(settings.weekly_workload_source or ""),
                placeholder=str(settings.weekly_workload_source or ""),
            ).classes("w-full")
            import_selected_only = ui.switch(tr("curriculum.import_selected_group_only"), value=True)

            def import_weekly_workload() -> None:
                try:
                    target_group_codes = None
                    if import_selected_only.value and state["group_id"]:
                        group = next((item for item in groups if item.id == state["group_id"]), None)
                        target_group_codes = [group.code] if group else None
                    with session_scope() as session:
                        service.import_weekly_workload(
                            session,
                            Path(str(workload_path.value)),
                            calendar_path=settings.calendar_source,
                            curriculum_path=settings.curriculum_source,
                            group_codes=target_group_codes,
                        )
                except ValueError as exc:
                    ui.notify(str(exc), color="negative", multi_line=True)
                    return
                ui.notify(tr("curriculum.weekly_import_done"), color="positive", multi_line=True)
                render_weekly_workload.refresh()
                render_loads.refresh()

            with ui.row().classes("gap-3 items-end w-full"):
                ui.button(tr("curriculum.weekly_import_button"), on_click=import_weekly_workload).props("color=amber-8")
                ui.label(tr("curriculum.weekly_import_hint")).classes("text-sm text-[#6b7280]")

        @ui.refreshable
        def render_weekly_workload() -> None:
            with session_scope() as session:
                query = select(WeeklyLoad).where(WeeklyLoad.is_active.is_(True))
                if state["group_id"]:
                    query = query.where(WeeklyLoad.group_id == state["group_id"])
                if state["semester"]:
                    query = query.where(WeeklyLoad.semester == state["semester"])
                rows = session.exec(query.order_by(WeeklyLoad.group_id, WeeklyLoad.semester, WeeklyLoad.subject_id)).all()
                subject_map = {subject.id: subject for subject in session.exec(select(Subject)).all()}
                group_map = {group.id: group for group in session.exec(select(Group)).all()}
                unresolved_rows = service.unresolved_weekly_rows(session, semester=state["semester"])
                balance_rows = service.teacher_balance_report(session)
            with ui.card().classes("panel-card p-4 w-full mt-4"):
                ui.label(tr("curriculum.weekly_review_title")).classes("text-lg font-semibold")
                if not rows:
                    ui.label(tr("curriculum.weekly_empty")).classes("text-sm text-[#6b7280]")
                else:
                    ui.table(
                        columns=[
                            {"name": "group", "label": tr("common.group"), "field": "group"},
                            {"name": "subject", "label": tr("common.subject"), "field": "subject"},
                            {"name": "semester", "label": tr("common.semester"), "field": "semester"},
                            {"name": "category", "label": tr("curriculum.weekly_category"), "field": "category"},
                            {"name": "subgroup", "label": tr("curriculum.subgroup"), "field": "subgroup"},
                            {"name": "weekly_hours", "label": tr("curriculum.hours_per_week"), "field": "weekly_hours"},
                            {"name": "weekly_pairs", "label": tr("curriculum.pairs_per_week"), "field": "weekly_pairs"},
                            {"name": "teacher_state", "label": tr("curriculum.teacher_state"), "field": "teacher_state"},
                            {"name": "teachers", "label": tr("curriculum.teacher_names"), "field": "teachers"},
                        ],
                        rows=[
                            {
                                "id": row.id,
                                "group": group_map.get(row.group_id).code if row.group_id in group_map else row.group_id,
                                "subject": subject_map.get(row.subject_id).name if row.subject_id in subject_map else row.subject_id,
                                "semester": row.semester,
                                "category": tr(f"curriculum.category_{row.load_category}") if row.load_category in {"regular", "facultative", "practice", "study_practice", "industrial_practice"} else row.load_category,
                                "subgroup": row.subgroup_code or "—",
                                "weekly_hours": row.weekly_hours,
                                "weekly_pairs": row.weekly_pairs,
                                "teacher_state": tr(f"curriculum.assignment_{row.assignment_state}") if row.assignment_state in {"fixed", "multi_teacher", "multi_teacher_ambiguous", "vacancy", "unresolved_manual_review", "candidate_pool"} else row.assignment_state,
                                "teachers": row.raw_teacher_names or "—",
                            }
                            for row in rows
                        ],
                        row_key="id",
                    ).classes("w-full")
            with ui.row().classes("w-full gap-4 mt-4 items-start"):
                with ui.card().classes("panel-card p-4 grow"):
                    ui.label(tr("curriculum.unresolved_title")).classes("text-lg font-semibold")
                    if not unresolved_rows:
                        ui.label(tr("curriculum.unresolved_empty")).classes("text-sm text-[#6b7280]")
                    else:
                        for row in unresolved_rows[:20]:
                            subject_name = subject_map.get(row.subject_id).name if row.subject_id in subject_map else str(row.subject_id)
                            group_name = group_map.get(row.group_id).code if row.group_id in group_map else str(row.group_id)
                            ui.label(
                                f"{group_name} | {subject_name} | "
                                f"{tr(f'curriculum.assignment_{row.assignment_state}') if row.assignment_state in {'fixed', 'multi_teacher', 'multi_teacher_ambiguous', 'vacancy', 'unresolved_manual_review', 'candidate_pool'} else row.assignment_state}"
                            ).classes("text-sm")
                with ui.card().classes("panel-card p-4 grow"):
                    ui.label(tr("curriculum.teacher_balance_title")).classes("text-lg font-semibold")
                    if not balance_rows:
                        ui.label(tr("curriculum.teacher_balance_empty")).classes("text-sm text-[#6b7280]")
                    else:
                        ui.table(
                            columns=[
                                {"name": "teacher_name", "label": tr("common.teacher"), "field": "teacher_name"},
                                {"name": "semester_3_pairs", "label": f"{tr('common.semester')} 3", "field": "semester_3_pairs"},
                                {"name": "semester_4_pairs", "label": f"{tr('common.semester')} 4", "field": "semester_4_pairs"},
                                {"name": "normalized_balance_score", "label": tr("curriculum.balance_score"), "field": "normalized_balance_score"},
                                {"name": "pending_rows", "label": tr("curriculum.pending_rows"), "field": "pending_rows"},
                            ],
                            rows=balance_rows,
                            row_key="teacher_id",
                        ).classes("w-full")

        @ui.refreshable
        def render_loads() -> None:
            with session_scope() as session:
                query = select(CurriculumLoad).where(CurriculumLoad.group_id == state["group_id"])
                if state["semester"]:
                    query = query.where(CurriculumLoad.semester == state["semester"])
                if state["subject_filter"]:
                    query = query.where(CurriculumLoad.subject_id == state["subject_filter"])
                loads = session.exec(query.order_by(CurriculumLoad.subject_id)).all()
                subject_map = {subject.id: subject for subject in session.exec(select(Subject)).all()}
                duplicate_groups: dict[tuple[int, int, int], int] = {}
                for item in session.exec(select(CurriculumLoad).where(CurriculumLoad.group_id == state["group_id"])).all():
                    key = (item.group_id, item.subject_id, item.semester)
                    duplicate_groups[key] = duplicate_groups.get(key, 0) + 1
            if loads and state["selected_load_id"] not in {load.id for load in loads}:
                state["selected_load_id"] = loads[0].id
            has_duplicates = any(count > 1 for count in duplicate_groups.values())
            if has_duplicates:
                ui.label(tr("curriculum.duplicates_warning")).classes("text-sm text-[#9a3412]")
            rows = [
                {
                    "id": load.id,
                    "subject": subject_map.get(load.subject_id).name if load.subject_id in subject_map else tr("common.none"),
                    "total_hours": load.total_hours,
                    "raw_total_hours": load.raw_total_hours,
                    "practice_hours": load.practice_hours,
                    "study_weeks": load.study_weeks,
                    "hours_per_week": load.hours_per_week,
                    "pairs_per_week": load.pairs_per_week,
                    "lesson_type": lesson_type_label(load.lesson_type),
                    "delivery_mode": delivery_mode_label(load.delivery_mode, lang=LANG),
                    "source_type": source_type_label(load.source_type),
                    "note": load.note,
                }
                for load in loads
            ]
            ui.table(
                columns=[
                    {"name": "subject", "label": tr("common.subject"), "field": "subject"},
                    {"name": "source_type", "label": tr("common.source"), "field": "source_type"},
                    {"name": "total_hours", "label": tr("curriculum.schedulable_hours"), "field": "total_hours"},
                    {"name": "raw_total_hours", "label": tr("curriculum.raw_hours"), "field": "raw_total_hours"},
                    {"name": "practice_hours", "label": tr("curriculum.practice_hours"), "field": "practice_hours"},
                    {"name": "study_weeks", "label": tr("curriculum.study_weeks"), "field": "study_weeks"},
                    {"name": "hours_per_week", "label": tr("curriculum.hours_per_week"), "field": "hours_per_week"},
                    {"name": "pairs_per_week", "label": tr("curriculum.pairs_per_week"), "field": "pairs_per_week"},
                    {"name": "lesson_type", "label": tr("subjects.lesson_type"), "field": "lesson_type"},
                    {"name": "delivery_mode", "label": tr("common.delivery_mode"), "field": "delivery_mode"},
                    {"name": "note", "label": tr("common.note"), "field": "note"},
                ],
                rows=rows,
                row_key="id",
            ).classes("panel-card w-full")
            with ui.row().classes("gap-3 mt-3 items-end"):
                ui.select(
                    {0: tr("common.all"), **{subject.id or 0: subject.name for subject in subjects}},
                    value=state["subject_filter"],
                    label=tr("common.subject"),
                    on_change=lambda event: (state.update({"subject_filter": event.value}), render_loads.refresh()),
                ).classes("w-80")
                ui.select(
                    {load.id or 0: f"{subject_map.get(load.subject_id).name if load.subject_id in subject_map else tr('common.none')} | {tr('common.semester')} {load.semester}" for load in loads},
                    value=state["selected_load_id"],
                    label=tr("common.select_record"),
                    on_change=lambda event: state.update({"selected_load_id": event.value}),
                ).classes("w-[26rem]")
                ui.button(tr("curriculum.add"), on_click=lambda: open_load_dialog()).props("color=amber-8")
                ui.button(tr("curriculum.edit"), on_click=lambda: open_load_dialog(state["selected_load_id"])).props("outline color=dark")
                ui.button(tr("common.delete"), on_click=lambda: delete_load(state["selected_load_id"])).props("outline color=negative")

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
        def open_load_dialog(load_id: int | None = None) -> None:
            with session_scope() as session:
                subjects_local = load_subjects(session)
                load = session.get(CurriculumLoad, load_id) if load_id else None
                subject = session.get(Subject, load.subject_id) if load else None
            with ui.dialog() as dialog, ui.card().classes("panel-card p-5 min-w-[860px]"):
                ui.label(tr("curriculum.edit") if load else tr("curriculum.add")).classes("text-lg font-semibold")
                with ui.element("div").classes("form-grid"):
                    group_id = ui.select(selection_options(groups), value=load.group_id if load else state["group_id"], label=tr("common.group"))
                    subject_id = ui.select(
                        selection_options(subjects_local, "name"),
                        value=load.subject_id if load else None,
                        label=tr("common.subject"),
                    )
                    semester = ui.select(
                        {3: f"{tr('common.semester')} 3", 4: f"{tr('common.semester')} 4"},
                        value=load.semester if load else state["semester"],
                        label=tr("common.semester"),
                    )
                    total_hours = ui.number(tr("curriculum.total_hours"), value=load.total_hours if load else 64, min=0, precision=0)
                    study_weeks = ui.number(tr("curriculum.study_weeks"), value=load.study_weeks if load else 16, min=1, precision=0)
                    hours_per_week = ui.number(tr("curriculum.hours_per_week"), value=load.hours_per_week if load else 4.0, min=0, precision=2)
                    pairs_per_week = ui.number(tr("curriculum.pairs_per_week"), value=load.pairs_per_week if load else 2.0, min=0, precision=2)
                    lesson_type = ui.select(lesson_type_options(), value=load.lesson_type if load else (subject.lesson_type if subject else "mixed"), label=tr("subjects.lesson_type"))
                    delivery_mode = ui.select(delivery_options(), value=load.delivery_mode if load else (subject.default_delivery_mode if subject else "offline"), label=tr("curriculum.delivery_type"))
                    can_be_online = ui.switch(tr("subjects.can_be_online"), value=subject.can_be_online if subject else False)
                    note = ui.textarea(tr("curriculum.manual_note"), value=load.note if load else "").classes("col-span-full")
                auto_calc = ui.switch(tr("common.auto_calculation"), value=True)
                ui.label(tr("curriculum.online_hint")).classes("text-xs text-[#6b7280]")

                def recalculate(notify: bool = False) -> None:
                    if not auto_calc.value:
                        return
                    weeks_value = float(study_weeks.value or 0)
                    total_value = float(total_hours.value or 0)
                    if weeks_value <= 0:
                        return
                    hours_value = round(total_value / weeks_value, 2)
                    pairs_value = round(total_value / 2 / weeks_value, 2)
                    hours_per_week.value = hours_value
                    pairs_per_week.value = pairs_value
                    if notify:
                        ui.notify(tr("notify.calculated"), color="positive")

                total_hours.on("update:model-value", lambda _event: recalculate())
                study_weeks.on("update:model-value", lambda _event: recalculate())
                subject_id.on(
                    "update:model-value",
                    lambda event: _apply_subject_defaults(event.value, subjects_local, lesson_type, delivery_mode, can_be_online),
                )

                with ui.row().classes("justify-between items-center w-full mt-3"):
                    ui.button(tr("common.calculate"), on_click=lambda: recalculate(True)).props("outline color=dark")
                    with ui.row().classes("gap-2"):
                        ui.button(tr("common.cancel"), on_click=dialog.close).props("flat")
                        ui.button(
                            tr("common.save"),
                            on_click=lambda: save_load(
                                load_id,
                                {
                                    "group_id": int(group_id.value or 0),
                                    "subject_id": int(subject_id.value or 0),
                                    "semester": int(semester.value or state["semester"] or 3),
                                    "total_hours": int(total_hours.value or 0),
                                    "study_weeks": int(study_weeks.value or 0),
                                    "hours_per_week": float(hours_per_week.value or 0),
                                    "pairs_per_week": float(pairs_per_week.value or 0),
                                    "lesson_type": lesson_type.value or "mixed",
                                    "delivery_mode": delivery_mode.value or "offline",
                                    "raw_total_hours": int(total_hours.value or 0),
                                    "practice_hours": load.practice_hours if load else 0,
                                    "source_code": load.source_code if load else "manual",
                                    "source_type": load.source_type if load else "manual",
                                    "note": note.value or "",
                                },
                                bool(can_be_online.value),
                                dialog,
                            ),
                        ).props("color=amber-8")
            dialog.open()

        def _apply_subject_defaults(subject_id: int | None, subject_rows: list[Subject], lesson_type_widget, delivery_widget, online_widget) -> None:
            subject = next((item for item in subject_rows if item.id == subject_id), None)
            if subject is None:
                return
            lesson_type_widget.value = subject.lesson_type
            delivery_widget.value = subject.default_delivery_mode
            online_widget.value = subject.can_be_online

        def save_load(load_id: int | None, payload: dict, allow_online: bool, dialog) -> None:
            try:
                with session_scope() as session:
                    subject = session.get(Subject, payload["subject_id"])
                    if subject is not None:
                        subject.can_be_online = allow_online
                        session.add(subject)
                        session.commit()
                        if not load_id:
                            payload["source_code"] = subject.code
                    if load_id:
                        service.update_curriculum_load(session, load_id, payload)
                    else:
                        service.create_curriculum_load(session, payload)
            except ValueError as exc:
                ui.notify(str(exc), color="negative", multi_line=True)
                return
            dialog.close()
            ui.notify(tr("curriculum.updated") if load_id else tr("curriculum.created"), color="positive")
            render_loads.refresh()

        def delete_load(load_id: int | None) -> None:
            if not load_id:
                ui.notify(tr("common.required_fields"), color="negative")
                return
            try:
                with session_scope() as session:
                    service.delete_curriculum_load(session, load_id)
            except ValueError as exc:
                ui.notify(str(exc), color="negative")
                return
            state["selected_load_id"] = None
            ui.notify(tr("curriculum.deleted"), color="positive")
            render_loads.refresh()

        render_loads()
        render_weekly_workload()


@ui.page("/generator")
def generator_page() -> None:
    with page_shell(tr("page.generator")):
        with session_scope() as session:
            groups = load_groups(session)
        initial_group_id = groups[0].id if groups else None
        state = {
            "group_id": initial_group_id,
            "semester": 4,
            "latest_result_id": None,
            "all_groups": False,
        }

        with ui.dialog() as generation_dialog, ui.card().classes("panel-card p-5 min-w-[620px]"):
            ui.label(tr("generator.running")).classes("text-lg font-semibold")
            step_label = ui.label(tr("generator.stage_prepare")).classes("text-sm text-[#6b7280]")
            progress_bar = ui.linear_progress(value=0.0).classes("w-full")
            status_label = ui.label(tr("generator.status_pending")).classes("text-sm")
            summary_label = ui.label("").classes("text-sm modal-text")
            with ui.row().classes("justify-end gap-2 w-full mt-4"):
                open_result_button = ui.button(
                    tr("generator.open_result"),
                    on_click=lambda: ui.navigate.to(f"/editor/{state['latest_result_id']}"),
                ).props("color=amber-8")
                open_result_button.disable()
                ui.button(tr("common.cancel"), on_click=generation_dialog.close).props("flat")

        with ui.row().classes("gap-3 w-full items-end"):
            group_select = ui.select(selection_options(groups), value=state["group_id"], label=tr("common.group")).classes("w-[26rem]")
            semester = ui.select({3: f"{tr('common.semester')} 3", 4: f"{tr('common.semester')} 4"}, value=state["semester"], label=tr("common.semester")).classes("w-56")
            all_groups = ui.switch(tr("generator.all_groups"), value=False)
            include_facultatives = ui.switch(tr("generator.include_facultatives"), value=False)
            enable_online = ui.switch(tr("generator.enable_online"), value=True)
        schedule_name = ui.input(tr("generator.schedule_name"), value="").classes("w-full")
        all_groups.on_value_change(lambda e: group_select.disable() if e.value else group_select.enable())

        @ui.refreshable
        def render_history() -> None:
            with session_scope() as session:
                jobs = service.list_generation_jobs(session, limit=12)
                group_map = {group.id: group for group in session.exec(select(Group)).all()}
            ui.label(tr("generator.history")).classes("text-lg font-semibold mt-4")
            if not jobs:
                ui.label(tr("generator.history_empty")).classes("text-sm text-[#6b7280]")
                return
            with ui.column().classes("w-full gap-3"):
                for job in jobs:
                    group = group_map.get(job.group_id)
                    scope_label = group.code if group else tr("generator.all_groups_label")
                    with ui.card().classes("panel-card p-4 w-full"):
                        ui.label(
                            f"{scope_label} | {tr('common.semester')} {job.semester} | "
                            f"{generation_status_label(job.status)}"
                        ).classes("font-semibold")
                        ui.label(job.summary_message or "—").classes("text-sm text-[#6b7280]")
                        with ui.row().classes("gap-2 mt-2"):
                            ui.label(f"ID: {job.id}").classes("text-xs text-[#6b7280]")
                            ui.label(str(job.created_at)).classes("text-xs text-[#6b7280]")
                            with session_scope() as session:
                                job_results = service.job_results(session, job.id or 0)
                            if job.result_schedule_id and not job_results:
                                ui.button(
                                    tr("generator.open_result"),
                                    on_click=lambda result_id=job.result_schedule_id: ui.navigate.to(f"/editor/{result_id}"),
                                ).props("outline color=dark")
                            for schedule in job_results[:8]:
                                ui.button(
                                    f"{tr('generator.open_result')}: {schedule.group_scope}",
                                    on_click=lambda result_id=schedule.id: ui.navigate.to(f"/editor/{result_id}"),
                                ).props("outline color=dark")

        async def _generate_schedule() -> None:
            if not all_groups.value and not group_select.value:
                ui.notify(tr("generator.group_required"), color="negative")
                return
            try:
                with session_scope() as session:
                    job = service.create_generation_job(
                        session,
                        group_id=int(group_select.value) if group_select.value and not all_groups.value else None,
                        semester=int(semester.value),
                        requested_name=schedule_name.value or "",
                        run_scope="all_groups" if all_groups.value else "single_group",
                        generation_mode="best_effort",
                        include_facultatives=bool(include_facultatives.value),
                        enable_online=bool(enable_online.value),
                        source_scope="normalized_weekly",
                    )
            except ValueError as exc:
                ui.notify(str(exc), color="negative", multi_line=True)
                return

            state["latest_result_id"] = None
            step_label.set_text(tr("generator.stage_prepare"))
            status_label.set_text(tr("generator.status_pending"))
            summary_label.set_text("")
            progress_bar.value = 0.0
            open_result_button.disable()
            generation_dialog.open()

            task = asyncio.create_task(asyncio.to_thread(service.run_generation_job, job.id or 0))
            while not task.done():
                with session_scope() as session:
                    current_job = service.get_generation_job(session, job.id or 0)
                if current_job is not None:
                    step_label.set_text(current_job.summary_message or tr("generator.running"))
                    status_label.set_text(generation_status_label(current_job.status))
                    progress_bar.value = max(0.0, min(float(current_job.progress_percent) / 100.0, 1.0))
                await asyncio.sleep(0.1)

            finished_job = await task
            status_label.set_text(generation_status_label(finished_job.status))
            progress_bar.value = 1.0
            if finished_job.status == "completed":
                step_label.set_text(tr("generator.done"))
            summary_label.set_text(finished_job.summary_message or "")
            render_history.refresh()
            if finished_job.status != "completed" or not finished_job.result_schedule_id:
                ui.notify(finished_job.summary_message or tr("generator.failed_generic"), color="negative", multi_line=True)
                return
            state["latest_result_id"] = finished_job.result_schedule_id
            open_result_button.enable()
            result_name = tr("generator.all_groups_label") if all_groups.value else f"{group_select.options.get(group_select.value, '')} / {tr('common.semester')} {semester.value}"
            ui.notify(tr("generator.created", name=result_name), color="positive")

        ui.button(
            tr("generator.run"),
            on_click=_generate_schedule,
        ).props("color=amber-8")
        render_history()


def _render_editor_page(result_id: int | None = None) -> None:
    with page_shell(tr("page.editor")):
        with session_scope() as session:
            groups = load_groups(session)
            teachers = load_teachers(session)
            rooms = session.exec(select(Room).order_by(Room.code)).all()
            jobs = service.list_generation_jobs(session, limit=40)
        group_map = {group.id: group for group in groups}
        initial_group_id = groups[0].id if groups else None
        initial_semester = 4 if groups else 3
        initial_schedule_id = result_id
        if result_id is not None:
            with session_scope() as session:
                selected_schedule = session.get(Schedule, result_id)
                if selected_schedule is not None:
                    initial_semester = selected_schedule.semester
                    codes = [item.strip() for item in (selected_schedule.group_scope or "").split(",") if item.strip()]
                    group = session.exec(select(Group).where(Group.code == codes[0])).first() if len(codes) == 1 else None
                    if group is not None:
                        initial_group_id = group.id
        if initial_schedule_id is None and initial_group_id is not None:
            with session_scope() as session:
                latest = service.latest_result_for_scope(session, group_id=int(initial_group_id), semester=initial_semester)
                initial_schedule_id = latest.id if latest else None
        state = {
            "schedule_id": initial_schedule_id,
            "view_mode": "group",
            "group_id": initial_group_id,
            "semester": initial_semester,
            "teacher_id": 0,
            "shift_filter": "all",
        }

        def result_options(group_id: int | None, semester: int) -> dict[int, str]:
            if group_id is None:
                return {}
            with session_scope() as session:
                scoped_results = service.list_results_for_scope(session, group_id=group_id, semester=semester, limit=30)
            options: dict[int, str] = {}
            for schedule in scoped_results:
                options[schedule.id or 0] = schedule.created_at.strftime("%Y-%m-%d %H:%M:%S")
            return options

        @ui.refreshable
        def render_grid() -> None:
            if not state["schedule_id"]:
                ui.label(tr("editor.no_scoped_schedule")).classes("text-sm text-[#6b7280]")
                return
            with session_scope() as session:
                entries = session.exec(select(ScheduleEntry).where(ScheduleEntry.schedule_id == state["schedule_id"])).all()
                subjects = {subject.id: subject for subject in session.exec(select(Subject)).all()}
                teacher_map = {teacher.id: teacher for teacher in session.exec(select(Teacher)).all()}
                room_map = {room.id: room for room in session.exec(select(Room)).all()}
                slot_labels = load_online_slot_labels(session)
                online_slots = [slot for slot in load_online_slots(session) if slot.is_active]
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

            def render_slot_cards(slot_entries: list[ScheduleEntry], *, online: bool = False) -> None:
                if not slot_entries:
                    ui.label(tr("common.free")).classes("empty-slot")
                    return
                has_conflict = slot_has_week_conflict(slot_entries)
                with ui.column().classes("slot-stack w-full"):
                    for entry in sorted(
                        slot_entries,
                        key=lambda item: (
                            item.online_slot_number or 0,
                            item.subject_id,
                            format_week_scope(item.week_scope, all_weeks_label=tr("editor.all_weeks")),
                        ),
                    ):
                        caption = (
                            online_entry_caption(entry, subjects, teacher_map, slot_labels)
                            if online
                            else entry_caption(entry, subjects, teacher_map, room_map)
                        )
                        if has_conflict:
                            caption = (
                                f"{tr('editor.conflict_badge')} | {tr('editor.overlap_badge')}\n"
                                f"{caption}"
                            )
                        ui.button(
                            caption,
                            on_click=lambda e=entry: open_edit_dialog(e.id or 0),
                        ).props("flat").classes(button_class_for_entry(entry, conflict=has_conflict))

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
                                render_slot_cards(slot_entries)
                ui.label(tr("editor.online_schedule")).classes("text-lg font-semibold mt-4")
                if not online_entries:
                    ui.label(tr("editor.no_online_lessons")).classes("text-sm text-[#6b7280]")
                else:
                    with ui.row().classes("w-full gap-3"):
                        for day_of_week in sorted({slot.day_of_week for slot in online_slots} or set(ONLINE_ALLOWED_DAYS)):
                            with ui.card().classes("panel-card p-4 grow"):
                                ui.label(day_label(day_of_week, lang=LANG)).classes("font-bold")
                                day_entries = [
                                    entry
                                    for entry in online_entries
                                    if entry.day_of_week == day_of_week
                                ]
                                render_slot_cards(day_entries, online=True)

        @ui.refreshable
        def render_feedback() -> None:
            if not state["schedule_id"]:
                ui.label(tr("editor.no_scoped_schedule")).classes("text-sm text-[#6b7280]")
                return
            with session_scope() as session:
                diagnostics = service.result_diagnostics(session, state["schedule_id"], state["group_id"] or None)
                related_conflict_ids = [
                    conflict.id
                    for conflict in diagnostics["hard_conflicts"] + diagnostics["unscheduled_conflicts"]
                    if conflict.id is not None
                ]
                suggestions = session.exec(
                    select(Suggestion).where(
                        Suggestion.conflict_id.in_(related_conflict_ids)
                    )
                ).all()
            summary = diagnostics["summary"]
            schedule = diagnostics["schedule"]
            ui.label(
                tr(
                    "editor.current_result_meta",
                    result_id=schedule.id,
                    created_at=schedule.created_at.strftime("%Y-%m-%d %H:%M:%S"),
                )
            ).classes("text-sm text-[#6b7280] mt-3")
            ui.label(tr("editor.result_summary")).classes("text-lg font-semibold mt-4")
            with ui.grid(columns=4).classes("w-full gap-3"):
                summary_rows = [
                    (tr("editor.summary_group"), summary["selected_group"]),
                    (tr("editor.summary_semester"), summary["selected_semester"]),
                    (tr("editor.summary_expected_subjects"), summary["expected_subjects_count"]),
                    (tr("editor.summary_fully_placed"), summary["fully_placed_subjects_count"]),
                    (tr("editor.summary_partially_placed"), summary["partially_placed_subjects_count"]),
                    (tr("editor.summary_not_placed"), summary["not_placed_subjects_count"]),
                    (tr("editor.summary_missing_pairs"), summary["total_missing_pairs"]),
                    (tr("editor.summary_hard_conflicts"), summary["hard_conflicts_count"]),
                    (tr("editor.summary_online_placed"), summary["online_placed_count"]),
                    (tr("editor.summary_online_missing"), summary["online_missing_count"]),
                    (tr("editor.summary_unresolved_rows"), summary["unresolved_teacher_rows_count"]),
                    (tr("editor.summary_teacher_balance"), summary["teachers_with_balance_issue_count"]),
                ]
                for label, value in summary_rows:
                    with ui.card().classes("panel-card p-3"):
                        ui.label(label).classes("text-xs text-[#6b7280]")
                        ui.label(str(value)).classes("text-xl font-semibold")

            def render_conflict_section(title: str, conflicts: list[Conflict], empty_label: str) -> None:
                ui.label(title).classes("text-lg font-semibold mt-4")
                if not conflicts:
                    ui.label(empty_label).classes("text-sm text-[#6b7280]")
                    return
                for conflict in conflicts:
                    with ui.expansion(f"[{severity_label(conflict.severity)}] {conflict.message}", value=False).classes("panel-card w-full"):
                        current_suggestions = sorted(
                            [item for item in suggestions if item.conflict_id == conflict.id],
                            key=lambda item: item.rank,
                        )
                        for suggestion in current_suggestions:
                            ui.label(f"{suggestion.rank}. {suggestion.message}")

            render_conflict_section(
                tr("editor.hard_conflicts"),
                diagnostics["hard_conflicts"],
                tr("editor.no_hard_conflicts"),
            )
            render_conflict_section(
                tr("editor.unscheduled_load"),
                diagnostics["unscheduled_conflicts"],
                tr("editor.no_unscheduled_load"),
            )

            ui.label(tr("editor.warnings")).classes("text-lg font-semibold mt-4")
            if not diagnostics["warnings"]:
                ui.label(tr("editor.no_warnings")).classes("text-sm text-[#6b7280]")
            else:
                for item in diagnostics["warnings"]:
                    with ui.card().classes("panel-card w-full p-3"):
                        ui.label(str(item["message"])).classes("text-sm")

            ui.label(tr("editor.normalization_issues")).classes("text-lg font-semibold mt-4")
            if not diagnostics["normalization_issues"]:
                ui.label(tr("editor.no_normalization_issues")).classes("text-sm text-[#6b7280]")
            else:
                for issue in diagnostics["normalization_issues"]:
                    with ui.card().classes("panel-card w-full p-3"):
                        ui.label(str(issue["subject"])).classes("font-semibold")
                        ui.label(str(issue["message"])).classes("text-sm text-[#6b7280]")

            ui.label(tr("editor.teacher_balance")).classes("text-lg font-semibold mt-4")
            if not diagnostics["teacher_balance_rows"]:
                ui.label(tr("editor.no_teacher_balance")).classes("text-sm text-[#6b7280]")
            else:
                ui.table(
                    columns=[
                        {"name": "teacher_name", "label": tr("common.teacher"), "field": "teacher_name"},
                        {"name": "semester_3_pairs", "label": "Семестр 3", "field": "semester_3_pairs"},
                        {"name": "semester_4_pairs", "label": "Семестр 4", "field": "semester_4_pairs"},
                        {"name": "normalized_balance_score", "label": "Отклонение", "field": "normalized_balance_score"},
                        {"name": "pending_rows", "label": "На уточнении", "field": "pending_rows"},
                    ],
                    rows=diagnostics["teacher_balance_rows"],
                    row_key="teacher_id",
                ).classes("panel-card w-full")

            ui.label(tr("editor.subject_summary")).classes("text-lg font-semibold mt-4")
            ui.table(
                columns=[
                    {"name": "subject", "label": tr("common.subject"), "field": "subject"},
                    {"name": "expected_pairs", "label": tr("editor.subject_column_expected"), "field": "expected_pairs"},
                    {"name": "placed_pairs", "label": tr("editor.subject_column_placed"), "field": "placed_pairs"},
                    {"name": "missing_pairs", "label": tr("editor.subject_column_missing"), "field": "missing_pairs"},
                    {"name": "status", "label": tr("editor.subject_column_status"), "field": "status"},
                    {"name": "reason", "label": tr("editor.subject_column_reason"), "field": "reason"},
                ],
                rows=diagnostics["subject_rows"],
                row_key="subject",
            ).classes("panel-card w-full")

        def open_edit_dialog(entry_id: int) -> None:
            with session_scope() as session:
                entry = session.get(ScheduleEntry, entry_id)
                group = session.get(Group, entry.group_id)
                teachers_local = load_teachers(session)
                rooms_local = session.exec(select(Room).order_by(Room.code)).all()
                slot_labels = load_online_slot_labels(session)
                online_slots = [slot for slot in load_online_slots(session) if slot.is_active]
                slot_day_map = {slot.id or 0: slot.day_of_week for slot in online_slots}
                schedule = session.get(Schedule, entry.schedule_id)
                manual_loads = session.exec(
                    select(CurriculumLoad).where(
                        CurriculumLoad.group_id == entry.group_id,
                        CurriculumLoad.semester == schedule.semester,
                    )
                ).all()
                weekly_loads = session.exec(
                    select(WeeklyLoad).where(
                        WeeklyLoad.group_id == entry.group_id,
                        WeeklyLoad.semester == schedule.semester,
                        WeeklyLoad.is_active.is_(True),
                    )
                ).all()
                subject_ids = {load.subject_id for load in manual_loads} | {load.subject_id for load in weekly_loads} | {entry.subject_id}
                subjects_local = [session.get(Subject, subject_id) for subject_id in sorted(subject_ids)]
            subject_options = {subject.id or 0: subject.name for subject in subjects_local if subject is not None}
            room_options = {0: tr("editor.remove_room"), **selection_options(rooms_local, "code")}
            with ui.dialog() as dialog, ui.card().classes("panel-card p-4 min-w-[520px]"):
                ui.label(tr("editor.edit_entry")).classes("text-lg font-semibold")
                subject_id = ui.select(subject_options, value=entry.subject_id, label=tr("editor.change_subject"))
                lesson_mode = ui.select(lesson_mode_options(), value=entry.lesson_mode, label=tr("editor.change_lesson_mode"))
                day_of_week = ui.select({day: day_label(day, lang=LANG) for day in DAYS}, value=entry.day_of_week, label=tr("editor.change_day"))
                pair_number = ui.select(
                    {
                        number: f"{pair_label(number, lang=LANG)} | {pair_time_range(number, lang=LANG)}"
                        for number in allowed_pairs_for_shift(group.shift if group else entry.shift)
                    },
                    value=entry.pair_number,
                    label=tr("editor.change_pair"),
                )
                online_slot_number = ui.select(
                    {
                        slot.id or 0: f"{slot.label} | {day_label(slot.day_of_week, lang=LANG)} | {slot.start_time}-{slot.end_time}"
                        for slot in online_slots
                    },
                    value=entry.online_slot_number or 1,
                    label=tr("editor.change_online_slot"),
                )
                teacher_id = ui.select(selection_options(teachers_local, "full_name"), value=entry.teacher_id, label=tr("editor.change_teacher"))
                room_id = ui.select(room_options, value=entry.room_id or 0, label=tr("editor.change_room"))
                delivery_mode = ui.select(regular_delivery_options(), value=entry.delivery_mode if entry.delivery_mode != "online" else "offline", label=tr("editor.change_mode"))
                locked = ui.switch(tr("editor.lock"), value=entry.locked)
                rename_teacher = ui.input(tr("editor.rename_current_teacher"))
                reassign_teacher = ui.select(selection_options(teachers_local, "full_name"), label=tr("editor.reassign_teacher"))
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
                            online_slot_days=slot_day_map,
                            teacher_id=teacher_id.value,
                            room_id=room_id.value,
                            delivery_mode=delivery_mode.value,
                            locked=bool(locked.value),
                            rename_teacher_to=rename_teacher.value or None,
                            reassign_teacher_id=reassign_teacher.value or None,
                        ),
                        dialog,
                    ),
                ).props("color=amber-8")
            dialog.open()

        def _entry_payload(
            subject_id: int | None,
            lesson_mode: str,
            day_of_week: int | None,
            pair_number: int | None,
            online_slot_number: int | None,
            online_slot_days: dict[int, int] | None,
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
                    "day_of_week": (online_slot_days or {}).get(slot_number, day_of_week or 3),
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

        def _save_entry(entry_id: int, payload: dict, dialog) -> None:
            try:
                with session_scope() as session:
                    service.update_entry(session, entry_id, payload)
            except ValueError as exc:
                ui.notify(tr("editor.save_failed", message=str(exc)), color="negative", multi_line=True)
                return
            dialog.close()
            ui.notify(tr("editor.entry_updated"), color="positive")
            render_grid.refresh()
            render_feedback.refresh()

        def _switch_scope(group_id: int | None, semester: int, result_select_widget, grid_refreshable, feedback_refreshable, state_ref: dict) -> None:
            state_ref["group_id"] = group_id
            state_ref["semester"] = semester
            options = result_options(group_id, semester)
            result_select_widget.options = options
            state_ref["schedule_id"] = next(iter(options.keys()), None)
            result_select_widget.value = state_ref["schedule_id"]
            grid_refreshable.refresh()
            feedback_refreshable.refresh()

        with ui.row().classes("gap-3 w-full"):
            result_select = ui.select(
                result_options(state["group_id"], state["semester"]),
                value=state["schedule_id"],
                label=tr("editor.result_selector"),
                on_change=lambda event: (
                    state.update({"schedule_id": event.value}),
                    render_grid.refresh(),
                    render_feedback.refresh(),
                ),
            ).classes("w-[28rem]")
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
                on_change=lambda event: _switch_scope(event.value, state["semester"], result_select, render_grid, render_feedback, state),
            )
            ui.select(
                {3: f"{tr('common.semester')} 3", 4: f"{tr('common.semester')} 4"},
                value=state["semester"],
                label=tr("common.semester"),
                on_change=lambda event: _switch_scope(state["group_id"], event.value, result_select, render_grid, render_feedback, state),
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

        render_grid()
        render_feedback()


@ui.page("/editor")
def editor_page() -> None:
    _render_editor_page()


@ui.page("/editor/{result_id}")
def editor_page_result(result_id: int) -> None:
    _render_editor_page(result_id)


@ui.page("/conflicts")
def conflicts_page() -> None:
    with page_shell(tr("page.conflicts")):
        with session_scope() as session:
            groups = load_groups(session)
            jobs = service.list_generation_jobs(session, limit=40)
        group_map = {group.id: group for group in groups}
        result_options = {
            job.result_schedule_id or 0: (
                f"#{job.id} | {(group_map.get(job.group_id).code if job.group_id in group_map else job.group_id)} | "
                f"{tr('common.semester')} {job.semester}"
            )
            for job in jobs
            if job.status == "completed" and job.result_schedule_id
        }
        if not result_options:
            result_options = {}
        state = {"schedule_id": next(iter(result_options.keys()), None), "group_id": 0}
        explanation_state = {"message": ""}

        with ui.dialog() as explanation_dialog, ui.card().classes("panel-card p-5 min-w-[760px] max-w-[960px]"):
            ui.label(tr("conflicts.ai_title")).classes("text-lg font-semibold")
            explanation_label = ui.label().classes("modal-text text-sm")
            with ui.row().classes("justify-end w-full mt-4"):
                ui.button(tr("conflicts.ai_close"), on_click=explanation_dialog.close).props("outline color=dark")

        @ui.refreshable
        def render_conflicts() -> None:
            if not state["schedule_id"]:
                return
            with session_scope() as session:
                diagnostics = service.result_diagnostics(session, state["schedule_id"], state["group_id"] or None)
                related_conflict_ids = [
                    conflict.id
                    for conflict in diagnostics["hard_conflicts"] + diagnostics["unscheduled_conflicts"]
                    if conflict.id is not None
                ]
                suggestions = session.exec(
                    select(Suggestion).where(
                        Suggestion.conflict_id.in_(related_conflict_ids)
                    )
                ).all()
                global_unresolved = service.unresolved_weekly_rows(session)
                global_balance = service.teacher_balance_report(session)
            ui.label(tr("conflicts.current_result")).classes("text-lg font-semibold")
            current_conflicts = diagnostics["hard_conflicts"] + diagnostics["unscheduled_conflicts"]
            if not current_conflicts:
                ui.label(tr("conflicts.none")).classes("text-sm text-[#6b7280]")
            else:
                for conflict in current_conflicts:
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
                        ).props("outline color=dark").classes("mt-2")

            ui.label(tr("conflicts.global_diagnostics")).classes("text-lg font-semibold mt-4")
            with ui.row().classes("w-full gap-4 items-start"):
                with ui.card().classes("panel-card p-4 grow"):
                    ui.label(tr("conflicts.global_unresolved")).classes("font-semibold")
                    if not global_unresolved:
                        ui.label(tr("curriculum.unresolved_empty")).classes("text-sm text-[#6b7280]")
                    else:
                        for row in global_unresolved[:20]:
                            label = tr(f"curriculum.assignment_{row.assignment_state}") if row.assignment_state in {"fixed", "multi_teacher", "multi_teacher_ambiguous", "vacancy", "unresolved_manual_review", "candidate_pool"} else row.assignment_state
                            ui.label(f"{row.group_id} | {row.semester} | {label} | {row.raw_teacher_names or '—'}").classes("text-sm")
                with ui.card().classes("panel-card p-4 grow"):
                    ui.label(tr("conflicts.global_balance")).classes("font-semibold")
                    if not global_balance:
                        ui.label(tr("curriculum.teacher_balance_empty")).classes("text-sm text-[#6b7280]")
                    else:
                        ui.table(
                            columns=[
                                {"name": "teacher_name", "label": tr("common.teacher"), "field": "teacher_name"},
                                {"name": "semester_3_pairs", "label": "Семестр 3", "field": "semester_3_pairs"},
                                {"name": "semester_4_pairs", "label": "Семестр 4", "field": "semester_4_pairs"},
                                {"name": "normalized_balance_score", "label": "Отклонение", "field": "normalized_balance_score"},
                            ],
                            rows=global_balance,
                            row_key="teacher_id",
                        ).classes("w-full")

        def _show_explanation(conflict_id: int) -> None:
            with session_scope() as session:
                message = service.explain_conflict(session, conflict_id)
            explanation_state["message"] = message
            explanation_label.set_text(explanation_state["message"])
            explanation_dialog.open()

        ui.select(
            result_options,
            value=state["schedule_id"],
            label=tr("editor.result_selector"),
            on_change=lambda event: (state.update({"schedule_id": event.value}), render_conflicts.refresh()),
        )
        ui.select(
            {0: tr("common.all"), **selection_options(groups)},
            value=state["group_id"],
            label=tr("common.group"),
            on_change=lambda event: (state.update({"group_id": event.value}), render_conflicts.refresh()),
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
            groups = load_groups(session)
            subjects = load_subjects(session)
            online_slots = session.exec(select(OnlineSlot).order_by(OnlineSlot.order_index, OnlineSlot.id)).all()
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

        ui.label(tr("settings.online_policy")).classes("text-xl font-semibold")
        with ui.card().classes("panel-card p-4 w-full"):
            ui.label(tr("settings.ai_provider")).classes("text-lg font-semibold")
            provider = ui.select({"dummy": "Без ИИ", "gemini": "Gemini"}, value=current_settings.get("ai_provider", "dummy"), label=tr("settings.ai_provider")).classes("w-80")
            gemini_key = ui.input(tr("settings.gemini_key"), password=True, password_toggle_button=True, value=current_settings.get("gemini_api_key", "")).classes("w-full")
            ui.label(tr("settings.ai_note")).classes("text-sm text-[#6b7280]")

            def _save_ai_settings() -> None:
                with session_scope() as session:
                    for key, value in {"ai_provider": provider.value, "gemini_api_key": gemini_key.value, "ui_language": "ru"}.items():
                        item = session.exec(select(AppSetting).where(AppSetting.key == key)).first()
                        if item is None:
                            item = AppSetting(key=key, value=value)
                        else:
                            item.value = value
                        session.add(item)
                    session.commit()
                ui.notify(tr("settings.saved"), color="positive")

            ui.button(tr("settings.save_settings"), on_click=_save_ai_settings).props("color=amber-8")

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

            @ui.refreshable
            def render_online_slots() -> None:
                with session_scope() as session:
                    rows = session.exec(select(OnlineSlot).order_by(OnlineSlot.order_index, OnlineSlot.id)).all()
                if not rows:
                    ui.label(tr("settings.online_slots_empty")).classes("text-sm text-[#6b7280]")
                for slot in rows:
                    with ui.card().classes("panel-card p-3 w-full mt-2"):
                        with ui.row().classes("form-grid"):
                            label_input = ui.input(tr("settings.slot_label"), value=slot.label)
                            day_input = ui.select({3: tr("day.wednesday"), 4: tr("day.thursday"), 5: tr("day.friday")}, value=slot.day_of_week, label=tr("editor.change_day"))
                            start_input = ui.input(tr("settings.slot_start"), value=slot.start_time)
                            end_input = ui.input(tr("settings.slot_end"), value=slot.end_time)
                            order_input = ui.number(tr("settings.slot_order"), value=slot.order_index, min=1, precision=0)
                            active_input = ui.switch(tr("settings.slot_active"), value=slot.is_active)
                        ui.button(
                            tr("common.save"),
                            on_click=lambda s=slot, li=label_input, di=day_input, sti=start_input, eni=end_input, oi=order_input, ai=active_input: _save_online_slot(
                                s.id or 0,
                                str(li.value or "").strip() or tr("online_slot.label", slot=s.id or 1),
                                int(di.value or 3),
                                str(sti.value or "").strip(),
                                str(eni.value or "").strip(),
                                bool(ai.value),
                                int(oi.value or 1),
                            ),
                        ).props("outline color=dark")

            def _save_online_slot(slot_id: int, label: str, day_of_week: int, start_time: str, end_time: str, is_active: bool, order_index: int) -> None:
                with session_scope() as session:
                    service.upsert_online_slot(
                        session,
                        slot_id,
                        label=label,
                        day_of_week=day_of_week,
                        start_time=start_time,
                        end_time=end_time,
                        is_active=is_active,
                        order_index=order_index,
                    )
                ui.notify(tr("settings.saved"), color="positive")
                render_online_slots.refresh()

            def _create_online_slot() -> None:
                with session_scope() as session:
                    rows = session.exec(select(OnlineSlot).order_by(OnlineSlot.order_index, OnlineSlot.id)).all()
                    next_index = (max((row.order_index for row in rows), default=0) or 0) + 1
                    service.upsert_online_slot(
                        session,
                        None,
                        label=tr("online_slot.label", slot=next_index),
                        day_of_week=5,
                        start_time="18:10",
                        end_time="19:30",
                        is_active=True,
                        order_index=next_index,
                    )
                ui.notify(tr("settings.online_slot_created"), color="positive")
                render_online_slots.refresh()

            ui.button(tr("settings.add_online_slot"), on_click=_create_online_slot).props("color=amber-8")
            render_online_slots()

        @ui.refreshable
        def render_subject_policy() -> None:
            with session_scope() as session:
                rows = load_subjects(session)
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
