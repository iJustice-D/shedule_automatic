from __future__ import annotations

from sqlmodel import Session

from app.core.config import settings
from app.db.session import engine, init_db
from app.services.seeding import Seeder


def main() -> None:
    init_db()
    with Session(engine) as session:
        Seeder().seed(session, settings.curriculum_source, settings.calendar_source)
    print("Database seeded.")


if __name__ == "__main__":
    main()
