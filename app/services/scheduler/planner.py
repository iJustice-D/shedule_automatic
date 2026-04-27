from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import replace

from sqlmodel import Session, select

from app.core.timetable import (
    DAYS,
    DELIVERY_OFFLINE,
    DELIVERY_ONLINE,
    LESSON_MODE_ONLINE,
    LESSON_MODE_REGULAR,
    allowed_pairs_for_shift,
    intervals_overlap,
    pair_end,
    pair_start,
    room_required,
)
from app.core.week_scope import decode_week_scope, scopes_overlap
from app.models import OnlineSlot, Teacher, WeeklyLoad
from app.services.online_policy import OnlinePolicyService
from app.services.scheduler.models import FeasibilityItem, FeasibilityReport, PlannedPlacement, PlacementRequest, PlannerResult


class FeasibilityAnalyzer:
    def __init__(self) -> None:
        self.online_policy_service = OnlinePolicyService()

    def analyze(
        self,
        session: Session,
        *,
        groups,
        rows,
        requests: list[PlacementRequest],
        enable_online: bool,
    ) -> FeasibilityReport:
        online_slots = session.exec(
            select(OnlineSlot).where(OnlineSlot.is_active.is_(True)).order_by(OnlineSlot.order_index, OnlineSlot.id)
        ).all()
        regular_capacity = sum(len(allowed_pairs_for_shift(group.shift)) * len(DAYS) for group in groups)
        online_capacity = len(online_slots)
        required_regular_requests = len([request for request in requests if request.lesson_mode == LESSON_MODE_REGULAR])
        unresolved_rows = sum(
            1
            for row in rows
            if row.assignment_state in {"vacancy", "unresolved_manual_review", "multi_teacher", "multi_teacher_ambiguous"}
        )
        excluded_rows = sum(1 for row in rows if row.excluded_status)
        requested_online_target = 0
        for group in groups:
            requested_online_target += self.online_policy_service.get_target_for_group(session, group) if enable_online else 0
        issues: list[FeasibilityItem] = []
        warnings: list[str] = []
        for row in rows:
            if row.excluded_status:
                issues.append(
                    FeasibilityItem(
                        load_key=row.load_key,
                        subject_id=row.subject_id,
                        subject_name=row.subject_name,
                        expected_pairs=row.total_pairs,
                        assignment_state=row.assignment_state,
                        issue_type="excluded",
                        message=row.excluded_reason,
                    )
                )
                continue
            if row.normalization_issue:
                issues.append(
                    FeasibilityItem(
                        load_key=row.load_key,
                        subject_id=row.subject_id,
                        subject_name=row.subject_name,
                        expected_pairs=row.total_pairs,
                        assignment_state=row.assignment_state,
                        issue_type="normalization",
                        message=row.normalization_issue,
                    )
                )
        if required_regular_requests > regular_capacity + (online_capacity if enable_online else 0):
            warnings.append(
                f"Ожидаемая недельная нагрузка ({required_regular_requests}) превышает доступную вместимость слотов ({regular_capacity + (online_capacity if enable_online else 0)})."
            )
        return FeasibilityReport(
            regular_capacity=regular_capacity,
            online_capacity=online_capacity,
            required_regular_requests=required_regular_requests,
            requested_online_target=requested_online_target,
            unresolved_rows=unresolved_rows,
            excluded_rows=excluded_rows,
            issues=issues,
            warnings=warnings,
        )


