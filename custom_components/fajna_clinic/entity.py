"""What every entity of the integration shares: a device per patient card."""

from __future__ import annotations

from collections.abc import Callable, Iterable

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import BASE_URL, DOMAIN, MANUFACTURER
from .coordinator import FajnaCoordinator
from .models import PersonData


class FajnaPersonEntity(CoordinatorEntity[FajnaCoordinator]):
    """An entity about one patient of the account.

    The site knows a patient by full name and by nothing else, so the name is the id.
    """

    _attr_has_entity_name = True

    def __init__(self, coordinator: FajnaCoordinator, person: str, key: str) -> None:
        super().__init__(coordinator)
        self._person = person
        account = coordinator.config_entry.unique_id
        self._attr_translation_key = key
        self._attr_unique_id = f"{account}_{person}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{account}_{person}")},
            name=person,
            manufacturer=MANUFACTURER,
            entry_type=DeviceEntryType.SERVICE,
            configuration_url=f"{BASE_URL}/account",
        )

    @property
    def person_data(self) -> PersonData | None:
        return self.coordinator.data.persons.get(self._person) if self.coordinator.data else None

    @property
    def available(self) -> bool:
        return super().available and self.person_data is not None


def add_person_entities(
    coordinator: FajnaCoordinator,
    async_add_entities: AddConfigEntryEntitiesCallback,
    build: Callable[[FajnaCoordinator, str], Iterable[Entity]],
) -> None:
    """Entities for every card now, and for a card added to the account later."""
    known: set[str] = set()

    @callback
    def add_new() -> None:
        new = [name for name in coordinator.data.persons if name not in known]
        if new:
            known.update(new)
            async_add_entities(entity for name in new for entity in build(coordinator, name))

    add_new()
    coordinator.config_entry.async_on_unload(coordinator.async_add_listener(add_new))
