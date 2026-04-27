from __future__ import annotations

from collections import defaultdict

from sqlmodel import Session, select

from app.models import Conflict, Group, Schedule, ScheduleEntry, Subject, Suggestion, Teacher
from app.services.online_policy import OnlinePolicyService
from app.services.scheduler.models import DiagnosticsBundle, SubjectSummaryRow
from app.services.scheduler.normalizer import WorkloadNormalizer
from app.services.weekly_workload import WeeklyWorkloadService


class ResultDiagnosticsService:
    def __init__(self) -> None:
        self.normalizer = WorkloadNormalizer()
        self.online_policy_service = OnlinePolicyService()
        self.weekly_workload_service = WeeklyWorkloadService()

    def build(
        self,
        session: Session,
        *,
        schedule_id: int,
        group_id: int | None = None,
        hard_conflicts: list[Conflict],
        unscheduled_conflicts: list[Conflict],
        warnings: list[dict[str, object]],
    ) -> DiagnosticsBundle:
        schedule = session.get(Schedule, schedule_id)
        if schedule is None:
            raise ValueError("Расписание не найдено.")
        group_ids = self._schedule_group_ids(session, schedule, group_id)
        groups = {group.id or 0: group for group in session.exec(select(Group)).all()}
        selected_groups = [groups[item] for item in group_ids if item in groups]
        group_codes = [group.code for group in selected_groups]
        _, normalized_rows, _ = self.normalizer.normalize_scope(
            session,
            semester=schedule.semester,
            group_codes=group_codes,
            include_facultatives=False,
        )
        entries_query = select(ScheduleEntry).where(ScheduleEntry.schedule_id == schedule_id)
        if group_ids:
            entries_query = entries_query.where(ScheduleEntry.group_id.in_(group_ids))
        entries = session.exec(entries_query).all()
        placed_by_load: dict[str, int] = defaultdict(int)
        online_placed_by_group: dict[int, int] = defaultdict(int)
        for entry in entries:
            if entry.source_load_key:
                placed_by_load[entry.source_load_key] += len(self._entry_weeks(entry))
            if entry.lesson_mode == "online":
                online_placed_by_group[entry.group_id] += 1

        grouped_rows: dict[tuple[int, int, str], list] = defaultdict(list)
        for row in normalized_rows:
            grouped_rows[(row.group_id, row.subject_id, row.subgroup_code or "")].append(row)

        subject_rows: list[SubjectSummaryRow] = []
        normalization_issues: list[dict[str, object]] = []
        summary = {
            "selected_group": selected_groups[0].code if len(selected_groups) == 1 else "Несколько групп",
            "selected_semester": schedule.semester,
            "expected_subjects_count": 0,
            "fully_placed_subjects_count": 0,
            "partially_placed_subjects_count": 0,
            "not_placed_subjects_count": 0,
            "total_missing_pairs": 0,
            "hard_conflicts_count": len(hard_conflicts),
            "unscheduled_count": len(unscheduled_conflicts),
            "warnings_count": len(warnings),
            "unresolved_teacher_rows_count": 0,
            "online_placed_count": sum(online_placed_by_group.values()),
            "online_missing_count": 0,
            "teachers_with_balance_issue_count": 0,
        }

        for key, rows in grouped_rows.items():
            example = rows[0]
            expected_pairs = sum(row.total_pairs for row in rows)
            placed_pairs = sum(placed_by_load.get(row.load_key, 0) for row in rows)
            missing_pairs = max(expected_pairs - placed_pairs, 0)
            status = "Полностью размещено"
            reason = ""
            if all(row.excluded_status for row in rows):
                status = rows[0].excluded_status
                reason = rows[0].excluded_reason
                placed_pairs = 0
                missing_pairs = expected_pairs
            elif any(row.assignment_state in {"vacancy", "unresolved_manual_review", "multi_teacher", "multi_teacher_ambiguous"} for row in rows) and missing_pairs:
                status = "Требуется уточнение преподавателя"
                reason = self._reason_from_rows(rows)
            elif any(row.subgroup_code and not row.teacher_candidates for row in rows) and missing_pairs:
                status = "Требуется уточнение подгруппы"
                reason = "Для подгруппы не хватает уточнённого закрепления преподавателя."
            elif missing_pairs and placed_pairs:
                status = "Частично размещено"
                reason = self._reason_from_rows(rows)
            elif missing_pairs:
                status = "Не размещено"
                reason = self._reason_from_rows(rows)

            label = example.subject_name
            if example.subgroup_code:
                label = f"{label} (подгруппа {example.subgroup_code})"
            subject_rows.append(
                SubjectSummaryRow(
                    group_id=example.group_id,
                    subject_id=example.subject_id,
                    subject=label,
                    expected_pairs=expected_pairs,
                    placed_pairs=placed_pairs,
                    missing_pairs=missing_pairs,
                    status=status,
                    reason=reason,
                    assignment_state=example.assignment_state,
                    subgroup_code=example.subgroup_code,
                )
            )
            summary["expected_subjects_count"] += 1
            if status == "Полностью размещено":
                summary["fully_placed_subjects_count"] += 1
            elif status == "Частично размещено":
                summary["partially_placed_subjects_count"] += 1
                summary["total_missing_pairs"] += missing_pairs
            elif status in {"Не размещено", "Требуется уточнение преподавателя", "Требуется уточнение подгруппы"}:
                summary["not_placed_subjects_count"] += 1
                summary["total_missing_pairs"] += missing_pairs

        for row in normalized_rows:
            if row.assignment_state in {"vacancy", "unresolved_manual_review", "multi_teacher", "multi_teacher_ambiguous", "candidate_pool"}:
                summary["unresolved_teacher_rows_count"] += 1
            if row.excluded_status or row.normalization_issue:
                normalization_issues.append(
                    {
                        "subject": row.subject_name if not row.subgroup_code else f"{row.subject_name} (подгруппа {row.subgroup_code})",
                        "state": row.assignment_state,
                        "message": row.excluded_reason or row.normalization_issue or "Требуется ручная проверка строки.",
                    }
                )

        teacher_ids = {
            entry.teacher_id
            for entry in entries
            if entry.teacher_id is not None
        }
        teacher_balance_rows = [
            item
            for item in self.weekly_workload_service.teacher_balance_report(session)
            if item["teacher_id"] in teacher_ids
        ]
        summary["teachers_with_balance_issue_count"] = sum(
            1 for item in teacher_balance_rows if float(item["normalized_balance_score"]) >= 2.0
        )

        for group in selected_groups:
            target = self.online_policy_service.get_target_for_group(session, group)
            summary["online_missing_count"] += max(target - online_placed_by_group.get(group.id or 0, 0), 0)

        return DiagnosticsBundle(
            summary=summary,
            subject_rows=sorted(subject_rows, key=lambda item: (item.status != "Не размещено", item.subject)),
            normalization_issues=normalization_issues,
            warnings=warnings,
            hard_conflicts=hard_conflicts,
            unscheduled_conflicts=unscheduled_conflicts,
            teacher_balance_rows=teacher_balance_rows,
        )

    @staticmethod
    def _schedule_group_ids(session: Session, schedule: Schedule, group_id: int | None) -> list[int]:
        if group_id is not None:
            return [group_id]
        codes = [item.strip() for item in (schedule.group_scope or "").split(",") if item.strip()]
        if not codes:
            return []
        return [group.id or 0 for group in session.exec(select(Group).where(Group.code.in_(codes))).all()]

    @staticmethod
    def _entry_weeks(entry: ScheduleEntry) -> set[int]:
        from app.core.week_scope import decode_week_scope

        weeks = decode_week_scope(entry.week_scope)
        return weeks or set()

    @staticmethod
    def _reason_from_rows(rows) -> str:
        if any(row.assignment_state == "vacancy" for row in rows):
            return "Для этой нагрузки не назначен преподаватель."
        if any(row.assignment_state == "multi_teacher_ambiguous" for row in rows):
            return "Неоднозначное закрепление преподавателя по семестру."
        if any(row.assignment_state == "multi_teacher" for row in rows):
            return "В исходной строке указано несколько преподавателей, требуется уточнение."
        if any(row.assignment_state == "candidate_pool" for row in rows):
            return "Для строки доступно несколько кандидатов, требуется уточнение закрепления."
        if any(row.normalization_issue for row in rows):
            return next(row.normalization_issue for row in rows if row.normalization_issue)
        return "Не хватило свободных слотов без нарушения жёстких ограничений."
