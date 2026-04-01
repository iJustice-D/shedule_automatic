from __future__ import annotations

PERIOD_TYPES = (
    "study",
    "industrial_practice",
    "study_practice",
    "teacher_practice",
    "exam_week",
    "state_exam",
    "final_attestation",
    "vacation",
    "holiday",
    "summer_training",
    "diploma_writing",
    "diploma_defense",
    "orientation_practice",
)

HARD_CONFLICTS = {
    "teacher_double_booked",
    "group_double_booked",
    "room_double_booked",
    "teacher_not_allowed",
    "blocked_period",
    "shift_violation",
    "room_required",
    "unscheduled_load",
    "online_slot_violation",
    "online_day_violation",
    "regular_in_online_slot",
}

SOFT_CONFLICTS = {
    "too_many_lessons_in_day",
    "same_subject_stack",
    "online_target_not_met",
    "online_not_allowed",
    "too_many_online_lessons_in_day",
}
