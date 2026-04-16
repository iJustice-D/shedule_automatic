from __future__ import annotations

from dataclasses import dataclass

from sqlmodel import Session, select

from app.core.timetable import ONLINE_SLOT_DEFINITIONS
from app.models import OnlineSlot, ScheduleEntry


@dataclass(slots=True)
class OnlineSlotView:
    id: int
    label: str
    day_of_week: int
    start_time: str
    end_time: str
    is_active: bool
    order_index: int


class OnlineSlotService:
    def ensure_defaults(self, session: Session) -> list[OnlineSlot]:
        rows = session.exec(select(OnlineSlot).order_by(OnlineSlot.order_index, OnlineSlot.id)).all()
        if rows:
            return rows
        created: list[OnlineSlot] = []
        for slot_id, payload in ONLINE_SLOT_DEFINITIONS.items():
            slot = OnlineSlot(
                id=slot_id,
                label=str(payload["label"]),
                day_of_week=int(payload["day_of_week"]),
                start_time=str(payload.get("start", "")),
                end_time=str(payload.get("end", "")),
                is_active=True,
                order_index=slot_id,
            )
            session.add(slot)
            created.append(slot)
        session.commit()
        return session.exec(select(OnlineSlot).order_by(OnlineSlot.order_index, OnlineSlot.id)).all()

    def active_slots(self, session: Session) -> list[OnlineSlot]:
        rows = session.exec(
            select(OnlineSlot).where(OnlineSlot.is_active.is_(True)).order_by(OnlineSlot.order_index, OnlineSlot.id)
        ).all()
        if rows:
            return rows
        return self.ensure_defaults(session)

    def all_slots(self, session: Session) -> list[OnlineSlot]:
        rows = session.exec(select(OnlineSlot).order_by(OnlineSlot.order_index, OnlineSlot.id)).all()
        if rows:
            return rows
        return self.ensure_defaults(session)

    @staticmethod
    def slot_map(session: Session) -> dict[int, OnlineSlot]:
        service = OnlineSlotService()
        return {item.id or 0: item for item in service.all_slots(session)}

    def apply_to_entry(self, session: Session, entry: ScheduleEntry) -> None:
        slot = self.slot_map(session).get(entry.online_slot_number or 0)
        if slot is None:
            return
        entry.day_of_week = slot.day_of_week
        entry.start_time = slot.start_time
        entry.end_time = slot.end_time
        entry.shift = "online"

