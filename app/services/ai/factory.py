from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from app.core.config import settings
from app.services.ai.dummy_provider import DummyExplanationProvider
from app.services.ai.gemini_provider import GeminiExplanationProvider


@dataclass(slots=True)
class AISettings:
    enabled: bool = settings.ai_enabled
    provider: str = settings.ai_provider or "gemini"
    gemini_api_key: str = settings.gemini_api_key
    gemini_model: str = settings.gemini_model
    gemini_timeout: int = settings.gemini_timeout


def build_ai_settings(values: Mapping[str, object] | None = None) -> AISettings:
    values = values or {}
    return AISettings(
        enabled=_parse_bool(values.get("ai_enabled"), settings.ai_enabled),
        provider=str(values.get("ai_provider") or settings.ai_provider or "gemini").strip().lower(),
        gemini_api_key=str(values.get("gemini_api_key") or settings.gemini_api_key or "").strip(),
        gemini_model=str(values.get("gemini_model") or settings.gemini_model or "gemini-2.5-flash").strip() or "gemini-2.5-flash",
        gemini_timeout=max(5, _parse_int(values.get("gemini_timeout"), settings.gemini_timeout)),
    )


def create_provider(config: AISettings):
    if not config.enabled:
        return DummyExplanationProvider(
            connection_message="ИИ отключен в настройках",
            fallback_notice="ИИ отключен в настройках. Используется стандартный режим без ИИ",
        )
    if config.provider != "gemini":
        return DummyExplanationProvider(
            connection_message="Используется стандартный режим без ИИ",
            fallback_notice="Используется стандартный режим без ИИ",
        )
    if not config.gemini_api_key:
        return DummyExplanationProvider(
            connection_message="API-ключ не указан",
            fallback_notice="API-ключ не указан. Используется стандартный режим без ИИ",
        )
    return GeminiExplanationProvider(
        api_key=config.gemini_api_key,
        model=config.gemini_model,
        timeout=config.gemini_timeout,
    )


def _parse_bool(value: object, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _parse_int(value: object, default: int) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
