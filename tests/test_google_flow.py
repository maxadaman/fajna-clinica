"""Linking Google Calendars, their settings, and the accounts writing to them."""

from __future__ import annotations

from collections.abc import Generator
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
    CONF_CARDS,
    CONF_KIND,
    CONF_PHONE,
    CONF_TOKEN,
    DOMAIN,
    GOOGLE_USERINFO_URL,
    KIND_ACCOUNT,
    KIND_GOOGLE,
)
from custom_components.fajna_clinic.google_calendar import (
    GoogleCalendarSync,
    Plan,
    person_key,
)
from custom_components.fajna_clinic.models import AccountData, PersonData, PlannedVisit

TOKEN_URL = "https://oauth2.googleapis.com/token"
CRED = "fajna_google"
ME = "me@gmail.com"
WIFE = "partner@example.com"
PHONE = "+380991112233"
IVAN = "Тестенко Іван Петрович"
MARIA = "Тестенко Марія Іванівна"
IVAN_CARD = person_key(PHONE, IVAN)
MARIA_CARD = person_key(PHONE, MARIA)


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


@pytest.fixture
def clinic() -> Generator[AsyncMock]:
    """The clinic's account, answering with two cards for as long as a test runs."""
    data = AccountData(persons={IVAN: PersonData(name=IVAN), MARIA: PersonData(name=MARIA)})
    fetch = AsyncMock(return_value=data)
    with patch("custom_components.fajna_clinic.api.FajnaClient.fetch_account", fetch):
        yield fetch


async def _load_account(hass: HomeAssistant) -> MockConfigEntry:
    account = MockConfigEntry(
        domain=DOMAIN,
        unique_id=PHONE,
        data={CONF_KIND: KIND_ACCOUNT, CONF_PHONE: PHONE, CONF_TOKEN: "t"},
    )
    account.add_to_hass(hass)
    assert await hass.config_entries.async_setup(account.entry_id)
    await hass.async_block_till_done()
    return account


def _offered(result: dict) -> tuple[list[str], list[str]]:
    """The cards the Google form offers, and those it has ticked to begin with."""
    key = next(k for k in result["data_schema"].schema if k == CONF_CARDS)
    selector = result["data_schema"].schema[key]
    return [o["value"] for o in selector.config["options"]], key.default()


@pytest.mark.usefixtures("credentials", "current_request_with_host", "clinic")
async def test_linking_google_asks_for_the_cards_calendar_and_invitees(
    hass: HomeAssistant, hass_client_no_auth, aioclient_mock: AiohttpClientMocker
) -> None:
    await _load_account(hass)
    result = await _link_google(hass, hass_client_no_auth, aioclient_mock)
    assert result["step_id"] == "google_settings"
    assert _offered(result) == ([IVAN_CARD, MARIA_CARD], [])

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_CARDS: [], CONF_CALENDAR_ID: "primary", CONF_ATTENDEES: "not-an-address"},
    )
    assert result["errors"] == {CONF_CARDS: "no_persons_chosen", CONF_ATTENDEES: "invalid_email"}

    with patch("custom_components.fajna_clinic.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_CARDS: [MARIA_CARD],
                CONF_CALENDAR_ID: "primary",
                CONF_ATTENDEES: f" {WIFE.upper()}, {WIFE} ",
            },
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == f"Google Calendar ({ME})"
    assert result["result"].unique_id == ME
    assert result["data"][CONF_KIND] == KIND_GOOGLE
    assert result["data"]["token"]["refresh_token"] == "refresh"
    assert result["options"] == {
        CONF_CARDS: [MARIA_CARD],
        CONF_CALENDAR_ID: "primary",
        CONF_ATTENDEES: [WIFE],
    }


@pytest.mark.usefixtures("credentials", "current_request_with_host", "clinic")
async def test_a_card_another_link_holds_is_not_offered(
    hass: HomeAssistant, hass_client_no_auth, aioclient_mock: AiohttpClientMocker
) -> None:
    await _load_account(hass)
    other = google_entry(**{CONF_CARDS: [IVAN_CARD]})
    other.add_to_hass(hass)
    hass.config_entries.async_update_entry(other, unique_id="partner@example.com")

    result = await _link_google(hass, hass_client_no_auth, aioclient_mock)

    assert _offered(result)[0] == [MARIA_CARD]


