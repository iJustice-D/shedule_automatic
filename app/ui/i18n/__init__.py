from __future__ import annotations

from collections.abc import Mapping

from app.ui.i18n.kz import KZ_TRANSLATIONS
from app.ui.i18n.ru import RU_TRANSLATIONS


TRANSLATIONS = {
    "ru": RU_TRANSLATIONS,
    "kz": KZ_TRANSLATIONS,
}

DEFAULT_LANGUAGE = "ru"


def _resolve(mapping: Mapping[str, object], parts: list[str]) -> str | None:
    current: object = mapping
    for part in parts:
        if not isinstance(current, Mapping) or part not in current:
            return None
        current = current[part]
    return current if isinstance(current, str) else None


def t(key: str, lang: str = DEFAULT_LANGUAGE, **kwargs: object) -> str:
    parts = key.split(".")
    selected = TRANSLATIONS.get(lang, RU_TRANSLATIONS)
    value = _resolve(selected, parts) or _resolve(RU_TRANSLATIONS, parts) or key
    return value.format(**kwargs)
