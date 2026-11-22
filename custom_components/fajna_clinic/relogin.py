"""Signing an account in again without the person, when they have allowed it.

The clinic ends a session after a while, and signing in needs a code it texts to the phone.
With the account's password stored (the account's options) and the SMS passed on to Home
Assistant — an iPhone's Shortcuts automation calling `fajna_clinic.submit_sms` — the steps
can run by themselves: send the phone, wait for the code, send it and the password.

It runs in the background, not in the coordinator's refresh: the wait for an SMS is minutes.
The state lives per entry in `hass.data`, apart from the coordinator, because a dead session
found while the entry is set up makes HA retry the setup with a new coordinator each time.

When it cannot finish — no code in time, a code or password refused — HA asks the person
(reauth), as it did before. A refused password is forgotten, so it is not tried again.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import logging
import re

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.util import dt as dt_util

from .api import FajnaClient, FajnaError, LoginRejected
from .const import (
    CONF_PASSWORD,
    CONF_PHONE,
    CONF_TOKEN,
    CONF_TOKEN_AT,
    DOMAIN,
    FIELD_PASSWORD,
    FIELD_PHONE,
    FIELD_PIN,
)

_LOGGER = logging.getLogger(__name__)

# How long to wait for the SMS to be passed on.
SMS_TIMEOUT = timedelta(minutes=5)
# Each attempt makes the clinic send an SMS: not more often than this, whatever happens.
MIN_INTERVAL = timedelta(hours=1)

# The code in the SMS: the first group of 4 to 6 digits standing on its own.
_CODE = re.compile(r"(?<!\d)(\d{4,6})(?!\d)")


def extract_code(message: str) -> str | None:
    match = _CODE.search(message)
    return match.group(1) if match else None


@callback
def async_get_relogin(hass: HomeAssistant, entry: ConfigEntry) -> Relogin:
    managers: dict[str, Relogin] = hass.data.setdefault(DOMAIN, {})
    if entry.entry_id not in managers:
        managers[entry.entry_id] = Relogin(hass, entry)
    return managers[entry.entry_id]


@callback
def async_remove_relogin(hass: HomeAssistant, entry_id: str) -> None:
    managers: dict[str, Relogin] = hass.data.get(DOMAIN, {})
    if (manager := managers.pop(entry_id, None)) is not None:
        manager.cancel()


@callback
def async_deliver_sms(hass: HomeAssistant, message: str) -> int:
    """Pass an SMS to the accounts waiting for one. Answers how many took its code."""
    code = extract_code(message)
    if code is None:
        _LOGGER.info("The SMS passed on has no code in it")
        return 0
    managers: dict[str, Relogin] = hass.data.get(DOMAIN, {})
    taken = sum(manager.deliver(code) for manager in managers.values())
    if not taken:
        _LOGGER.info("An SMS code was passed on, but no account is signing in")
    return taken


class Relogin:
    """One account's automatic sign-in: at most one at a time, and not too often."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._hass = hass
        self._entry = entry
        self._task: asyncio.Task[None] | None = None
        self._code: asyncio.Future[str] | None = None
        self._last_start: datetime | None = None
        # Set when an attempt fails: the person signs in, and a working session clears it.
        self._failed = False

    @property
    def enabled(self) -> bool:
        return bool(self._entry.data.get(CONF_PASSWORD))

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def can_start(self) -> bool:
        if not self.enabled or self._failed or self.running:
            return False
        return self._last_start is None or dt_util.utcnow() - self._last_start >= MIN_INTERVAL

    @callback
    def session_works(self) -> None:
        """A read succeeded: whatever failed before is behind us."""
        self._failed = False

    @callback
    def start(self) -> None:
        self._last_start = dt_util.utcnow()
        self._task = self._hass.async_create_background_task(
            self._async_run(), f"{DOMAIN} sign-in of {self._entry.title}"
        )

    @callback
    def deliver(self, code: str) -> bool:
        if self._code is None or self._code.done():
            return False
        self._code.set_result(code)
        return True

    @callback
    def cancel(self) -> None:
        if self._task is not None:
            self._task.cancel()

    async def _async_run(self) -> None:
        entry = self._entry
        _LOGGER.info("The session of %s has ended: signing in again", entry.title)
        session = async_create_clientsession(
            self._hass, cookie_jar=aiohttp.DummyCookieJar(), auto_cleanup=False
        )
        try:
            token = await self._async_sign_in(FajnaClient(session))
        except Exception as err:
            self._failed = True
            if isinstance(err, TimeoutError):
                _LOGGER.warning(
                    "No SMS code for %s within %s: sign in from the notification",
                    entry.title,
                    SMS_TIMEOUT,
                )
            elif isinstance(err, LoginRejected) and err.args == (FIELD_PASSWORD,):
                _LOGGER.warning(
                    "The clinic refused the stored password of %s: it is forgotten", entry.title
                )
                data = {k: v for k, v in entry.data.items() if k != CONF_PASSWORD}
                self._hass.config_entries.async_update_entry(entry, data=data)
            elif isinstance(err, FajnaError):
                _LOGGER.warning("Could not sign %s in again: %r", entry.title, err)
            else:
                _LOGGER.exception("Could not sign %s in again", entry.title)
            entry.async_start_reauth(self._hass)
            return
        finally:
            self._code = None
            await session.close()

        _LOGGER.info("Signed %s in again", entry.title)
        self._hass.config_entries.async_update_entry(
            entry,
            data={**entry.data, CONF_TOKEN: token, CONF_TOKEN_AT: dt_util.utcnow().isoformat()},
        )
        # The entry's client holds the old session: a reload gives it the new one.
        self._hass.config_entries.async_schedule_reload(entry.entry_id)

    async def _async_sign_in(self, client: FajnaClient) -> str:
        """The site's steps, in whatever order it asks for them. Answers the new session."""
        entry = self._entry
        self._code = self._hass.loop.create_future()
        field = await client.start_login()
        answered: set[str] = set()
        while field is not None:
            if field in answered:
                # The site asking twice for a step means it went round: do not loop.
                raise FajnaError(f"the login asked for '{field}' again")
            answered.add(field)
            if field == FIELD_PHONE:
                value = entry.data[CONF_PHONE]
            elif field == FIELD_PIN:
                async with asyncio.timeout(SMS_TIMEOUT.total_seconds()):
                    value = await self._code
            elif field == FIELD_PASSWORD:
                value = entry.data[CONF_PASSWORD]
            else:
                raise FajnaError(f"the login asked for an unknown field '{field}'")
            field = await client.submit(value)
        assert client.token
        return client.token