class ScopedSchedulePlanner:
    def __init__(self) -> None:
        self.online_policy_service = OnlinePolicyService()
        self._online_slots: dict[int, OnlineSlot] = {}
        self._teacher_base_by_semester: dict[int, dict[int, float]] = defaultdict(lambda: defaultdict(float))
        self._semester: int = 0

    def plan(
        self,
        session: Session,
        *,
        semester: int,
        groups,
        requests: list[PlacementRequest],
        enable_online: bool,
    ) -> PlannerResult:
        self._semester = semester
        self._online_slots = {
            slot.id or 0: slot
            for slot in session.exec(
                select(OnlineSlot).where(OnlineSlot.is_active.is_(True)).order_by(OnlineSlot.order_index, OnlineSlot.id)
            ).all()
        }
        self._teacher_base_by_semester = self._teacher_loads(session)

        planned_requests = self._assign_online_requests(session, groups, requests, enable_online=enable_online)
        ordered_requests = sorted(planned_requests, key=self._priority_key)
        placements: list[PlannedPlacement] = []
        unplaced: list[tuple[PlacementRequest, str]] = []
        for request in ordered_requests:
            placement = self._find_best_placement(placements, request)
            if placement is None:
                unplaced.append((request, self._unplaced_reason(request)))
                continue
            placements.append(placement)

        repaired, leftovers = self._repair_unplaced(placements, unplaced)
        placements = repaired
        warnings = []
        if enable_online:
            online_target = sum(self.online_policy_service.get_target_for_group(session, group) for group in groups)
            online_placed = sum(1 for placement in placements if placement.request.lesson_mode == LESSON_MODE_ONLINE)
            if online_target > online_placed:
                warnings.append(
                    f"Не удалось полностью выполнить цель по онлайн-занятиям: размещено {online_placed} из {online_target}."
                )
        return PlannerResult(placements=placements, unplaced=leftovers, warnings=warnings)

    def _assign_online_requests(self, session: Session, groups, requests: list[PlacementRequest], *, enable_online: bool) -> list[PlacementRequest]:
        if not enable_online:
            return requests
        by_group: dict[int, list[PlacementRequest]] = defaultdict(list)
        group_map = {group.id or 0: group for group in groups}
        for request in requests:
            by_group[request.group_id].append(request)
        updated: list[PlacementRequest] = []
        chosen_keys: set[str] = set()
        for group_id, group_requests in by_group.items():
            group = group_map.get(group_id)
            if group is None:
                continue
            target = self.online_policy_service.get_target_for_group(session, group)
            if target <= 0:
                continue
            selected_for_group: set[str] = set()
            eligible = [
                request
                for request in group_requests
                if request.can_be_online
                and request.assignment_state not in {"vacancy", "unresolved_manual_review"}
                and request.subgroup_code is None
                and request.lesson_mode == LESSON_MODE_REGULAR
            ]
            by_subject: dict[int, list[PlacementRequest]] = defaultdict(list)
            for request in eligible:
                by_subject[request.subject_id].append(request)
            for subject_id in sorted(by_subject, key=lambda item: (-len(by_subject[item]), item)):
                if len(selected_for_group) >= target:
                    break
                selected_for_group.add(by_subject[subject_id][0].request_key)
            if len(selected_for_group) < target:
                for request in eligible:
                    if len(selected_for_group) >= target:
                        break
                    selected_for_group.add(request.request_key)
            chosen_keys.update(selected_for_group)
        for request in requests:
            if request.request_key in chosen_keys:
                updated.append(
                    replace(
                        request,
                        lesson_mode=LESSON_MODE_ONLINE,
                        delivery_mode=DELIVERY_ONLINE,
                        room_candidates=[None],
                    )
                )
            else:
                updated.append(request)
        return updated

    def _repair_unplaced(
        self,
        placements: list[PlannedPlacement],
        unplaced: list[tuple[PlacementRequest, str]],
    ) -> tuple[list[PlannedPlacement], list[tuple[PlacementRequest, str]]]:
        current = list(placements)
        leftover: list[tuple[PlacementRequest, str]] = []
        for request, reason in unplaced:
            if self._repair_request(current, request, depth=2):
                continue
            leftover.append((request, reason))
        return current, leftover

    def _repair_request(self, placements: list[PlannedPlacement], request: PlacementRequest, depth: int) -> bool:
        placement = self._find_best_placement(placements, request)
        if placement is not None:
            placements.append(placement)
            return True
        if depth <= 0:
            return False
        for candidate in self._candidate_combinations(request):
            blockers = self._blockers_for_candidate(placements, request, *candidate)
            movable = [blocker for blocker in blockers if blocker.request.assignment_state != "fixed"]
            if len(movable) != len(blockers) or len(movable) > 2:
                continue
            remaining = [item for item in placements if item not in blockers]
            moved_blockers: list[PlannedPlacement] = []
            success = True
            for blocker in movable:
                blocker_request = blocker.request
                replacement = self._find_best_placement(remaining + moved_blockers, blocker_request, forbidden={self._slot_key(blocker)})
                if replacement is None:
                    success = False
                    break
                moved_blockers.append(replacement)
            if not success:
                continue
            for blocker in blockers:
                placements.remove(blocker)
            placements.extend(moved_blockers)
            new_placement = self._placement_from_candidate(request, *candidate)
            placements.append(new_placement)
            return True
        return False

    def _find_best_placement(
        self,
        placements: list[PlannedPlacement],
        request: PlacementRequest,
        forbidden: set[tuple[int, int, int]] | None = None,
    ) -> PlannedPlacement | None:
        best: tuple[tuple[int, int, int, int, int, int], PlannedPlacement] | None = None
        for candidate in self._candidate_combinations(request):
            day_of_week, pair_number, online_slot_id, teacher_id, room_id = candidate
            slot_key = (day_of_week, pair_number, online_slot_id or 0)
            if forbidden and slot_key in forbidden:
                continue
            blockers = self._blockers_for_candidate(placements, request, day_of_week, pair_number, online_slot_id, teacher_id, room_id)
            if blockers:
                continue
            score = (
                self._day_load_penalty(placements, request, day_of_week),
                self._gap_penalty(placements, request, day_of_week, pair_number),
                self._same_subject_penalty(placements, request, day_of_week),
                self._teacher_gap_penalty(placements, request, teacher_id, day_of_week, pair_number, online_slot_id),
                self._teacher_balance_penalty(placements, request, teacher_id),
                day_of_week * 10 + (pair_number or (online_slot_id or 0)),
            )
            placement = self._placement_from_candidate(request, day_of_week, pair_number, online_slot_id, teacher_id, room_id)
            if best is None or score < best[0]:
                best = (score, placement)
        return best[1] if best else None

    def _candidate_combinations(self, request: PlacementRequest) -> list[tuple[int, int, int | None, int | None, int | None]]:
        if request.assignment_state == "vacancy" or not request.teacher_candidates:
            return []
        candidates: list[tuple[int, int, int | None, int | None, int | None]] = []
        if request.lesson_mode == LESSON_MODE_ONLINE:
            for slot in sorted(self._online_slots.values(), key=lambda item: (item.order_index, item.id or 0)):
                if not slot.is_active:
                    continue
                for teacher_id in request.teacher_candidates:
                    candidates.append((slot.day_of_week, 0, slot.id or 0, teacher_id, None))
            return candidates
        for day_of_week in DAYS:
            for pair_number in allowed_pairs_for_shift(request.shift):
                for teacher_id in request.teacher_candidates:
                    if room_required(request.delivery_mode):
                        for room_id in request.room_candidates:
                            candidates.append((day_of_week, pair_number, None, teacher_id, room_id))
                    else:
                        candidates.append((day_of_week, pair_number, None, teacher_id, None))
        return candidates

    def _blockers_for_candidate(
        self,
        placements: list[PlannedPlacement],
        request: PlacementRequest,
        day_of_week: int,
        pair_number: int,
        online_slot_id: int | None,
        teacher_id: int | None,
        room_id: int | None,
    ) -> list[PlannedPlacement]:
        start_time, end_time = self._candidate_times(pair_number, online_slot_id)
        blockers: list[PlannedPlacement] = []
        for item in placements:
            if item.day_of_week != day_of_week:
                continue
            if not scopes_overlap(item.request.week_scope, request.week_scope):
                continue
            other_start, other_end = self._candidate_times(item.pair_number, item.online_slot_id)
            if not intervals_overlap(start_time, end_time, other_start, other_end):
                continue
            if teacher_id is not None and item.teacher_id == teacher_id:
                blockers.append(item)
                continue
            if self._group_overlap(item.request, request):
                blockers.append(item)
                continue
            if room_required(request.delivery_mode) and room_id is not None and room_required(item.request.delivery_mode) and item.room_id == room_id:
                blockers.append(item)
        return blockers

    @staticmethod
    def _group_overlap(left: PlacementRequest, right: PlacementRequest) -> bool:
        if left.group_id != right.group_id:
            return False
        if left.subgroup_code and right.subgroup_code and left.subgroup_code != right.subgroup_code:
            return False
        return True

    def _candidate_times(self, pair_number: int, online_slot_id: int | None) -> tuple[str, str]:
        if online_slot_id:
            slot = self._online_slots.get(online_slot_id)
            if slot is not None:
                return slot.start_time, slot.end_time
        if pair_number:
            return pair_start(pair_number), pair_end(pair_number)
        return "", ""

    @staticmethod
    def _placement_from_candidate(
        request: PlacementRequest,
        day_of_week: int,
        pair_number: int,
        online_slot_id: int | None,
        teacher_id: int | None,
        room_id: int | None,
    ) -> PlannedPlacement:
        return PlannedPlacement(
            request=request,
            day_of_week=day_of_week,
            pair_number=pair_number,
            online_slot_id=online_slot_id,
            teacher_id=teacher_id,
            room_id=room_id,
        )

    @staticmethod
    def _slot_key(placement: PlannedPlacement) -> tuple[int, int, int]:
        return placement.day_of_week, placement.pair_number, placement.online_slot_id or 0

    @staticmethod
    def _day_load_penalty(placements: list[PlannedPlacement], request: PlacementRequest, day_of_week: int) -> int:
        return len([item for item in placements if item.request.group_id == request.group_id and item.day_of_week == day_of_week]) * 4

    @staticmethod
    def _gap_penalty(placements: list[PlannedPlacement], request: PlacementRequest, day_of_week: int, pair_number: int) -> int:
        if request.lesson_mode == LESSON_MODE_ONLINE:
            return 0
        pairs = sorted(
            item.pair_number
            for item in placements
            if item.request.group_id == request.group_id and item.request.lesson_mode == LESSON_MODE_REGULAR and item.day_of_week == day_of_week
        )
        if not pairs:
            return 0
        future = sorted(pairs + [pair_number])
        penalty = 0
        for left, right in zip(future, future[1:]):
            if right - left > 1:
                penalty += 3
        return penalty

    @staticmethod
    def _same_subject_penalty(placements: list[PlannedPlacement], request: PlacementRequest, day_of_week: int) -> int:
        return sum(
            3
            for item in placements
            if item.request.group_id == request.group_id
            and item.day_of_week == day_of_week
            and item.request.subject_id == request.subject_id
        )

    def _teacher_gap_penalty(
        self,
        placements: list[PlannedPlacement],
        request: PlacementRequest,
        teacher_id: int | None,
        day_of_week: int,
        pair_number: int,
        online_slot_id: int | None,
    ) -> int:
        if teacher_id is None:
            return 100
        teacher_entries = [
            item
            for item in placements
            if item.teacher_id == teacher_id and item.day_of_week == day_of_week
        ]
        if request.lesson_mode == LESSON_MODE_ONLINE:
            return len(teacher_entries) * 2
        pairs = sorted(item.pair_number for item in teacher_entries if item.pair_number)
        if not pairs:
            return 0
        future = sorted(pairs + [pair_number])
        return sum(2 for left, right in zip(future, future[1:]) if right - left > 1)

    def _teacher_balance_penalty(self, placements: list[PlannedPlacement], request: PlacementRequest, teacher_id: int | None) -> int:
        if teacher_id is None:
            return 50
        other_semester = 4 if self._semester == 3 else 3
        current_base = float(self._teacher_base_by_semester.get(teacher_id, {}).get(self._semester, 0.0))
        other_base = float(self._teacher_base_by_semester.get(teacher_id, {}).get(other_semester, 0.0))
        current_scheduled = sum(item.request.weekly_weight for item in placements if item.teacher_id == teacher_id)
        projected = current_base + current_scheduled + request.weekly_weight
        return int(round(abs(projected - other_base) * 3))

    @staticmethod
    def _unplaced_reason(request: PlacementRequest) -> str:
        if request.assignment_state == "vacancy":
            return "Для этой нагрузки не назначен преподаватель."
        if request.assignment_state == "multi_teacher":
            return "В исходной строке указано несколько преподавателей, требуется уточнение."
        if request.assignment_state == "unresolved_manual_review":
            return "Строка требует ручной проверки преподавателя."
        if request.assignment_state == "candidate_pool" and not request.teacher_candidates:
            return "Не найдено допустимых преподавателей для строки."
        if request.lesson_mode == LESSON_MODE_ONLINE:
            return "Не найден свободный онлайн-слот без конфликта преподавателя или группы."
        return "Не хватило свободных слотов без нарушения жёстких ограничений."

    @staticmethod
    def _priority_key(request: PlacementRequest) -> tuple[int, int, int, int, int, int, int]:
        teacher_options = len(request.teacher_candidates) or 99
        room_options = len(request.room_candidates) or 99
        fixed_rank = 0 if request.fixed_teacher_id else 1
        subgroup_rank = 0 if request.subgroup_code else 1
        large_rank = -int(round(request.total_pairs))
        special_room_rank = 0 if request.requires_special_room else 1
        unresolved_rank = 1 if request.assignment_state in {"vacancy", "unresolved_manual_review", "multi_teacher"} else 0
        return (
            fixed_rank,
            subgroup_rank,
            teacher_options,
            special_room_rank,
            unresolved_rank,
            room_options,
            large_rank - request.source_priority,
        )

    @staticmethod
    def _teacher_loads(session: Session) -> dict[int, dict[int, float]]:
        loads: dict[int, dict[int, float]] = defaultdict(lambda: defaultdict(float))
        rows = session.exec(select(WeeklyLoad).where(WeeklyLoad.is_active.is_(True))).all()
        for row in rows:
            teacher_id = row.resolved_teacher_id or row.fixed_teacher_id
            if teacher_id:
                loads[teacher_id][row.semester] += float(row.weekly_pairs or 0.0)
        return loads
