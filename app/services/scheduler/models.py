from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class NormalizedLoadRow:
    load_key: str
    source_kind: str
    source_id: int
    group_id: int
    group_code: str
    semester: int
    subject_id: int
    subject_name: str
    load_type: str
    subgroup_code: str | None
    assignment_state: str
    teacher_candidates: list[int]
    fixed_teacher_id: int | None
    weekly_pairs: float
    total_pairs: int
    study_weeks: list[int]
    can_be_online: bool
    default_delivery_mode: str
    requires_special_room: bool
    source_priority: int
    raw_teacher_names: str = ""
    note: str = ""
    excluded_status: str = ""
    excluded_reason: str = ""
    normalization_issue: str = ""


@dataclass(slots=True)
class PlacementRequest:
    request_key: str
    load_key: str
    source_kind: str
    group_id: int
    group_code: str
    semester: int
    subject_id: int
    subject_name: str
    subgroup_code: str | None
    assignment_state: str
    teacher_candidates: list[int]
    fixed_teacher_id: int | None
    room_candidates: list[int | None]
    shift: str
    week_scope: str
    lesson_mode: str
    delivery_mode: str
    requires_special_room: bool
    can_be_online: bool
    source_priority: int
    total_pairs: int
    weekly_weight: float
    note: str = ""


@dataclass(slots=True)
class FeasibilityItem:
    load_key: str
    subject_id: int
    subject_name: str
    expected_pairs: int
    assignment_state: str
    issue_type: str
    message: str


@dataclass(slots=True)
class FeasibilityReport:
    regular_capacity: int
    online_capacity: int
    required_regular_requests: int
    requested_online_target: int
    unresolved_rows: int
    excluded_rows: int
    issues: list[FeasibilityItem] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass(slots=True)
class PlannedPlacement:
    request: PlacementRequest
    day_of_week: int
    pair_number: int
    online_slot_id: int | None
    teacher_id: int | None
    room_id: int | None


@dataclass(slots=True)
class PlannerResult:
    placements: list[PlannedPlacement] = field(default_factory=list)
    unplaced: list[tuple[PlacementRequest, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass(slots=True)
class SubjectSummaryRow:
    group_id: int
    subject_id: int
    subject: str
    expected_pairs: int
    placed_pairs: int
    missing_pairs: int
    status: str
    reason: str
    assignment_state: str
    subgroup_code: str | None = None


@dataclass(slots=True)
class DiagnosticsBundle:
    summary: dict[str, object]
    subject_rows: list[SubjectSummaryRow]
    normalization_issues: list[dict[str, object]]
    warnings: list[dict[str, object]]
    hard_conflicts: list[object]
    unscheduled_conflicts: list[object]
    teacher_balance_rows: list[dict[str, object]]
