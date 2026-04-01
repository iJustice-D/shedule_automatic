from __future__ import annotations

from app.models import Conflict, Suggestion
from app.services.ai.base import AIExplanationService


class DummyExplanationProvider(AIExplanationService):
    def explain_conflict(self, conflict: Conflict, suggestions: list[Suggestion]) -> str:
        ranked = "; ".join(suggestion.message for suggestion in sorted(suggestions, key=lambda item: item.rank))
        if not ranked:
            ranked = "Автоматическое решение пока не найдено."
        return f"{conflict.message} Возможные действия: {ranked}"
