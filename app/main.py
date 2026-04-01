from __future__ import annotations

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from sqlmodel import Session

from app.api.routes import router
from app.core.config import settings
from app.db.session import engine, init_db
from app.services.seeding import Seeder
from app.ui.pages import register_ui


def bootstrap() -> None:
    init_db()
    with Session(engine) as session:
        Seeder().seed(session, settings.curriculum_source, settings.calendar_source)


bootstrap()

app = FastAPI(title=settings.app_name)
app.include_router(router, prefix="/api")
app.mount("/exports", StaticFiles(directory=settings.exports_dir), name="exports")
register_ui(app)


if __name__ == "__main__":
    uvicorn.run("app.main:app", host="127.0.0.1", port=8080, reload=False)
