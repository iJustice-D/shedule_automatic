from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from sqlmodel import Session, select

from app.core.timetable import (
    DAYS,
    DELIVERY_OFFLINE,
    DELIVERY_ONLINE,
    LESSON_MODE_ONLINE,
    LESSON_MODE_REGULAR,
    ONLINE_ALLOWED_DAYS,
    SLOT_CATEGORY_ONLINE_EXTRA,
    SLOT_CATEGORY_REGULAR,
    allowed_pairs_for_shift,
    apply_timeslot_to_entry,
    online_slot_day,
    online_slot_numbers,
    room_required,
)
from app.core.week_scope import encode_week_scope, scopes_overlap, spread_weeks
from app.models import AcademicPeriod, CurriculumLoad, Group, GroupSubjectTeacher, Room, Schedule, ScheduleEntry, Subject, TeacherSubject
from app.services.online_policy import OnlinePolicyService


@dataclass(slots=True)
class SessionDemand:
    group_id: int
    subject_id: int
    teacher_candidates: list[int]
    room_candidates: list[int | None]
    week_scope: str
    shift: str
    delivery_mode: str = DELIVERY_OFFLINE
    lesson_mode: str = LESSON_MODE_REGULAR
    slot_category: str = SLOT_CATEGORY_REGULAR
    online_allowed: bool = False
    locked: bool = False
    metadata: dict[str, int | str] = field(default_factory=dict)


