"""Linking Google Calendar, its settings, and the accounts writing to it."""

from __future__ import annotations

from datetime import datetime
import time
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from homeassistant.components.application_credentials import (
    ClientCredential,
    async_import_client_credential,
)
from homeassistant.config_entries import SOURCE_USER, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import config_entry_oauth2_flow
from homeassistant.setup import async_setup_component

from custom_components.fajna_clinic.const import (
    CLINIC_TZ,
    CONF_ATTENDEES,
    CONF_CALENDAR_ID,
    CONF_KIND,
    CONF_PHONE,
    CONF_TOKEN,
    DOMAIN,
    GOOGLE_USERINFO_URL,
    KIND_ACCOUNT,
    KIND_GOOGLE,
)
from custom_components.fajna_clinic.google_calendar import GoogleCalendarSync
from custom_components.fajna_clinic.models import AccountData, PersonData, PlannedVisit

TOKEN_URL = "https://oauth2.googleapis.com/token"
CRED = "fajna_google"
ME = "me@gmail.com"
WIFE = "partner@example.com"
PHONE = "+380991112233"


@pytest.fixture
async def credentials(hass: HomeAssistant) -> None:
    assert await async_setup_component(hass, "application_credentials", {})
    await async_import_client_credential(hass, DOMAIN, ClientCredential("id", "secret"), CRED)


def _token() -> dict:
    return {
        "access_token": "access",
        "refresh_token": "refresh",
        "expires_in": 3600,
        "expires_at": time.time() + 3600,
        "token_type": "Bearer",
    }


def google_entry(**options) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=ME,
        data={CONF_KIND: KIND_GOOGLE, "auth_implementation": CRED, "token": _token()},
        options={CONF_CALENDAR_ID: "primary", CONF_ATTENDEES: [WIFE], **options},
    )


async def _link_google(hass, hass_client_no_auth, aioclient_mock: AiohttpClientMocker) -> dict:
    """The menu → Google → Google's consent page → back with a code."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "google"}
    )
    assert result["type"] is FlowResultType.EXTERNAL_STEP
    assert "access_type=offline" in result["url"]
    assert "calendar.events" in result["url"]

    state = config_entry_oauth2_flow._encode_jwt(
        hass,
        {
            "flow_id": result["flow_id"],
            "redirect_uri": "https://example.com/auth/external/callback",
        },
    )
    client = await hass_client_no_auth()
    resp = await client.get(f"/auth/external/callback?code=abcd&state={state}")
    assert resp.status == 200

    aioclient_mock.post(TOKEN_URL, json=_token())
    aioclient_mock.get(GOOGLE_USERINFO_URL, json={"email": "Me@Gmail.com"})
    return await hass.config_entries.flow.async_configure(result["flow_id"])


@pytest.mark.usefixtures("credentials", "current_request_with_host")
async def test_linking_google_asks_for_the_calendar_and_invitees(
    hass: HomeAssistant, hass_client_no_auth, aioclient_mock: AiohttpClientMocker
) -> None:
    result = await _link_google(hass, hass_client_no_auth, aioclient_mock)
    assert result["step_id"] == "google_settings"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CALENDAR_ID: "primary", CONF_ATTENDEES: "not-an-address"}
    )
    assert result["errors"] == {CONF_ATTENDEES: "invalid_email"}

    with patch("custom_components.fajna_clinic.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_CALENDAR_ID: "primary", CONF_ATTENDEES: f" {WIFE.upper()}, {WIFE} "},
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == f"Google Calendar ({ME})"
    assert result["result"].unique_id == ME
    assert result["data"][CONF_KIND] == KIND_GOOGLE
    assert result["data"]["token"]["refresh_token"] == "refresh"
    assert result["options"] == {CONF_CALENDAR_ID: "primary", CONF_ATTENDEES: [WIFE]}


@pytest.mark.usefixtures("credentials")
async def test_google_is_linked_once(hass: HomeAssistant) -> None:
    google_entry().add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "google"}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "google_already_configured"


@pytest.mark.usefixtures("credentials")
async def test_options_change_the_invitees(hass: HomeAssistant) -> None:
    entry = google_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_CALENDAR_ID: "family@group.calendar.google.com", CONF_ATTENDEES: ""},
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {
        CONF_CALENDAR_ID: "family@group.calendar.google.com",
        CONF_ATTENDEES: [],
    }


async def test_an_account_has_no_options(hass: HomeAssistant) -> None:
    account = MockConfigEntry(domain=DOMAIN, data={CONF_KIND: KIND_ACCOUNT})
    account.add_to_hass(hass)

    from custom_components.fajna_clinic.config_flow import FajnaConfigFlow

    assert not FajnaConfigFlow.async_supports_options_flow(account)
    assert FajnaConfigFlow.async_supports_options_flow(google_entry())


# --------------------------------------------------------------------------- accounts → Google


VISIT = PlannedVisit(datetime(2026, 11, 27, 18, 40, tzinfo=CLINIC_TZ), "Масаж спини", "Олег")
DATA = AccountData(persons={"Тестенко Іван": PersonData(name="Тестенко Іван", planned=[VISIT])})


def _account_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=PHONE,
        data={CONF_KIND: KIND_ACCOUNT, CONF_PHONE: PHONE, CONF_TOKEN: "t"},
    )


@pytest.mark.usefixtures("credentials")
async def test_each_refresh_of_an_account_syncs_it_to_google(hass: HomeAssistant) -> None:
    google = google_entry()
    google.add_to_hass(hass)
    account = _account_entry()
    account.add_to_hass(hass)

    with (
        patch(
            "custom_components.fajna_clinic.api.FajnaClient.fetch_account",
            AsyncMock(return_value=DATA),
        ),
        patch.object(GoogleCalendarSync, "async_sync_account", AsyncMock()) as sync,
    ):
        # Setting the domain up loads both entries.
        assert await hass.config_entries.async_setup(google.entry_id)
        await hass.async_block_till_done()

    assert google.state is ConfigEntryState.LOADED
    assert account.state is ConfigEntryState.LOADED
    assert isinstance(google.runtime_data, GoogleCalendarSync)
    assert sync.await_args.args[:2] == (PHONE, DATA)


@pytest.mark.usefixtures("credentials")
async def test_google_failing_does_not_take_the_account_down(hass: HomeAssistant) -> None:
    google = google_entry()
    google.add_to_hass(hass)
    account = _account_entry()
    account.add_to_hass(hass)

    with (
        patch(
            "custom_components.fajna_clinic.api.FajnaClient.fetch_account",
            AsyncMock(return_value=DATA),
        ),
        patch.object(
            GoogleCalendarSync, "async_sync_account", AsyncMock(side_effect=RuntimeError("quota"))
        ),
    ):
        # Setting the domain up loads both entries.
        assert await hass.config_entries.async_setup(google.entry_id)
        await hass.async_block_till_done()

    assert account.state is ConfigEntryState.LOADED
    assert account.runtime_data.last_update_success


async def test_without_google_an_account_works_as_before(hass: HomeAssistant) -> None:
    account = _account_entry()
    account.add_to_hass(hass)

    with patch(
        "custom_components.fajna_clinic.api.FajnaClient.fetch_account",
        AsyncMock(return_value=DATA),
    ):
        assert await hass.config_entries.async_setup(account.entry_id)
        await hass.async_block_till_done()

    assert account.state is ConfigEntryState.LOADED
