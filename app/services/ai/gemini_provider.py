from __future__ import annotations

import json

import requests

from app.models import Conflict, Group, Schedule, ScheduleEntry, Suggestion
from app.services.ai.base import AITestResult, AIExplanationService


SYSTEM_PROMPT = (
    "Ты помощник в приложении колледжного расписания. "
    "Отвечай только на русском языке, кратко и понятно. "
    "Ты можешь объяснять конфликты, варианты исправления, ручные изменения и кратко суммировать расписание. "
    "Никогда не утверждай, что расписание корректно или некорректно, и не подменяй собой детерминированную валидацию."
)


class GeminiExplanationProvider(AIExplanationService):
    api_url_template = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

    def __init__(self, api_key: str, model: str = "gemini-2.5-flash", timeout: int = 15) -> None:
        self.api_key = api_key.strip()
        self.model = model.strip() or "gemini-2.5-flash"
        self.timeout = max(5, int(timeout))

    def explain_conflict(self, conflict: Conflict, suggestions: list[Suggestion]) -> str:
        ranked = [f"{index}. {item.message}" for index, item in enumerate(sorted(suggestions, key=lambda item: item.rank), start=1)]
        prompt = (
            "Объясни конфликт в расписании и кратко прокомментируй предложенные варианты исправления.\n"
            f"Серьезность: {conflict.severity}\n"
            f"Сообщение конфликта: {conflict.message}\n"
            f"Варианты исправления: {ranked or ['Варианты не найдены']}\n"
            "Сделай 2-4 коротких предложения без утверждений о полной корректности расписания."
        )
        return self._generate(prompt, max_output_tokens=220)

    def summarize_schedule(
        self,
        schedule: Schedule,
        entries: list[ScheduleEntry],
        conflicts: list[Conflict],
        groups: list[Group],
    ) -> str:
        prompt = (
            "Сделай короткую сводку по расписанию колледжа.\n"
            f"Название расписания: {schedule.name}\n"
            f"Семестр: {schedule.semester}\n"
            f"Группы: {[group.code for group in groups] or ['не выбраны']}\n"
            f"Всего занятий: {len(entries)}\n"
            f"Онлайн-занятий: {sum(1 for entry in entries if entry.lesson_mode == 'online')}\n"
            f"Очных или гибридных занятий: {sum(1 for entry in entries if entry.lesson_mode != 'online')}\n"
            f"Конфликтов: {len(conflicts)}\n"
            f"Критических конфликтов: {sum(1 for item in conflicts if item.severity == 'hard')}\n"
            "Сделай 2-3 предложения на русском языке."
        )
        return self._generate(prompt, max_output_tokens=180)

    def explain_manual_edit(
        self,
        before: dict[str, object],
        after: dict[str, object],
        remaining_conflicts: list[Conflict],
    ) -> str:
        prompt = (
            "Объясни, что изменилось после ручного редактирования занятия, и на что пользователю стоит обратить внимание.\n"
            f"До изменения: {json.dumps(before, ensure_ascii=False)}\n"
            f"После изменения: {json.dumps(after, ensure_ascii=False)}\n"
            f"Оставшиеся конфликты: {[item.message for item in remaining_conflicts]}\n"
            "Сделай 2-4 коротких предложения и не утверждай, что расписание уже полностью корректно."
        )
        return self._generate(prompt, max_output_tokens=220)

    def test_connection(self) -> AITestResult:
        try:
            self._generate("Ответь одним словом: OK.", max_output_tokens=8)
        except RuntimeError:
            return AITestResult(False, "Не удалось подключиться к Gemini")
        return AITestResult(True, "Подключение к Gemini успешно")

    def _generate(self, prompt: str, *, max_output_tokens: int) -> str:
        if not self.api_key:
            raise RuntimeError("Не удалось подключиться к Gemini")
        payload = {
            "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.2,
                "maxOutputTokens": max_output_tokens,
            },
        }
        try:
            response = requests.post(
                self.api_url_template.format(model=self.model),
                headers={
                    "Content-Type": "application/json",
                    "x-goog-api-key": self.api_key,
                },
                json=payload,
                timeout=self.timeout,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise RuntimeError("Не удалось подключиться к Gemini") from exc
        text = self._extract_text(response.json())
        if not text:
            raise RuntimeError("Не удалось подключиться к Gemini")
        return text.strip()

    @staticmethod
    def _extract_text(payload: dict) -> str:
        candidates = payload.get("candidates") or []
        for candidate in candidates:
            content = candidate.get("content") or {}
            parts = content.get("parts") or []
            texts = [part.get("text", "").strip() for part in parts if part.get("text")]
            if texts:
                return "\n".join(texts)
        return ""
