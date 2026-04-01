from __future__ import annotations

from collections.abc import Generator
import sqlite3
from pathlib import Path

from sqlmodel import Session, SQLModel, create_engine

from app.core.config import settings


engine = create_engine(
    settings.database_url,
    echo=False,
    connect_args={"check_same_thread": False},
)


def get_session() -> Generator[Session, None, None]:
    with Session(engine) as session:
        yield session


def init_db() -> None:
    SQLModel.metadata.create_all(engine)
    _migrate_sqlite_schema()


def _sqlite_db_path() -> Path | None:
    if not settings.database_url.startswith("sqlite:///"):
        return None
    path = settings.database_url.replace("sqlite:///", "", 1)
    if path == ":memory:":
        return None
    return Path(path)


def _table_columns(connection: sqlite3.Connection, table_name: str) -> dict[str, dict[str, int | str | None]]:
    rows = connection.execute(f"PRAGMA table_info('{table_name}')").fetchall()
    return {
        row[1]: {"type": row[2], "notnull": row[3], "default": row[4], "pk": row[5]}
        for row in rows
    }


def _ensure_column(connection: sqlite3.Connection, table_name: str, column_name: str, sql_definition: str) -> None:
    columns = _table_columns(connection, table_name)
    if column_name in columns:
        return
    connection.execute(f"ALTER TABLE '{table_name}' ADD COLUMN {column_name} {sql_definition}")


def _recreate_scheduleentry_if_needed(connection: sqlite3.Connection) -> None:
    columns = _table_columns(connection, "scheduleentry")
    if not columns:
        return
    needs_recreate = columns.get("room_id", {}).get("notnull") == 1
    if not needs_recreate:
        return
    connection.execute("PRAGMA foreign_keys=OFF")
    connection.execute(
        """
        CREATE TABLE scheduleentry_new (
            id INTEGER NOT NULL PRIMARY KEY,
            schedule_id INTEGER NOT NULL,
            group_id INTEGER NOT NULL,
            subject_id INTEGER NOT NULL,
            teacher_id INTEGER NOT NULL,
            room_id INTEGER,
            day_of_week INTEGER NOT NULL,
            pair_number INTEGER NOT NULL,
            online_slot_number INTEGER,
            lesson_mode VARCHAR DEFAULT 'regular',
            slot_category VARCHAR DEFAULT 'regular',
            shift VARCHAR DEFAULT '',
            start_time VARCHAR DEFAULT '',
            end_time VARCHAR DEFAULT '',
            delivery_mode VARCHAR DEFAULT 'offline',
            week_scope VARCHAR NOT NULL,
            locked BOOLEAN NOT NULL,
            FOREIGN KEY(schedule_id) REFERENCES schedule (id),
            FOREIGN KEY(group_id) REFERENCES "group" (id),
            FOREIGN KEY(subject_id) REFERENCES subject (id),
            FOREIGN KEY(teacher_id) REFERENCES teacher (id),
            FOREIGN KEY(room_id) REFERENCES room (id)
        )
        """
    )
    connection.execute(
        """
        INSERT INTO scheduleentry_new (
            id, schedule_id, group_id, subject_id, teacher_id, room_id, day_of_week, pair_number,
            online_slot_number, lesson_mode, slot_category, shift, start_time, end_time, delivery_mode, week_scope, locked
        )
        SELECT
            id,
            schedule_id,
            group_id,
            subject_id,
            teacher_id,
            room_id,
            day_of_week,
            pair_number,
            CASE
                WHEN delivery_mode = 'online' AND day_of_week = 3 THEN 1
                WHEN delivery_mode = 'online' AND day_of_week = 4 THEN 2
                WHEN delivery_mode = 'online' AND day_of_week = 5 THEN 3
                WHEN delivery_mode = 'online' THEN 1
                ELSE NULL
            END,
            CASE WHEN delivery_mode = 'online' THEN 'online' ELSE 'regular' END,
            CASE WHEN delivery_mode = 'online' THEN 'online_extra' ELSE 'regular' END,
            CASE WHEN pair_number <= 3 THEN 'morning' ELSE 'afternoon' END,
            CASE pair_number
                WHEN 1 THEN '08:00'
                WHEN 2 THEN '09:40'
                WHEN 3 THEN '11:10'
                WHEN 4 THEN '13:30'
                WHEN 5 THEN '15:10'
                WHEN 6 THEN '16:40'
                ELSE ''
            END,
            CASE pair_number
                WHEN 1 THEN '09:20'
                WHEN 2 THEN '11:00'
                WHEN 3 THEN '12:30'
                WHEN 4 THEN '14:50'
                WHEN 5 THEN '16:30'
                WHEN 6 THEN '18:00'
                ELSE ''
            END,
            'offline',
            week_scope,
            locked
        FROM scheduleentry
        """
    )
    connection.execute("DROP TABLE scheduleentry")
    connection.execute("ALTER TABLE scheduleentry_new RENAME TO scheduleentry")
    connection.execute("CREATE INDEX IF NOT EXISTS ix_scheduleentry_subject_id ON scheduleentry (subject_id)")
    connection.execute("CREATE INDEX IF NOT EXISTS ix_scheduleentry_schedule_id ON scheduleentry (schedule_id)")
    connection.execute("CREATE INDEX IF NOT EXISTS ix_scheduleentry_teacher_id ON scheduleentry (teacher_id)")
    connection.execute("CREATE INDEX IF NOT EXISTS ix_scheduleentry_group_id ON scheduleentry (group_id)")
    connection.execute("CREATE INDEX IF NOT EXISTS ix_scheduleentry_room_id ON scheduleentry (room_id)")
    connection.execute("PRAGMA foreign_keys=ON")


