from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Department(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    code: str = Field(index=True, unique=True)
    name: str


class Group(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    code: str = Field(index=True, unique=True)
    name: str
    home_department_id: int = Field(foreign_key="department.id")
    course: int
    year: int = 1
    semester: int
    student_count: int
    shift: str = "morning"


class Subject(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    code: str = Field(index=True, unique=True)
    name: str
    owner_department_id: int = Field(foreign_key="department.id")
    lesson_type: str
    requires_special_room: bool = False
    can_be_online: bool = False
    default_delivery_mode: str = "offline"


class Teacher(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    full_name: str
    short_name: str
    home_department_id: int = Field(foreign_key="department.id")
    editable_name: Optional[str] = None
    max_weekly_pairs: int = 20


class Room(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    code: str = Field(index=True, unique=True)
    name: str
    capacity: int = 30
    room_type: str = "standard"


class Timeslot(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    day_of_week: int = 0
    pair_number: int
    shift: str = "morning"
    start_time: str = ""
    end_time: str = ""
    label: str


class AcademicPeriod(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    group_id: int = Field(foreign_key="group.id", index=True)
    semester: int = Field(index=True)
    week_number: int = Field(index=True)
    period_type: str
    is_schedulable: bool = False


class CurriculumLoad(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    group_id: int = Field(foreign_key="group.id", index=True)
    subject_id: int = Field(foreign_key="subject.id", index=True)
    semester: int = Field(index=True)
    total_hours: int
    study_weeks: int
    hours_per_week: float
    pairs_per_week: float
    lesson_type: str
    delivery_mode: str = "offline"
    raw_total_hours: int = 0
    practice_hours: int = 0
    source_code: str = ""
    details_json: str = ""


class TeacherSubject(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    teacher_id: int = Field(foreign_key="teacher.id", index=True)
    subject_id: int = Field(foreign_key="subject.id", index=True)
    can_teach: bool = True
    priority: int = 1


class GroupSubjectTeacher(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    group_id: int = Field(foreign_key="group.id", index=True)
    subject_id: int = Field(foreign_key="subject.id", index=True)
    teacher_id: int = Field(foreign_key="teacher.id", index=True)
    fixed: bool = False


class Schedule(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    semester: int = Field(index=True)
    details_json: str = ""
    created_at: datetime = Field(default_factory=utcnow)


class ScheduleEntry(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    schedule_id: int = Field(foreign_key="schedule.id", index=True)
    group_id: int = Field(foreign_key="group.id", index=True)
    subject_id: int = Field(foreign_key="subject.id", index=True)
    teacher_id: int = Field(foreign_key="teacher.id", index=True)
    room_id: Optional[int] = Field(default=None, foreign_key="room.id", index=True)
    day_of_week: int
    pair_number: int
    online_slot_number: Optional[int] = None
    lesson_mode: str = "regular"
    slot_category: str = "regular"
    shift: str = ""
    start_time: str = ""
    end_time: str = ""
    delivery_mode: str = "offline"
    week_scope: str
    locked: bool = False


class Conflict(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    schedule_id: int = Field(foreign_key="schedule.id", index=True)
    type: str = Field(index=True)
    severity: str
    code: str
    message: str
    related_entry_ids: str = ""
    details_json: str = ""


class Suggestion(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    conflict_id: int = Field(foreign_key="conflict.id", index=True)
    action_type: str
    message: str
    rank: int
    payload_json: str = ""


class ChangeLog(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    schedule_id: int = Field(foreign_key="schedule.id", index=True)
    action_type: str
    before_json: str
    after_json: str
    created_at: datetime = Field(default_factory=utcnow)


class AppSetting(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    key: str = Field(index=True, unique=True)
    value: str = ""


class OnlinePolicy(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    course: Optional[int] = Field(default=None, index=True)
    group_id: Optional[int] = Field(default=None, foreign_key="group.id", index=True)
    subject_id: Optional[int] = Field(default=None, foreign_key="subject.id", index=True)
    target_online_lessons_per_week: int = 0
    allow_online: bool = True
    is_active: bool = True
    note: str = ""
