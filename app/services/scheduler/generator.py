from __future__ import annotations

from sqlmodel import Session

from app.core.timetable import DELIVERY_ONLINE, LESSON_MODE_ONLINE, LESSON_MODE_REGULAR, SLOT_CATEGORY_ONLINE_EXTRA, SLOT_CATEGORY_REGULAR, apply_timeslot_to_entry
from app.models import Schedule, ScheduleEntry
from app.services.online_slots import OnlineSlotService
from app.services.scheduler.models import FeasibilityReport, NormalizedLoadRow, PlacementRequest, PlannerResult
from app.services.scheduler.normalizer import WorkloadNormalizer
from app.services.scheduler.planner import FeasibilityAnalyzer, ScopedSchedulePlanner


class HybridScheduleGenerator:
    def __init__(self) -> None:
        self.normalizer = WorkloadNormalizer()
        self.feasibility_analyzer = FeasibilityAnalyzer()
        self.planner = ScopedSchedulePlanner()
        self.online_slot_service = OnlineSlotService()

    def normalize_scope(
        self,
        session: Session,
        *,
        semester: int,
        group_codes: list[str] | None = None,
        include_facultatives: bool = False,
    ) -> tuple[list, list[NormalizedLoadRow], list[PlacementRequest]]:
        return self.normalizer.normalize_scope(
            session,
            semester=semester,
            group_codes=group_codes,
            include_facultatives=include_facultatives,
        )

    def feasibility(
        self,
        session: Session,
        *,
        semester: int,
        group_codes: list[str] | None = None,
        include_facultatives: bool = False,
        enable_online: bool = True,
    ) -> FeasibilityReport:
        groups, rows, requests = self.normalize_scope(
            session,
            semester=semester,
            group_codes=group_codes,
            include_facultatives=include_facultatives,
        )
        return self.feasibility_analyzer.analyze(
            session,
            groups=groups,
            rows=rows,
            requests=requests,
            enable_online=enable_online,
        )

    def generate(
        self,
        session: Session,
        semester: int,
        group_codes: list[str] | None = None,
        schedule_name: str | None = None,
        include_facultatives: bool = False,
        enable_online: bool = True,
        generation_job_id: int | None = None,
    ) -> Schedule:
        schedules = self.generate_run(
            session,
            semester=semester,
            group_codes=group_codes,
            schedule_name=schedule_name,
            include_facultatives=include_facultatives,
            enable_online=enable_online,
            generation_job_id=generation_job_id,
        )
        if not schedules:
            raise ValueError("Расписание для выбранной группы не было построено.")
        return schedules[0]

    def generate_run(
        self,
        session: Session,
        *,
        semester: int,
        group_codes: list[str] | None = None,
        schedule_name: str | None = None,
        include_facultatives: bool = False,
        enable_online: bool = True,
        generation_job_id: int | None = None,
    ) -> list[Schedule]:
        groups, rows, requests = self.normalize_scope(
            session,
            semester=semester,
            group_codes=group_codes,
            include_facultatives=include_facultatives,
        )
        if not groups:
            return []

        planner_result = self.planner.plan(
            session,
            semester=semester,
            groups=groups,
            requests=requests,
            enable_online=enable_online,
        )
        schedule_by_group: dict[int, Schedule] = {}
        for group in groups:
            schedule = Schedule(
                name=(schedule_name or f"Расписание семестра {semester}") if len(groups) == 1 else f"{schedule_name or f'Расписание семестра {semester}'} | {group.code}",
                semester=semester,
                group_scope=group.code,
                generation_job_id=generation_job_id,
            )
            session.add(schedule)
            session.commit()
            session.refresh(schedule)
            schedule_by_group[group.id or 0] = schedule

        ordered_placements = sorted(
            planner_result.placements,
            key=lambda item: (
                1 if item.request.lesson_mode == LESSON_MODE_ONLINE else 0,
                item.day_of_week,
                item.pair_number or 99,
                item.online_slot_id or 99,
                item.request.subject_id,
            ),
        )
        entries = [
            self._make_entry(session, schedule_by_group[placement.request.group_id].id or 0, placement)
            for placement in ordered_placements
            if placement.request.group_id in schedule_by_group
        ]
        for entry in entries:
            session.add(entry)
        session.commit()
        return [schedule_by_group[group.id or 0] for group in groups if (group.id or 0) in schedule_by_group]

    def _make_entry(self, session: Session, schedule_id: int, placement) -> ScheduleEntry:
        request = placement.request
        entry = ScheduleEntry(
            schedule_id=schedule_id,
            group_id=request.group_id,
            subject_id=request.subject_id,
            teacher_id=placement.teacher_id or 0,
            room_id=placement.room_id,
            day_of_week=placement.day_of_week,
            pair_number=placement.pair_number,
            online_slot_number=placement.online_slot_id,
            lesson_mode=request.lesson_mode,
            slot_category=SLOT_CATEGORY_ONLINE_EXTRA if request.lesson_mode == LESSON_MODE_ONLINE else SLOT_CATEGORY_REGULAR,
            shift=request.shift,
            delivery_mode=DELIVERY_ONLINE if request.lesson_mode == LESSON_MODE_ONLINE else request.delivery_mode,
            subgroup_code=request.subgroup_code,
            week_scope=request.week_scope,
            source_load_key=request.load_key,
            source_kind=request.source_kind,
            locked=False,
        )
        apply_timeslot_to_entry(entry)
        if entry.lesson_mode == LESSON_MODE_ONLINE:
            self.online_slot_service.apply_to_entry(session, entry)
        return entry


GreedyScheduleGenerator = HybridScheduleGenerator