def _migrate_sqlite_schema() -> None:
    db_path = _sqlite_db_path()
    if db_path is None or not db_path.exists():
        return
    connection = sqlite3.connect(db_path)
    try:
        _ensure_column(connection, "group", "year", "INTEGER DEFAULT 1")
        _ensure_column(connection, "group", "shift", "VARCHAR DEFAULT 'morning'")
        _ensure_column(connection, "subject", "can_be_online", "BOOLEAN DEFAULT 0")
        _ensure_column(connection, "subject", "default_delivery_mode", "VARCHAR DEFAULT 'offline'")
        _ensure_column(connection, "timeslot", "shift", "VARCHAR DEFAULT 'morning'")
        _ensure_column(connection, "timeslot", "start_time", "VARCHAR DEFAULT ''")
        _ensure_column(connection, "timeslot", "end_time", "VARCHAR DEFAULT ''")
        _ensure_column(connection, "curriculumload", "delivery_mode", "VARCHAR DEFAULT 'offline'")
        _recreate_scheduleentry_if_needed(connection)
        _ensure_column(connection, "scheduleentry", "shift", "VARCHAR DEFAULT ''")
        _ensure_column(connection, "scheduleentry", "start_time", "VARCHAR DEFAULT ''")
        _ensure_column(connection, "scheduleentry", "end_time", "VARCHAR DEFAULT ''")
        _ensure_column(connection, "scheduleentry", "delivery_mode", "VARCHAR DEFAULT 'offline'")
        _ensure_column(connection, "scheduleentry", "online_slot_number", "INTEGER")
        _ensure_column(connection, "scheduleentry", "lesson_mode", "VARCHAR DEFAULT 'regular'")
        _ensure_column(connection, "scheduleentry", "slot_category", "VARCHAR DEFAULT 'regular'")
        connection.execute('UPDATE "group" SET year = course WHERE year IS NULL OR year = 0')
        connection.execute('UPDATE "group" SET shift = CASE WHEN shift IS NULL OR shift = "" THEN "morning" ELSE shift END')
        connection.execute(
            """
            UPDATE scheduleentry
            SET shift = CASE
                    WHEN pair_number <= 3 THEN 'morning'
                    ELSE 'afternoon'
                END,
                start_time = CASE pair_number
                    WHEN 1 THEN '08:00'
                    WHEN 2 THEN '09:40'
                    WHEN 3 THEN '11:10'
                    WHEN 4 THEN '13:30'
                    WHEN 5 THEN '15:10'
                    WHEN 6 THEN '16:40'
                    ELSE start_time
                END,
                end_time = CASE pair_number
                    WHEN 1 THEN '09:20'
                    WHEN 2 THEN '11:00'
                    WHEN 3 THEN '12:30'
                    WHEN 4 THEN '14:50'
                    WHEN 5 THEN '16:30'
                    WHEN 6 THEN '18:00'
                    ELSE end_time
                END,
                delivery_mode = CASE
                    WHEN delivery_mode IS NULL OR delivery_mode = '' THEN 'offline'
                    ELSE delivery_mode
                END,
                lesson_mode = CASE
                    WHEN delivery_mode = 'online' THEN 'online'
                    WHEN lesson_mode IS NULL OR lesson_mode = '' THEN 'regular'
                    ELSE lesson_mode
                END,
                slot_category = CASE
                    WHEN delivery_mode = 'online' THEN 'online_extra'
                    WHEN slot_category IS NULL OR slot_category = '' THEN 'regular'
                    ELSE slot_category
                END,
                online_slot_number = CASE
                    WHEN delivery_mode = 'online' AND day_of_week = 3 THEN 1
                    WHEN delivery_mode = 'online' AND day_of_week = 4 THEN 2
                    WHEN delivery_mode = 'online' AND day_of_week = 5 THEN 3
                    WHEN delivery_mode = 'online' AND (online_slot_number IS NULL OR online_slot_number = 0) THEN 1
                    ELSE online_slot_number
                END,
                pair_number = CASE
                    WHEN delivery_mode = 'online' THEN 0
                    ELSE pair_number
                END,
                shift = CASE
                    WHEN delivery_mode = 'online' THEN ''
                    ELSE shift
                END,
                start_time = CASE
                    WHEN delivery_mode = 'online' THEN ''
                    ELSE start_time
                END,
                end_time = CASE
                    WHEN delivery_mode = 'online' THEN ''
                    ELSE end_time
                END
            """
        )
        connection.commit()
    finally:
        connection.close()
