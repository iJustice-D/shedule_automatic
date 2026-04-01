from __future__ import annotations

from abc import ABC, abstractmethod

from app.models import Conflict, Suggestion


class AIExplanationService(ABC):
    @abstractmethod
    def explain_conflict(self, conflict: Conflict, suggestions: list[Suggestion]) -> str:
        raise NotImplementedError
