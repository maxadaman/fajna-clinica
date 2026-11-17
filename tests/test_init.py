"""Setting an account up: entities per patient, and a dead session asking to sign in."""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

from freezegun.api import FrozenDateTimeFactory
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from custom_components.fajna_clinic.api import AuthExpired, CannotConnect
from custom_components.fajna_clinic.const import CLINIC_TZ, CONF_PHONE, CONF_TOKEN, DOMAIN
from custom_components.fajna_clinic.models import AccountData, PersonData, PlannedVisit

PHONE = "+380991112233"
NOW = datetime(2026, 11, 20, 12, 0, tzinfo=CLINIC_TZ)


def account(*persons: PersonData) -> AccountData:
    return AccountData(persons={p.name: p for p in persons})


IVAN = PersonData(
    name="Тестенко Іван Петрович",
    planned=[
        PlannedVisit(datetime(2026, 12, 2, 9, 5, tzinfo=CLINIC_TZ), "Терапевт", "Анна"),
        PlannedVisit(datetime(2026, 11, 27, 18, 40, tzinfo=CLINIC_TZ), "Масаж спини", "Олег"),
        PlannedVisit(datetime(2026, 11, 18, 19, 20, tzinfo=CLINIC_TZ), "Масаж спини", "Олег"),
    ],
)
MARIA = PersonData(name="Тестенко Марія Іванівна")


def _entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=PHONE, data={CONF_PHONE: PHONE, CONF_TOKEN: "t"}
    )
    entry.add_to_hass(hass)
    return entry


def _fetch(*results):
    return patch(
        "custom_components.fajna_clinic.api.FajnaClient.fetch_account",
        AsyncMock(side_effect=list(results)),
    )


async def test_a_device_and_entities_per_patient(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(NOW)
    entry = _entry(hass)
    with _fetch(account(IVAN, MARIA)):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    devices = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    assert sorted(d.name for d in devices) == [IVAN.name, MARIA.name]

    def state(platform: str, person: PersonData, key: str):
        entity_id = er.async_get(hass).async_get_entity_id(
            platform, DOMAIN, f"{PHONE}_{person.name}_{key}"
        )
        return hass.states.get(entity_id)

    # The visit on the 18th has passed: the next one is the 27th, 18:40 in Kyiv.
    next_visit = state("sensor", IVAN, "next_visit")
    assert next_visit.state == "2026-11-27T16:40:00+00:00"
    assert next_visit.attributes["doctor"] == "Олег"
    assert state("sensor", MARIA, "next_visit").state == "unknown"

    assert state("sensor", IVAN, "upcoming_visits").state == "3"
    assert state("sensor", MARIA, "upcoming_visits").state == "0"
    assert state("calendar", IVAN, "visits") is not None
    assert state("calendar", MARIA, "visits").state == "off"


async def test_calendar_lists_the_visits_in_range(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(NOW)
    entry = _entry(hass)
    with _fetch(account(IVAN)):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    (calendar,) = [s.entity_id for s in hass.states.async_all("calendar")]

    response = await hass.services.async_call(
        "calendar",
        "get_events",
        {
            "entity_id": calendar,
            "start_date_time": NOW.isoformat(),
            "end_date_time": (NOW + timedelta(days=10)).isoformat(),
        },
        blocking=True,
        return_response=True,
    )

    events = response[calendar]["events"]
    assert [e["summary"] for e in events] == ["Масаж спини"]
    assert events[0]["description"] == "Лікар: Олег"
    assert hass.states.get(calendar).attributes["message"] == "Масаж спини"


async def test_a_patient_added_later_gets_entities(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(NOW)
    entry = _entry(hass)
    with _fetch(account(IVAN), account(IVAN, MARIA)):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert len(hass.states.async_all("calendar")) == 1

        freezer.tick(timedelta(minutes=31))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()

    assert len(hass.states.async_all("calendar")) == 2


async def test_a_dead_session_starts_reauth(hass: HomeAssistant) -> None:
    entry = _entry(hass)
    with _fetch(AuthExpired("login page")):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert [f["context"]["source"] for f in flows] == [SOURCE_REAUTH]


async def test_the_site_down_retries(hass: HomeAssistant) -> None:
    entry = _entry(hass)
    with _fetch(CannotConnect("down")):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_unload(hass: HomeAssistant) -> None:
    entry = _entry(hass)
    with _fetch(account(IVAN)):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state is ConfigEntryState.NOT_LOADED