class GreedyScheduleGenerator:
    def __init__(self) -> None:
        self.online_policy_service = OnlinePolicyService()

    def generate(
        self,
        session: Session,
        semester: int,
        group_codes: list[str] | None = None,
        schedule_name: str | None = None,
    ) -> Schedule:
        groups_query = select(Group)
        if group_codes:
            groups_query = groups_query.where(Group.code.in_(group_codes))
        groups = session.exec(groups_query.order_by(Group.code)).all()
        schedule = Schedule(name=schedule_name or f"Расписание семестра {semester}", semester=semester)
        session.add(schedule)
        session.commit()
        session.refresh(schedule)

        demands: list[SessionDemand] = []
        for group in groups:
            study_weeks = self._get_study_weeks(session, group.id or 0, semester)
            if not study_weeks:
                continue
            loads = session.exec(
                select(CurriculumLoad).where(
                    CurriculumLoad.group_id == group.id,
                    CurriculumLoad.semester == semester,
                )
            ).all()
            for load in loads:
                demands.extend(self._build_demands(session, group, load, study_weeks))

        self._apply_online_targets(session, groups, demands)
        teacher_pressure = self._teacher_pressure(demands)
        regular_demands = sorted(
            [demand for demand in demands if demand.lesson_mode == LESSON_MODE_REGULAR],
            key=lambda item: self._priority_key(item, teacher_pressure),
        )
        online_demands = sorted(
            [demand for demand in demands if demand.lesson_mode == LESSON_MODE_ONLINE],
            key=lambda item: self._priority_key(item, teacher_pressure),
        )

        entries: list[ScheduleEntry] = []
        unscheduled_regular = self._place_phase(entries, regular_demands, schedule.id or 0)
        self._repair_phase(entries, unscheduled_regular, schedule.id or 0)
        unscheduled_online = self._place_phase(entries, online_demands, schedule.id or 0)
        self._repair_phase(entries, unscheduled_online, schedule.id or 0)

        for entry in entries:
            session.add(entry)
        session.commit()
        return schedule

    def _place_phase(
        self,
        entries: list[ScheduleEntry],
        demands: list[SessionDemand],
        schedule_id: int,
    ) -> list[SessionDemand]:
        unscheduled: list[SessionDemand] = []
        for demand in demands:
            placement = self._find_best_slot(entries, demand)
            if placement is None:
                unscheduled.append(demand)
                continue
            entries.append(self._make_entry(schedule_id, demand, placement))
        return unscheduled

    def _repair_phase(
        self,
        entries: list[ScheduleEntry],
        unscheduled: list[SessionDemand],
        schedule_id: int,
    ) -> None:
        pending = list(unscheduled)
        unscheduled.clear()
        for demand in pending:
            placement = self._find_best_slot(entries, demand)
            if placement is not None:
                entries.append(self._make_entry(schedule_id, demand, placement))
                continue
            if self._repair_single_blocker(entries, demand, schedule_id):
                continue
            unscheduled.append(demand)

    def _build_demands(
        self,
        session: Session,
        group: Group,
        load: CurriculumLoad,
        study_weeks: list[int],
    ) -> list[SessionDemand]:
        total_pairs = max(int(round(load.total_hours / 2)), 1)
        base_pairs = total_pairs // len(study_weeks)
        extra_pairs = total_pairs % len(study_weeks)
        subject = session.get(Subject, load.subject_id)
        online_allowed = self.online_policy_service.is_subject_allowed_online(session, group, subject) if subject else False
        teacher_candidates = self._teacher_candidates(session, group.id or 0, load.subject_id)
        room_candidates = self._room_candidates(session, load.subject_id)
        common_metadata = {
            "total_hours": load.total_hours,
            "study_weeks": len(study_weeks),
            "teacher_option_count": len(teacher_candidates),
            "room_option_count": len(room_candidates),
        }
        demands = [
            SessionDemand(
                group_id=group.id or 0,
                subject_id=load.subject_id,
                teacher_candidates=teacher_candidates,
                room_candidates=room_candidates,
                week_scope=encode_week_scope(study_weeks),
                shift=group.shift,
                delivery_mode=load.delivery_mode or DELIVERY_OFFLINE,
                online_allowed=online_allowed,
                metadata=common_metadata.copy(),
            )
            for _ in range(base_pairs)
        ]
        if extra_pairs:
            demands.append(
                SessionDemand(
                    group_id=group.id or 0,
                    subject_id=load.subject_id,
                    teacher_candidates=teacher_candidates,
                    room_candidates=room_candidates,
                    week_scope=encode_week_scope(spread_weeks(study_weeks, extra_pairs)),
                    shift=group.shift,
                    delivery_mode=load.delivery_mode or DELIVERY_OFFLINE,
                    online_allowed=online_allowed,
                    metadata=common_metadata.copy(),
                )
            )
        return demands

    def _apply_online_targets(self, session: Session, groups: list[Group], demands: list[SessionDemand]) -> None:
        group_map = {group.id: group for group in groups}
        by_group: dict[int, list[SessionDemand]] = {}
        for demand in demands:
            by_group.setdefault(demand.group_id, []).append(demand)
        for group_id, group_demands in by_group.items():
            group = group_map.get(group_id)
            if group is None:
                continue
            target = self.online_policy_service.get_target_for_group(session, group)
            if target <= 0:
                continue
            eligible = [demand for demand in group_demands if demand.online_allowed]
            if not eligible:
                continue
            chosen: list[SessionDemand] = []
            used_subjects: set[int] = set()
            for demand in eligible:
                if demand.subject_id in used_subjects:
                    continue
                chosen.append(demand)
                used_subjects.add(demand.subject_id)
                if len(chosen) >= target:
                    break
            if len(chosen) < target:
                for demand in eligible:
                    if demand in chosen:
                        continue
                    chosen.append(demand)
                    if len(chosen) >= target:
                        break
            for demand in chosen[:target]:
                demand.lesson_mode = LESSON_MODE_ONLINE
                demand.slot_category = SLOT_CATEGORY_ONLINE_EXTRA
                demand.delivery_mode = DELIVERY_ONLINE
                demand.room_candidates = [None]

    def _find_best_slot(
        self,
        entries: list[ScheduleEntry],
        demand: SessionDemand,
        origin: tuple[int, int, int] | None = None,
        forbidden_slots: set[tuple[int, int, int]] | None = None,
    ) -> tuple[int, int, int | None, int, int | None] | None:
        if not demand.teacher_candidates:
            return None
        if room_required(demand.delivery_mode) and not demand.room_candidates:
            return None
        best_score: tuple[int, int, int, int, int, int] | None = None
        for teacher_id in demand.teacher_candidates:
            for room_id in demand.room_candidates:
                for day_of_week, pair_number, online_slot_number in self._candidate_slots(demand):
                    slot_id = (day_of_week, pair_number, online_slot_number or 0)
                    if forbidden_slots and slot_id in forbidden_slots:
                        continue
                    if self._violates_hard_constraints(entries, demand, day_of_week, pair_number, online_slot_number, teacher_id, room_id):
                        continue
                    score = self._soft_score(
                        entries,
                        demand,
                        day_of_week,
                        pair_number,
                        online_slot_number,
                        teacher_id,
                        origin=origin,
                    )
                    candidate = (score, day_of_week, pair_number, online_slot_number or 0, teacher_id, room_id or 0)
                    if best_score is None or candidate < best_score:
                        best_score = candidate
        if best_score is None:
            return None
        _, day_of_week, pair_number, online_slot_number, teacher_id, room_id = best_score
        return day_of_week, pair_number, online_slot_number or None, teacher_id, room_id or None

    def _repair_single_blocker(
        self,
        entries: list[ScheduleEntry],
        demand: SessionDemand,
        schedule_id: int,
    ) -> bool:
        for teacher_id in demand.teacher_candidates:
            for room_id in demand.room_candidates:
                for day_of_week, pair_number, online_slot_number in self._candidate_slots(demand):
                    blockers = self._blocking_entries(entries, demand, day_of_week, pair_number, online_slot_number, teacher_id, room_id)
                    if len(blockers) != 1:
                        continue
                    blocker = blockers[0]
                    if blocker.locked:
                        continue
                    blocker_demand = self._demand_from_entry(blocker)
                    remaining = [entry for entry in entries if entry is not blocker]
                    alternate = self._find_best_slot(
                        remaining,
                        blocker_demand,
                        origin=(blocker.day_of_week, blocker.pair_number, blocker.online_slot_number or 0),
                        forbidden_slots={(day_of_week, pair_number, online_slot_number or 0)},
                    )
                    if alternate is None:
                        continue
                    alt_day, alt_pair, alt_online_slot, alt_teacher, alt_room = alternate
                    blocker.day_of_week = alt_day
                    blocker.pair_number = alt_pair
                    blocker.online_slot_number = alt_online_slot
                    blocker.teacher_id = alt_teacher
                    blocker.room_id = alt_room
                    apply_timeslot_to_entry(blocker)
                    if self._violates_hard_constraints(
                        remaining + [blocker],
                        demand,
                        day_of_week,
                        pair_number,
                        online_slot_number,
                        teacher_id,
                        room_id,
                    ):
                        continue
                    entries.append(
                        self._make_entry(
                            schedule_id,
                            demand,
                            (day_of_week, pair_number, online_slot_number, teacher_id, room_id),
                        )
                    )
                    return True
        return False

    def _candidate_slots(self, demand: SessionDemand) -> list[tuple[int, int, int | None]]:
        if demand.lesson_mode == LESSON_MODE_ONLINE:
            return [(online_slot_day(slot_number), 0, slot_number) for slot_number in online_slot_numbers()]
        return [
            (day_of_week, pair_number, None)
            for day_of_week in DAYS
            for pair_number in allowed_pairs_for_shift(demand.shift)
        ]

    def _blocking_entries(
        self,
        entries: list[ScheduleEntry],
        demand: SessionDemand,
        day_of_week: int,
        pair_number: int,
        online_slot_number: int | None,
        teacher_id: int,
        room_id: int | None,
    ) -> list[ScheduleEntry]:
        blockers: list[ScheduleEntry] = []
        for entry in entries:
            if entry.lesson_mode != demand.lesson_mode:
                continue
            if entry.day_of_week != day_of_week:
                continue
            if demand.lesson_mode == LESSON_MODE_REGULAR and entry.pair_number != pair_number:
                continue
            if demand.lesson_mode == LESSON_MODE_ONLINE and entry.online_slot_number != online_slot_number:
                continue
            if not scopes_overlap(entry.week_scope, demand.week_scope):
                continue
            if entry.group_id == demand.group_id or entry.teacher_id == teacher_id:
                blockers.append(entry)
                continue
            if demand.lesson_mode == LESSON_MODE_REGULAR and room_required(entry.delivery_mode) and room_required(demand.delivery_mode):
                if room_id is not None and entry.room_id == room_id:
                    blockers.append(entry)
        return blockers

    def _violates_hard_constraints(
        self,
        entries: list[ScheduleEntry],
        demand: SessionDemand,
        day_of_week: int,
        pair_number: int,
        online_slot_number: int | None,
        teacher_id: int,
        room_id: int | None,
    ) -> bool:
        return bool(self._blocking_entries(entries, demand, day_of_week, pair_number, online_slot_number, teacher_id, room_id))

    def _make_entry(
        self,
        schedule_id: int,
        demand: SessionDemand,
        placement: tuple[int, int, int | None, int, int | None],
    ) -> ScheduleEntry:
        day_of_week, pair_number, online_slot_number, teacher_id, room_id = placement
        entry = ScheduleEntry(
            schedule_id=schedule_id,
            group_id=demand.group_id,
            subject_id=demand.subject_id,
            teacher_id=teacher_id,
            room_id=room_id,
            day_of_week=day_of_week,
            pair_number=pair_number,
            online_slot_number=online_slot_number,
            lesson_mode=demand.lesson_mode,
            slot_category=demand.slot_category,
            week_scope=demand.week_scope,
            delivery_mode=demand.delivery_mode,
            locked=demand.locked,
        )
        apply_timeslot_to_entry(entry)
        return entry

    def _demand_from_entry(self, entry: ScheduleEntry) -> SessionDemand:
        return SessionDemand(
            group_id=entry.group_id,
            subject_id=entry.subject_id,
            teacher_candidates=[entry.teacher_id],
            room_candidates=[entry.room_id],
            week_scope=entry.week_scope,
            shift=entry.shift,
            delivery_mode=entry.delivery_mode,
            lesson_mode=entry.lesson_mode,
            slot_category=entry.slot_category,
            locked=entry.locked,
            metadata={"total_hours": 0},
        )

    @staticmethod
    def _soft_score(
        entries: list[ScheduleEntry],
        demand: SessionDemand,
        day_of_week: int,
        pair_number: int,
        online_slot_number: int | None,
        teacher_id: int,
        origin: tuple[int, int, int] | None = None,
    ) -> int:
        if demand.lesson_mode == LESSON_MODE_ONLINE:
            group_day_online = [
                entry
                for entry in entries
                if entry.group_id == demand.group_id and entry.lesson_mode == LESSON_MODE_ONLINE and entry.day_of_week == day_of_week
            ]
            teacher_day_online = [
                entry
                for entry in entries
                if entry.teacher_id == teacher_id and entry.lesson_mode == LESSON_MODE_ONLINE and entry.day_of_week == day_of_week
            ]
            same_subject = sum(1 for entry in group_day_online if entry.subject_id == demand.subject_id)
            spread_penalty = len(group_day_online) * 8
            teacher_penalty = len(teacher_day_online) * 4
            repair_distance = 0
            if origin is not None:
                repair_distance = abs(day_of_week - origin[0]) * 10 + abs((online_slot_number or 0) - origin[2]) * 5
            return spread_penalty + teacher_penalty + same_subject * 4 + repair_distance

        group_day_entries = [
            entry
            for entry in entries
            if entry.group_id == demand.group_id and entry.lesson_mode == LESSON_MODE_REGULAR and entry.day_of_week == day_of_week
        ]
        teacher_day_entries = [
            entry
            for entry in entries
            if entry.teacher_id == teacher_id and entry.lesson_mode == LESSON_MODE_REGULAR and entry.day_of_week == day_of_week
        ]
        same_subject_day = sum(1 for entry in group_day_entries if entry.subject_id == demand.subject_id)
        group_pairs = sorted(entry.pair_number for entry in group_day_entries)
        gap_penalty = 0
        if group_pairs:
            future_pairs = sorted(group_pairs + [pair_number])
            for previous, current in zip(future_pairs, future_pairs[1:]):
                if current - previous > 1:
                    gap_penalty += 4
        consecutive_penalty = 0
        if group_pairs:
            contiguous = 1
            future_pairs = sorted(group_pairs + [pair_number])
            for previous, current in zip(future_pairs, future_pairs[1:]):
                contiguous = contiguous + 1 if current - previous == 1 else 1
                if contiguous > 2:
                    consecutive_penalty += 3
        compactness = abs(pair_number - min(allowed_pairs_for_shift(demand.shift)))
        repair_distance = 0
        if origin is not None:
            repair_distance = abs(day_of_week - origin[0]) * 10 + abs(pair_number - origin[1]) * 3
        return (
            len(group_day_entries) * 7
            + len(teacher_day_entries) * 4
            + same_subject_day * 5
            + gap_penalty
            + consecutive_penalty
            + compactness
            + repair_distance
        )

    @staticmethod
    def _teacher_pressure(demands: list[SessionDemand]) -> Counter[int]:
        pressure: Counter[int] = Counter()
        for demand in demands:
            pressure.update(demand.teacher_candidates)
        return pressure

    @staticmethod
    def _priority_key(demand: SessionDemand, teacher_pressure: Counter[int]) -> tuple[int, int, int, int, int, int]:
        teacher_option_count = len(demand.teacher_candidates) or 99
        room_option_count = len(demand.room_candidates) or 99
        pressure = max((teacher_pressure.get(teacher_id, 0) for teacher_id in demand.teacher_candidates), default=0)
        total_hours = int(demand.metadata.get("total_hours", 0))
        return (
            0 if demand.lesson_mode == LESSON_MODE_REGULAR else 1,
            0 if demand.locked else 1,
            teacher_option_count,
            room_option_count,
            -pressure,
            -total_hours,
        )

    @staticmethod
    def _get_study_weeks(session: Session, group_id: int, semester: int) -> list[int]:
        periods = session.exec(
            select(AcademicPeriod).where(
                AcademicPeriod.group_id == group_id,
                AcademicPeriod.semester == semester,
                AcademicPeriod.is_schedulable.is_(True),
            )
        ).all()
        return sorted(period.week_number for period in periods)

    @staticmethod
    def _teacher_candidates(session: Session, group_id: int, subject_id: int) -> list[int]:
        fixed = session.exec(
            select(GroupSubjectTeacher).where(
                GroupSubjectTeacher.group_id == group_id,
                GroupSubjectTeacher.subject_id == subject_id,
            )
        ).all()
        if fixed:
            ordered = sorted(fixed, key=lambda item: (not item.fixed, item.teacher_id))
            return [item.teacher_id for item in ordered]
        allowed = session.exec(
            select(TeacherSubject).where(
                TeacherSubject.subject_id == subject_id,
                TeacherSubject.can_teach.is_(True),
            )
        ).all()
        if allowed:
            ordered = sorted(allowed, key=lambda item: (item.priority, item.teacher_id))
            return [item.teacher_id for item in ordered]
        return []

    @staticmethod
    def _room_candidates(session: Session, subject_id: int) -> list[int | None]:
        subject = session.get(Subject, subject_id)
        if subject and subject.requires_special_room:
            rooms = session.exec(
                select(Room).where(Room.room_type.in_(["computer_lab", "design_lab"]))
            ).all()
            if rooms:
                return [room.id or 0 for room in rooms]
            return []
        rooms = session.exec(select(Room).order_by(Room.code)).all()
        return [room.id or 0 for room in rooms]
