"""The Fajna Clinic integration: a patient account's visits in Home Assistant.

Two kinds of entry share the domain: a clinic account (one per phone number), and a Google
account whose calendar holds the visits of the patient cards chosen for it (one per Google
account).
"""

from __future__ import annotations

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.config_entry_oauth2_flow import (
    OAuth2Session,
    async_get_config_entry_implementation,
)

from .api import FajnaClient
from .const import (
    CONF_ATTENDEES,
    CONF_CALENDAR_ID,
    CONF_CARDS,
    CONF_KIND,
    CONF_PERSONS,
    CONF_TOKEN,
    DEFAULT_CALENDAR_ID,
    DOMAIN,
    KIND_ACCOUNT,
    KIND_GOOGLE,
)
from .coordinator import FajnaConfigEntry, FajnaCoordinator
from .google_calendar import GoogleCalendarApi, GoogleCalendarSync

PLATFORMS: list[Platform] = [Platform.CALENDAR, Platform.SENSOR]

type GoogleConfigEntry = ConfigEntry[GoogleCalendarSync]


def entry_kind(entry: ConfigEntry) -> str:
    return entry.data.get(CONF_KIND, KIND_ACCOUNT)


def create_client(hass: HomeAssistant, token: str | None = None) -> FajnaClient:
    """A client on a session of its own that keeps no cookies.

    The client holds the account's cookie itself. HA's shared session has one cookie jar
    for everything, where a second account's login would overwrite the first one's.
    Created during entry setup, the session is closed when the entry unloads.
    """
    session = async_create_clientsession(hass, cookie_jar=aiohttp.DummyCookieJar())
    return FajnaClient(session, token)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if entry_kind(entry) == KIND_GOOGLE:
        return await _async_setup_google(hass, entry)
    coordinator = FajnaCoordinator(hass, entry, create_client(hass, entry.data[CONF_TOKEN]))
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    _remove_unchosen_devices(hass, entry)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


def _remove_unchosen_devices(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """A card taken out of the choice takes its device, and so its entities, with it."""
    persons = entry.options.get(CONF_PERSONS)
    if persons is None:
        return
    kept = {(DOMAIN, f"{entry.unique_id}_{person}") for person in persons}
    registry = dr.async_get(hass)
    for device in dr.async_entries_for_config_entry(registry, entry.entry_id):
        if not device.identifiers & kept:
            registry.async_update_device(device.id, remove_config_entry_id=entry.entry_id)


async def _async_setup_google(hass: HomeAssistant, entry: GoogleConfigEntry) -> bool:
    implementation = await async_get_config_entry_implementation(hass, entry)
    session = OAuth2Session(hass, entry, implementation)
    api = GoogleCalendarApi(
        session.async_request, entry.options.get(CONF_CALENDAR_ID, DEFAULT_CALENDAR_ID)
    )
    entry.runtime_data = GoogleCalendarSync(
        api,
        entry.options.get(CONF_ATTENDEES, []),
        email=entry.unique_id or "",
        persons=entry.options.get(CONF_CARDS),
    )
    # The accounts sync on their next refresh; asking now makes a new link work at once.
    for account in hass.config_entries.async_loaded_entries(DOMAIN):
        if entry_kind(account) == KIND_ACCOUNT:
            await account.runtime_data.async_request_refresh()
    return True


async def async_unload_entry(hass: HomeAssistant, entry: FajnaConfigEntry) -> bool:
    if entry_kind(entry) == KIND_GOOGLE:
        return True
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
