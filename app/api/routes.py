from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from app.db.session import get_session
from app.models import AcademicPeriod, AppSetting, Conflict, CurriculumLoad, GenerationJob, Group, OnlineSlot, Room, Schedule, ScheduleEntry, Subject, Suggestion, Teacher, WeeklyLoad
from app.schemas.api import CalendarPeriodUpdate, EntryUpdate, GenerateRequest, SettingUpdate, TeacherCreate, TeacherRename
from app.services.timetable_service import TimetableService


router = APIRouter()
service = TimetableService()


def as_json(items):
    if isinstance(items, list):
        return [item.model_dump(mode="json") for item in items]
    return items.model_dump(mode="json")


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/groups")
def list_groups(session: Session = Depends(get_session)):
    return as_json(session.exec(select(Group).order_by(Group.code)).all())


@router.get("/teachers")
def list_teachers(session: Session = Depends(get_session)):
    return as_json(session.exec(select(Teacher).order_by(Teacher.full_name)).all())


@router.post("/teachers")
def create_teacher(payload: TeacherCreate, session: Session = Depends(get_session)):
    teacher = service.create_teacher(
        session,
        full_name=payload.full_name,
        short_name=payload.short_name,
        home_department_id=payload.home_department_id,
        max_weekly_pairs=payload.max_weekly_pairs,
    )
    return as_json(teacher)


@router.patch("/teachers/{teacher_id}/rename")
def rename_teacher(teacher_id: int, payload: TeacherRename, session: Session = Depends(get_session)):
    teacher = service.rename_teacher(session, teacher_id, payload.name)
    return as_json(teacher)


@router.get("/subjects")
def list_subjects(session: Session = Depends(get_session)):
    return as_json(session.exec(select(Subject).order_by(Subject.name)).all())


@router.get("/rooms")
def list_rooms(session: Session = Depends(get_session)):
    return as_json(session.exec(select(Room).order_by(Room.code)).all())


@router.get("/calendar")
def list_calendar(group_id: int | None = None, semester: int | None = None, session: Session = Depends(get_session)):
    query = select(AcademicPeriod).order_by(AcademicPeriod.group_id, AcademicPeriod.week_number)
    if group_id:
        query = query.where(AcademicPeriod.group_id == group_id)
    if semester:
        query = query.where(AcademicPeriod.semester == semester)
    return as_json(session.exec(query).all())


@router.put("/calendar/{period_id}")
def update_calendar(period_id: int, payload: CalendarPeriodUpdate, session: Session = Depends(get_session)):
    period = session.get(AcademicPeriod, period_id)
    if period is None:
        raise HTTPException(404, "Учебный период не найден.")
    period.period_type = payload.period_type
    period.is_schedulable = payload.is_schedulable
    session.add(period)
    session.commit()
    session.refresh(period)
    return as_json(period)


@router.get("/curriculum-loads")
def list_curriculum_loads(group_id: int | None = None, semester: int | None = None, session: Session = Depends(get_session)):
    query = select(CurriculumLoad).order_by(CurriculumLoad.group_id, CurriculumLoad.semester)
    if group_id:
        query = query.where(CurriculumLoad.group_id == group_id)
    if semester:
        query = query.where(CurriculumLoad.semester == semester)
    return as_json(session.exec(query).all())


@router.get("/weekly-loads")
def list_weekly_loads(group_id: int | None = None, semester: int | None = None, session: Session = Depends(get_session)):
    query = select(WeeklyLoad).where(WeeklyLoad.is_active.is_(True)).order_by(WeeklyLoad.group_id, WeeklyLoad.semester, WeeklyLoad.subject_id)
    if group_id:
        query = query.where(WeeklyLoad.group_id == group_id)
    if semester:
        query = query.where(WeeklyLoad.semester == semester)
    return as_json(session.exec(query).all())


@router.get("/weekly-loads/unresolved")
def list_unresolved_weekly_loads(semester: int | None = None, session: Session = Depends(get_session)):
    query = select(WeeklyLoad).where(
        WeeklyLoad.is_active.is_(True),
        WeeklyLoad.assignment_state.in_(["vacancy", "candidate_pool", "multi_teacher", "unresolved_manual_review"]),
    ).order_by(WeeklyLoad.group_id, WeeklyLoad.subject_id)
    if semester:
        query = query.where(WeeklyLoad.semester == semester)
    return as_json(session.exec(query).all())


@router.get("/online-slots")
def list_online_slots(session: Session = Depends(get_session)):
    return as_json(session.exec(select(OnlineSlot).order_by(OnlineSlot.order_index, OnlineSlot.id)).all())


@router.get("/schedules")
def list_schedules(session: Session = Depends(get_session)):
    return as_json(session.exec(select(Schedule).order_by(Schedule.created_at.desc())).all())


