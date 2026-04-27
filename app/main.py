from __future__ import annotations

import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlmodel import Session

from app.api.routes import router
from app.core.config import settings
from app.db.session import engine, init_db
from app.services.seeding import Seeder
from app.ui.pages import register_ui, resolve_favicon


def bootstrap() -> None:
    init_db()
    with Session(engine) as session:
        Seeder().seed(
            session,
            settings.curriculum_source,
            settings.calendar_source,
            settings.weekly_workload_source,
            include_demo=settings.include_demo_seed,
        )


bootstrap()

app = FastAPI(title=settings.app_name)
app.include_router(router, prefix="/api")
app.mount("/exports", StaticFiles(directory=settings.exports_dir), name="exports")


def _emoji_favicon_svg(char: str = "📅") -> str:
    return f"""
    <svg viewBox="0 0 128 128" width="128" height="128" xmlns="http://www.w3.org/2000/svg">
        <rect width="128" height="128" rx="24" fill="#2f4858" />
        <text x="50%" y="54%" text-anchor="middle" dominant-baseline="middle" font-size="92">{char}</text>
    </svg>
    """


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> Response:
    favicon_value = resolve_favicon()
    if hasattr(favicon_value, "exists") and favicon_value.exists():
        return FileResponse(str(favicon_value), media_type="image/svg+xml")
    return Response(_emoji_favicon_svg(), media_type="image/svg+xml")


register_ui(app)


if __name__ == "__main__":
    uvicorn.run("app.main:app", host="127.0.0.1", port=8080, reload=False)
