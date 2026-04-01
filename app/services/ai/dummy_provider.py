from __future__ import annotations

from typing import Any

from app.models import Conflict, Group, Schedule, ScheduleEntry, Suggestion
from app.services.ai.base import AITestResult, AIExplanationService


class DummyExplanationProvider(AIExplanationService):
    def __init__(
        self,
        *,
        connection_message: str = "Используется стандартный режим без ИИ",
        fallback_notice: str | None = None,
    ) -> None:
        self.connection_message = connection_message
        self.fallback_notice = fallback_notice or connection_message

    def explain_conflict(self, conflict: Conflict, suggestions: list[Suggestion]) -> str:
        ranked = [f"{index}. {item.message}" for index, item in enumerate(sorted(suggestions, key=lambda item: item.rank), start=1)]
        if ranked:
            details = " Возможные варианты исправления: " + " ".join(ranked)
        else:
            details = " Автоматические варианты исправления пока не найдены."
        return self._with_notice(
            f"Конфликт: {conflict.message}.{details} Итоговую корректность расписания по-прежнему определяют правила системы."
        )

    def summarize_schedule(
        self,
        schedule: Schedule,
        entries: list[ScheduleEntry],
        conflicts: list[Conflict],
        groups: list[Group],
    ) -> str:
        total_entries = len(entries)
        online_entries = sum(1 for entry in entries if entry.lesson_mode == "online")
        regular_entries = total_entries - online_entries
        hard_conflicts = sum(1 for item in conflicts if item.severity == "hard")
        group_codes = ", ".join(group.code for group in groups[:5]) if groups else "не выбраны"
        return self._with_notice(
            "Краткая сводка расписания: "
            f"«{schedule.name}», группы {group_codes}, всего занятий {total_entries}, "
            f"очных {regular_entries}, онлайн {online_entries}, конфликтов {len(conflicts)}, "
            f"критических {hard_conflicts}."
        )

    def explain_manual_edit(
        self,
        before: dict[str, Any],
        after: dict[str, Any],
        remaining_conflicts: list[Conflict],
    ) -> str:
        field_titles = {
            "subject": "предмет",
            "lesson_type": "тип занятия",
            "day": "день",
            "slot": "слот",
            "teacher": "преподаватель",
            "room": "аудитория",
            "delivery_mode": "формат",
            "locked": "фиксация",
        }
        changes = [
            f"{title}: «{before.get(key, 'не указано')}» -> «{after.get(key, 'не указано')}»"
            for key, title in field_titles.items()
            if before.get(key) != after.get(key)
        ]
        if not changes:
            changes.append("существенные параметры занятия не изменились")
        if remaining_conflicts:
            tail = f"После изменения осталось конфликтов: {len(remaining_conflicts)}."
        else:
            tail = (
                "После изменения активные конфликты не обнаружены текущими правилами, "
                "но окончательную корректность все равно проверяет система."
            )
        return self._with_notice(f"Изменение сохранено. {'; '.join(changes[:4])}. {tail}")

    def test_connection(self) -> AITestResult:
        return AITestResult(False, self.connection_message)

    def _with_notice(self, text: str) -> str:
        notice = self.fallback_notice.strip().rstrip(".")
        if not notice:
            return text
        return f"{notice}. {text}"
