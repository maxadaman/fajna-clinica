"""Signing in again by itself: the SMS passed on through the service, or HA asking the person."""

from __future__ import annotations

import asyncio
from collections.abc import Generator
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.core import HomeAssistant

from custom_components.fajna_clinic.api import AuthExpired, FajnaClient, LoginRejected
from custom_components.fajna_clinic.const import (
    CONF_PASSWORD,
    CONF_PHONE,
    CONF_TOKEN,
    CONF_TOKEN_AT,
    DOMAIN,
)
from custom_components.fajna_clinic.models import AccountData, PersonData
from custom_components.fajna_clinic.relogin import extract_code

PHONE = "+380991112233"
SMS = "Ваш код для входу в кабінет Файна Клініка: 482913. Нікому не повідомляйте його."
IVAN = PersonData(name="Тестенко Іван Петрович")


@pytest.mark.parametrize(
    ("message", "code"),
    [
        (SMS, "482913"),
        ("Код: 1234", "1234"),
        ("1234", "1234"),
        ("Дзвоніть +380991112233, код 5678", "5678"),
        ("Без коду", None),
    ],
)
def test_the_code_is_read_from_the_sms(message: str, code: str | None) -> None:
    assert extract_code(message) == code


class FakeSite:
    """The site's login: phone, then the SMS code, then the password."""

    def __init__(self, *, refuse: str | None = None) -> None:
        self.answers: list[str] = []
        self.refuse = refuse

    async def start_login(self, client: FajnaClient) -> str:
        client._token = None
        return "phone"

    async def submit(self, client: FajnaClient, value: str) -> str | None:
        step = ("phone", "pin", "password")[len(self.answers)]
        if step == self.refuse:
            raise LoginRejected(step)
        self.answers.append(value)
        if step == "password":
            client._token = "new"
            return None
        return {"phone": "pin", "pin": "password"}[step]


@pytest.fixture
def site() -> Generator[FakeSite]:
    fake = FakeSite()

    async def start_login(client: FajnaClient) -> str:
        return await fake.start_login(client)

    async def submit(client: FajnaClient, value: str) -> str | None:
        return await fake.submit(client, value)

    with (
        patch.object(FajnaClient, "start_login", start_login),
        patch.object(FajnaClient, "submit", submit),
        patch("custom_components.fajna_clinic.PLATFORMS", []),
    ):
        yield fake


def _entry(hass: HomeAssistant, password: str | None = "secret") -> MockConfigEntry:
    data = {CONF_PHONE: PHONE, CONF_TOKEN: "old", CONF_TOKEN_AT: "2026-11-20T10:00:00+00:00"}
    if password:
        data[CONF_PASSWORD] = password
    entry = MockConfigEntry(domain=DOMAIN, unique_id=PHONE, title="Іван", data=data)
    entry.add_to_hass(hass)
    return entry


def _fetch(*results):
    return patch(
        "custom_components.fajna_clinic.api.FajnaClient.fetch_account",
        AsyncMock(side_effect=list(results)),
    )


def _reauths(hass: HomeAssistant) -> list:
    return [
        f for f in hass.config_entries.flow.async_progress() if f["context"]["source"] == "reauth"
    ]


async def _settle(hass: HomeAssistant) -> None:
    """Let the sign-in run up to its wait for the SMS. It is a background task, which
    `async_block_till_done` does not wait for, and waiting for it would wait out the SMS."""
    await hass.async_block_till_done()
    for _ in range(20):
        await asyncio.sleep(0)
    await hass.async_block_till_done()


async def _submit_sms(hass: HomeAssistant, message: str = SMS) -> None:
    await hass.services.async_call(DOMAIN, "submit_sms", {"message": message}, blocking=True)
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_a_dead_session_signs_in_again_with_the_sms(
    hass: HomeAssistant, site: FakeSite
) -> None:
    entry = _entry(hass)
    with _fetch(AuthExpired("login page"), AccountData(persons={IVAN.name: IVAN})):
        await hass.config_entries.async_setup(entry.entry_id)
        await _settle(hass)
        # Waiting for the code: not an error for the person yet.
        assert entry.state is ConfigEntryState.SETUP_RETRY
        assert not _reauths(hass)
        assert site.answers == [PHONE]

        await _submit_sms(hass)

    assert site.answers == [PHONE, "482913", "secret"]
    assert entry.data[CONF_TOKEN] == "new"
    assert entry.data[CONF_TOKEN_AT] != "2026-11-20T10:00:00+00:00"
    assert entry.data[CONF_PASSWORD] == "secret"
    assert entry.state is ConfigEntryState.LOADED
    assert not _reauths(hass)


async def test_a_session_ending_while_loaded_signs_in_again(
    hass: HomeAssistant, site: FakeSite
) -> None:
    entry = _entry(hass)
    data = AccountData(persons={IVAN.name: IVAN})
    with _fetch(data, AuthExpired("login page"), data):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        # Without entities the coordinator does not poll by itself: the half-hour read.
        await entry.runtime_data.async_refresh()
        await _settle(hass)
        assert entry.runtime_data.last_update_success is False
        assert site.answers == [PHONE]

        await _submit_sms(hass)

    assert entry.data[CONF_TOKEN] == "new"
    assert entry.state is ConfigEntryState.LOADED


async def test_no_sms_in_time_asks_the_person(hass: HomeAssistant, site: FakeSite) -> None:
    entry = _entry(hass)
    with (
        patch("custom_components.fajna_clinic.relogin.SMS_TIMEOUT", timedelta(milliseconds=1)),
        _fetch(AuthExpired("login page"), AuthExpired("login page")),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
        assert [f["context"]["source"] for f in _reauths(hass)] == [SOURCE_REAUTH]

        # The next try does not text again: HA waits for the person.
        await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()

    assert site.answers == [PHONE]
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert entry.data[CONF_TOKEN] == "old"
    assert entry.data[CONF_PASSWORD] == "secret"


async def test_a_refused_password_is_forgotten(hass: HomeAssistant, site: FakeSite) -> None:
    site.refuse = "password"
    entry = _entry(hass)
    with _fetch(AuthExpired("login page")):
        await hass.config_entries.async_setup(entry.entry_id)
        await _settle(hass)
        await _submit_sms(hass)

    assert CONF_PASSWORD not in entry.data
    assert entry.data[CONF_TOKEN] == "old"
    assert len(_reauths(hass)) == 1


async def test_without_a_password_the_person_is_asked_at_once(
    hass: HomeAssistant, site: FakeSite
) -> None:
    entry = _entry(hass, password=None)
    with _fetch(AuthExpired("login page")):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert site.answers == []
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert len(_reauths(hass)) == 1


async def test_an_sms_nobody_waits_for_is_ignored(hass: HomeAssistant, site: FakeSite) -> None:
    entry = _entry(hass)
    with _fetch(AccountData(persons={IVAN.name: IVAN})):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    await _submit_sms(hass)
    await _submit_sms(hass, "Без коду")

    assert site.answers == []
    assert entry.data[CONF_TOKEN] == "old"