@router.get("/generation-jobs")
def list_generation_jobs(group_id: int | None = None, semester: int | None = None, session: Session = Depends(get_session)):
    return as_json(service.list_generation_jobs(session, group_id=group_id, semester=semester))


@router.get("/generation-jobs/{job_id}")
def get_generation_job(job_id: int, session: Session = Depends(get_session)):
    job = service.get_generation_job(session, job_id)
    if job is None:
        raise HTTPException(404, "Запуск генерации не найден.")
    return as_json(job)


@router.post("/schedules/generate")
def generate_schedule(payload: GenerateRequest, session: Session = Depends(get_session)):
    group_id = payload.group_id
    if group_id is None and payload.group_codes:
        group = session.exec(select(Group).where(Group.code == payload.group_codes[0])).first()
        group_id = group.id if group else None
    if group_id is None:
        raise HTTPException(400, "Нужно выбрать группу для генерации.")
    job = service.create_generation_job(
        session,
        group_id=group_id,
        semester=payload.semester,
        requested_name=payload.name or "",
        generation_mode=payload.generation_mode,
        include_facultatives=payload.include_facultatives,
        enable_online=payload.enable_online,
        source_scope=payload.source_scope,
    )
    job = service.run_generation_job(job.id or 0)
    if job.status != "completed" or not job.result_schedule_id:
        raise HTTPException(400, job.summary_message or "Расписание для выбранной группы не было построено.")
    schedule = session.get(Schedule, job.result_schedule_id)
    data = as_json(schedule)
    data["generation_job_id"] = job.id
    data["generation_status"] = job.status
    data["generation_summary"] = job.summary_message
    return data


@router.get("/schedules/{schedule_id}/entries")
def list_schedule_entries(
    schedule_id: int,
    group_id: int | None = None,
    teacher_id: int | None = None,
    session: Session = Depends(get_session),
):
    query = select(ScheduleEntry).where(ScheduleEntry.schedule_id == schedule_id)
    if group_id:
        query = query.where(ScheduleEntry.group_id == group_id)
    if teacher_id:
        query = query.where(ScheduleEntry.teacher_id == teacher_id)
    return as_json(session.exec(query).all())


@router.get("/schedules/{schedule_id}/conflicts")
def list_conflicts(schedule_id: int, session: Session = Depends(get_session)):
    conflicts = session.exec(select(Conflict).where(Conflict.schedule_id == schedule_id)).all()
    suggestions = session.exec(
        select(Suggestion).where(
            Suggestion.conflict_id.in_([conflict.id for conflict in conflicts if conflict.id is not None])
        )
    ).all()
    return {
        "conflicts": as_json(conflicts),
        "suggestions": as_json(suggestions),
    }


@router.get("/schedules/{schedule_id}/diagnostics")
def schedule_diagnostics(schedule_id: int, group_id: int | None = None, session: Session = Depends(get_session)):
    data = service.result_diagnostics(session, schedule_id, group_id=group_id)
    return {
        "summary": data["summary"],
        "subject_rows": data["subject_rows"],
        "hard_conflicts": as_json(data["hard_conflicts"]),
        "unscheduled_conflicts": as_json(data["unscheduled_conflicts"]),
        "warnings": data["warnings"],
        "normalization_issues": data["normalization_issues"],
        "teacher_balance_rows": data["teacher_balance_rows"],
    }


@router.put("/entries/{entry_id}")
def update_entry(entry_id: int, payload: EntryUpdate, session: Session = Depends(get_session)):
    try:
        entry = service.update_entry(session, entry_id, payload.model_dump(exclude_none=True))
    except ValueError as exc:
        status_code = 404 if "не найден" in str(exc).lower() else 400
        raise HTTPException(status_code, str(exc)) from exc
    return as_json(entry)


@router.post("/schedules/{schedule_id}/export/{export_format}")
def export_schedule(schedule_id: int, export_format: str, session: Session = Depends(get_session)):
    try:
        file_path = service.export_schedule(session, schedule_id, export_format)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"path": str(file_path), "name": Path(file_path).name, "url": f"/exports/{Path(file_path).name}"}


@router.get("/conflicts/{conflict_id}/explanation")
def explain_conflict(conflict_id: int, session: Session = Depends(get_session)):
    return {"message": service.explain_conflict(session, conflict_id)}


@router.get("/settings")
def get_settings(session: Session = Depends(get_session)):
    items = session.exec(select(AppSetting)).all()
    return {item.key: item.value for item in items}


@router.put("/settings")
def update_settings(payload: SettingUpdate, session: Session = Depends(get_session)):
    for key, value in payload.model_dump().items():
        item = session.exec(select(AppSetting).where(AppSetting.key == key)).first()
        if item is None:
            item = AppSetting(key=key, value=str(value))
        else:
            item.value = str(value)
        session.add(item)
    session.commit()
    return {"status": "saved", "message": "Настройки сохранены."}