@pytest.mark.usefixtures("credentials")
async def test_google_needs_an_account_first(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "google"}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "add_account_first"


@pytest.mark.usefixtures("credentials", "clinic")
async def test_no_link_while_a_link_from_before_holds_every_card(hass: HomeAssistant) -> None:
    await _load_account(hass)
    google_entry().add_to_hass(hass)  # no cards chosen: it holds them all

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "google"}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_free_cards"


@pytest.mark.usefixtures("credentials", "clinic")
async def test_options_choose_the_cards_of_a_link_from_before(hass: HomeAssistant) -> None:
    await _load_account(hass)
    entry = google_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    # It held every card: all of them are ticked.
    assert _offered(result) == ([IVAN_CARD, MARIA_CARD], [IVAN_CARD, MARIA_CARD])
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_CARDS: [IVAN_CARD],
            CONF_CALENDAR_ID: "family@group.calendar.google.com",
            CONF_ATTENDEES: "",
        },
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {
        CONF_CARDS: [IVAN_CARD],
        CONF_CALENDAR_ID: "family@group.calendar.google.com",
        CONF_ATTENDEES: [],
    }


@pytest.mark.usefixtures("credentials", "clinic")
async def test_options_keep_a_card_of_an_account_that_is_down(hass: HomeAssistant) -> None:
    await _load_account(hass)
    away = person_key("+380671112233", "Тестенко Олег")
    entry = google_entry(**{CONF_CARDS: [IVAN_CARD, away]})
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_CARDS: [IVAN_CARD, MARIA_CARD], CONF_CALENDAR_ID: "primary", CONF_ATTENDEES: ""},
    )

    assert entry.options[CONF_CARDS] == [IVAN_CARD, MARIA_CARD, away]


async def test_each_kind_has_its_own_options(hass: HomeAssistant) -> None:
    account = MockConfigEntry(domain=DOMAIN, data={CONF_KIND: KIND_ACCOUNT})
    account.add_to_hass(hass)

    from custom_components.fajna_clinic.config_flow import (
        AccountOptionsFlow,
        FajnaConfigFlow,
        GoogleOptionsFlow,
    )

    assert isinstance(FajnaConfigFlow.async_get_options_flow(account), AccountOptionsFlow)
    assert isinstance(FajnaConfigFlow.async_get_options_flow(google_entry()), GoogleOptionsFlow)


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
async def test_every_linked_calendar_syncs_then_linked_guests_accept(hass: HomeAssistant) -> None:
    mine = google_entry(**{CONF_CARDS: [IVAN_CARD]})
    mine.add_to_hass(hass)
    hers = google_entry(**{CONF_CARDS: [MARIA_CARD]})
    hers.add_to_hass(hass)
    hass.config_entries.async_update_entry(hers, unique_id=WIFE)
    account = _account_entry()
    account.add_to_hass(hass)
    synced: list[str] = []
    event = {"iCalUID": "e1@google.com", "attendees": [{"email": ME}]}

    async def sync(self: GoogleCalendarSync, account: str, data, now) -> Plan:
        synced.append(self.email)
        return Plan(current=[event] if self.email == WIFE else [])

    with (
        patch(
            "custom_components.fajna_clinic.api.FajnaClient.fetch_account",
            AsyncMock(return_value=DATA),
        ),
        patch.object(GoogleCalendarSync, "async_sync_account", sync),
        patch(
            "custom_components.fajna_clinic.coordinator.async_accept_invitations",
            AsyncMock(return_value=1),
        ) as accept,
    ):
        assert await hass.config_entries.async_setup(mine.entry_id)
        await hass.async_block_till_done()

    assert {ME, WIFE} <= set(synced)
    events, syncs = accept.await_args.args
    assert events == [event]
    assert set(syncs) == {ME, WIFE}
    assert mine.runtime_data.holds(PHONE, IVAN)
    assert not mine.runtime_data.holds(PHONE, MARIA)


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
