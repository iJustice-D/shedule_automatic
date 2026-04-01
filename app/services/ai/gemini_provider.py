from __future__ import annotations

from app.models import Conflict, Suggestion
from app.services.ai.base import AIExplanationService


class GeminiExplanationProvider(AIExplanationService):
    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    def explain_conflict(self, conflict: Conflict, suggestions: list[Suggestion]) -> str:
        if not self.api_key:
            raise RuntimeError("Ключ Gemini API не настроен.")
        ranked = "; ".join(suggestion.message for suggestion in sorted(suggestions, key=lambda item: item.rank))
        return (
            "Интеграция Gemini пока работает как заглушка. "
            f"Конфликт: {conflict.message}. Возможные исправления: {ranked or 'нет'}."
        )
