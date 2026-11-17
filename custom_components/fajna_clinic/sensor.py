"""Sensors per patient: when the next visit is, and how many are booked."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .coordinator import FajnaConfigEntry, FajnaCoordinator
from .entity import FajnaPersonEntity, add_person_entities
from .models import PlannedVisit

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: FajnaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    add_person_entities(
        entry.runtime_data,
        async_add_entities,
        lambda coordinator, person: [
            FajnaNextVisitSensor(coordinator, person),
            FajnaUpcomingVisitsSensor(coordinator, person),
        ],
    )


class FajnaNextVisitSensor(FajnaPersonEntity, SensorEntity):
    """The start of the next booked visit; unknown when nothing is booked."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: FajnaCoordinator, person: str) -> None:
        super().__init__(coordinator, person, "next_visit")

    def _next(self) -> PlannedVisit | None:
        data = self.person_data
        if data is None:
            return None
        now = dt_util.now()
        upcoming = [v for v in data.planned if v.start >= now]
        return min(upcoming, key=lambda v: v.start, default=None)

    @property
    def native_value(self) -> datetime | None:
        visit = self._next()
        return visit.start if visit else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        visit = self._next()
        if visit is None:
            return None
        return {
            "service": visit.service,
            "doctor": visit.doctor,
            "instructions": visit.instructions,
        }


class FajnaUpcomingVisitsSensor(FajnaPersonEntity, SensorEntity):
    """How many visits are booked."""

    def __init__(self, coordinator: FajnaCoordinator, person: str) -> None:
        super().__init__(coordinator, person, "upcoming_visits")

    @property
    def native_value(self) -> int | None:
        data = self.person_data
        return len(data.planned) if data else None
