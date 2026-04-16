from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parents[2]
load_dotenv(BASE_DIR / ".env")


def _first_existing_path(*candidates: str) -> Path:
    for candidate in candidates:
        path = Path(candidate)
        if path.exists():
            return path
    return Path(candidates[0])


@dataclass(slots=True)
class Settings:
    app_name: str = os.getenv("APP_NAME", "Автоматизация расписания колледжа")
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./timetable.db")
    exports_dir: Path = BASE_DIR / os.getenv("EXPORTS_DIR", "./exports")
    ai_provider: str = os.getenv("AI_PROVIDER", "dummy")
    gemini_api_key: str = os.getenv("GEMINI_API_KEY", "")
    secret_key: str = os.getenv("SECRET_KEY", "development-secret")
    curriculum_source: Path = BASE_DIR / "data" / "Оқу жоспар_ЕТБ-1124.xls"
    calendar_source: Path = BASE_DIR / "data" / "Үрдіс 2025-2026 оқу жылы соңғысы (1).pdf"
    weekly_workload_source: Path = _first_existing_path(
        os.getenv("WEEKLY_WORKLOAD_SOURCE", str(BASE_DIR / "data" / "2025-2026 ПРОГРАММИСТТЕР_ИНКАР (1) (2) (1).docx")),
        str(Path("/Users/adiletongar/Downloads/2025-2026 ПРОГРАММИСТТЕР_ИНКАР (1) (2) (1).docx")),
    )


settings = Settings()
settings.exports_dir.mkdir(parents=True, exist_ok=True)
