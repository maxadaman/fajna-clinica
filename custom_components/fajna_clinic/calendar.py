"""A calendar per patient: the visits booked."""

from __future__ import annotations

from datetime import datetime

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import DEFAULT_VISIT_DURATION
from .coordinator import FajnaConfigEntry, FajnaCoordinator
from .entity import FajnaPersonEntity, add_person_entities
from .models import PlannedVisit

PARALLEL_UPDATES = 0


def visit_event(visit: PlannedVisit) -> CalendarEvent:
    details = [
        ("Лікар", visit.doctor),
        ("Інструкції", visit.instructions),
        ("Оплата", visit.payment),
    ]
    return CalendarEvent(
        start=visit.start,
        # The page gives the start only.
        end=visit.start + DEFAULT_VISIT_DURATION,
        summary=visit.service or "Візит до Файної Клініки",
        description="\n".join(f"{label}: {value}" for label, value in details if value) or None,
        uid=visit.uid,
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: FajnaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    add_person_entities(
        entry.runtime_data,
        async_add_entities,
        lambda coordinator, person: [FajnaVisitsCalendar(coordinator, person)],
    )


class FajnaVisitsCalendar(FajnaPersonEntity, CalendarEntity):
    """The visits one patient has booked."""

    def __init__(self, coordinator: FajnaCoordinator, person: str) -> None:
        super().__init__(coordinator, person, "visits")

    def _events(self) -> list[CalendarEvent]:
        data = self.person_data
        return sorted((visit_event(v) for v in data.planned), key=lambda e: e.start) if data else []

    @property
    def event(self) -> CalendarEvent | None:
        """The visit under way, or else the next one."""
        now = dt_util.now()
        return next((e for e in self._events() if e.end > now), None)

    async def async_get_events(
        self, hass: HomeAssistant, start_date: datetime, end_date: datetime
    ) -> list[CalendarEvent]:
        return [e for e in self._events() if e.start < end_date and e.end > start_date]
