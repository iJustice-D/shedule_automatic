from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from app.models import Conflict, Group, Schedule, ScheduleEntry, Suggestion


@dataclass(slots=True)
class AITestResult:
    success: bool
    message: str


class AIExplanationService(ABC):
    @abstractmethod
    def explain_conflict(self, conflict: Conflict, suggestions: list[Suggestion]) -> str:
        raise NotImplementedError

    @abstractmethod
    def summarize_schedule(
        self,
        schedule: Schedule,
        entries: list[ScheduleEntry],
        conflicts: list[Conflict],
        groups: list[Group],
    ) -> str:
        raise NotImplementedError

    @abstractmethod
    def explain_manual_edit(
        self,
        before: dict[str, Any],
        after: dict[str, Any],
        remaining_conflicts: list[Conflict],
    ) -> str:
        raise NotImplementedError

    @abstractmethod
    def test_connection(self) -> AITestResult:
        raise NotImplementedError
