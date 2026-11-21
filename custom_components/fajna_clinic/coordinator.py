"""Polls one account of the clinic, and passes its visits on to the linked Google Calendars."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import AuthExpired, FajnaClient, FajnaError
from .const import CONF_KIND, CONF_PERSONS, DOMAIN, KIND_GOOGLE, UPDATE_INTERVAL
from .google_calendar import GoogleCalendarSync, async_accept_invitations
from .models import AccountData

_LOGGER = logging.getLogger(__name__)

type FajnaConfigEntry = ConfigEntry[FajnaCoordinator]


class FajnaCoordinator(DataUpdateCoordinator[AccountData]):
    """Reads the chosen patient cards of one account every half hour."""

    config_entry: FajnaConfigEntry

    def __init__(self, hass: HomeAssistant, entry: FajnaConfigEntry, client: FajnaClient) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=UPDATE_INTERVAL,
        )
        self.client = client

    @property
    def persons(self) -> list[str] | None:
        """The cards chosen for this entry; None for an entry from before the choice."""
        return self.config_entry.options.get(CONF_PERSONS)

    async def _async_update_data(self) -> AccountData:
        try:
            data = await self.client.fetch_account(self.persons)
        except AuthExpired as err:
            # Signing in again needs an SMS code, so it is the person's step: HA asks them.
            raise ConfigEntryAuthFailed(str(err)) from err
        except FajnaError as err:
            raise UpdateFailed(str(err)) from err
        # Only a successful read reaches Google: a failed one would read as «no visits».
        await self._async_push_to_google(data)
        return data

    def _google(self) -> list[GoogleCalendarSync]:
        return [
            entry.runtime_data
            for entry in self.hass.config_entries.async_loaded_entries(DOMAIN)
            if entry.data.get(CONF_KIND) == KIND_GOOGLE
        ]

    async def _async_push_to_google(self, data: AccountData) -> None:
        """Each linked calendar gets its cards, then linked attendees answer their invites."""
        syncs = self._google()
        account = self.config_entry.unique_id
        now = dt_util.now()
        events: list[dict[str, Any]] = []
        for sync in syncs:
            try:
                events.extend((await sync.async_sync_account(account, data, now)).current)
            except Exception as err:
                # The calendar is a copy: its failure must not take the account's entities
                # down. The next refresh tries again, from the clinic's state then.
                _LOGGER.warning("Could not update Google Calendar %s: %s", sync.email, err)
        if not events:
            return
        try:
            accepted = await async_accept_invitations(events, {s.email: s for s in syncs})
        except Exception as err:
            # An invitation left unanswered is answered on the next refresh.
            _LOGGER.warning("Could not accept a Google Calendar invitation: %s", err)
            return
        if accepted:
            _LOGGER.info("Google Calendar: %d invitation(s) accepted", accepted)
