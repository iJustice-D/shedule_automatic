from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class GenerateRequest(BaseModel):
    name: Optional[str] = None
    semester: int = Field(ge=3, le=4)
    group_id: Optional[int] = None
    group_codes: list[str] = Field(default_factory=list)
    generation_mode: str = "best_effort"
    include_facultatives: bool = False
    enable_online: bool = True
    source_scope: str = "normalized_weekly"


class EntryUpdate(BaseModel):
    day_of_week: Optional[int] = Field(default=None, ge=1, le=5)
    pair_number: Optional[int] = Field(default=None, ge=0, le=6)
    online_slot_number: Optional[int] = Field(default=None, ge=1, le=3)
    lesson_mode: Optional[str] = None
    subject_id: Optional[int] = None
    teacher_id: Optional[int] = None
    room_id: Optional[int] = None
    delivery_mode: Optional[str] = None
    locked: Optional[bool] = None
    rename_teacher_to: Optional[str] = None
    reassign_teacher_id: Optional[int] = None


class TeacherCreate(BaseModel):
    full_name: str
    short_name: Optional[str] = None
    home_department_id: int
    max_weekly_pairs: int = 20


class TeacherRename(BaseModel):
    name: str


class CalendarPeriodUpdate(BaseModel):
    period_type: str
    is_schedulable: bool


class SettingUpdate(BaseModel):
    ai_provider: str
    gemini_api_key: str = ""
    ui_language: str = "ru"
