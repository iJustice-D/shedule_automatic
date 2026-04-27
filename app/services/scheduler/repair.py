from __future__ import annotations

from collections import defaultdict

from sqlmodel import Session, select

from app.core.timetable import apply_timeslot_to_entry
from app.models import Group, Schedule, ScheduleEntry, Subject
from app.services.online_slots import OnlineSlotService
from app.services.scheduler.normalizer import WorkloadNormalizer
from app.services.scheduler.planner import ScopedSchedulePlanner


class LocalRepairService:
    def __init__(self) -> None:
        self.normalizer = WorkloadNormalizer()
        self.planner = ScopedSchedulePlanner()
        self.online_slot_service = OnlineSlotService()

    def repair_schedule_scope(
        self,
        session: Session,
        *,
        schedule_id: int,
        semester: int,
        group_ids: list[int],
    ) -> int:
        if not group_ids:
            return 0
        groups = [session.get(Group, group_id) for group_id in group_ids]
        groups = [group for group in groups if group is not None]
        if not groups:
            return 0
        _, _, requests = self.normalizer.normalize_scope(
            session,
            semester=semester,
            group_codes=[group.code for group in groups],
            include_facultatives=False,
        )
        schedule = session.get(Schedule, schedule_id)
        related_schedule_ids = self._related_schedule_ids(session, schedule)
        schedule_entries = session.exec(
            select(ScheduleEntry).where(
                ScheduleEntry.schedule_id.in_(related_schedule_ids),
            )
        ).all()
        existing_placements, remaining_requests = self._bind_existing_entries(
            [entry for entry in schedule_entries if entry.group_id in group_ids],
            requests,
            background_entries=[entry for entry in schedule_entries if entry.group_id not in group_ids],
        )
        repaired_count = 0
        self.planner._semester = semester
        self.planner._online_slots = {slot.id or 0: slot for slot in self.online_slot_service.active_slots(session)}
        for request in remaining_requests:
            placement = self.planner._find_best_placement(existing_placements, request)
            if placement is None:
                continue
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
                slot_category="online_extra" if request.lesson_mode == "online" else "regular",
                shift=request.shift,
                delivery_mode=request.delivery_mode,
                subgroup_code=request.subgroup_code,
                week_scope=request.week_scope,
                source_load_key=request.load_key,
                source_kind=request.source_kind,
                locked=False,
            )
            apply_timeslot_to_entry(entry)
            if entry.lesson_mode == "online":
                self.online_slot_service.apply_to_entry(session, entry)
            session.add(entry)
            session.flush()
            existing_placements.append(placement)
            repaired_count += 1
        if repaired_count:
            session.commit()
        return repaired_count

    def _bind_existing_entries(self, entries: list[ScheduleEntry], requests, background_entries: list[ScheduleEntry] | None = None):
        requests_by_load: dict[str, list] = defaultdict(list)
        for request in requests:
            requests_by_load[request.load_key].append(request)
        existing_placements = []
        for entry in [*(background_entries or []), *entries]:
            source_requests = requests_by_load.get(entry.source_load_key or "", [])
            request = None
            if source_requests:
                request = source_requests.pop(0)
            if request is None:
                subject_name = str(entry.subject_id)
                request = next(
                    (item for item in requests if item.subject_id == entry.subject_id and item.group_id == entry.group_id),
                    None,
                )
            if request is None:
                continue
            existing_placements.append(
                self.planner._placement_from_candidate(
                    request,
                    entry.day_of_week,
                    entry.pair_number,
                    entry.online_slot_number,
                    entry.teacher_id,
                    entry.room_id,
                )
            )
        remaining_requests = []
        for items in requests_by_load.values():
            remaining_requests.extend(items)
        return existing_placements, remaining_requests

    @staticmethod
    def _related_schedule_ids(session: Session, schedule: Schedule | None) -> list[int]:
        if schedule is None or schedule.id is None:
            return []
        if schedule.generation_job_id:
            related = session.exec(select(Schedule.id).where(Schedule.generation_job_id == schedule.generation_job_id)).all()
            return [item for item in related if item is not None]
        return [schedule.id]
