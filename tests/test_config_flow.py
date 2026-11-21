"""Adding an account and signing in again, with the site's client stubbed."""

from __future__ import annotations

from collections.abc import Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.fajna_clinic.api import CannotConnect, LoginRejected, UnexpectedPage
from custom_components.fajna_clinic.config_flow import normalize_phone
from custom_components.fajna_clinic.const import (
    CONF_KIND,
    CONF_PERSONS,
    CONF_PHONE,
    CONF_TOKEN,
    DOMAIN,
    KIND_ACCOUNT,
)

from .conftest import make_token

PHONE = "+380991112233"
TOKEN = make_token(name="Тестенко Іван Петрович")
IVAN = "Тестенко Іван Петрович"
MARIA = "Тестенко Марія Іванівна"
OLENA = "Тестенко Олена Іванівна"


@pytest.fixture
def client() -> Generator[MagicMock]:
    """The login as the site answers it: phone -> pin -> password -> signed in."""
    mock = MagicMock()
    mock.start_login = AsyncMock(return_value="phone")
    mock.submit = AsyncMock(side_effect=["pin", "password", None])
    mock.token = TOKEN
    mock.list_persons = AsyncMock(return_value=[IVAN])
    with (
        patch("custom_components.fajna_clinic.config_flow.create_client", return_value=mock),
        patch("custom_components.fajna_clinic.async_setup_entry", return_value=True),
    ):
        yield mock


async def _start(hass: HomeAssistant) -> dict:
    """The menu, then «add a patient account»: the phone form."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.MENU
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "account"}
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("+380 99 111 22 33", PHONE),
        ("0991112233", PHONE),
        ("380991112233", PHONE),
        ("099-111-22-33", PHONE),
        ("12345", None),
        ("+48 600 000 000", None),
    ],
)
def test_phone_is_normalized(raw: str, expected: str | None) -> None:
    assert normalize_phone(raw) == expected


async def test_full_login_creates_the_entry(hass: HomeAssistant, client: MagicMock) -> None:
    result = await _start(hass)
    assert result["step_id"] == "account"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PHONE: "0991112233"}
    )
    assert result["step_id"] == "pin"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"pin": " 1234 "})
    assert result["step_id"] == "password"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"password": "secret"}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Тестенко Іван Петрович"
    # The password is not kept: only the phone and the session.
    assert result["data"] == {CONF_KIND: KIND_ACCOUNT, CONF_PHONE: PHONE, CONF_TOKEN: TOKEN}
    # One card: nothing to choose, it is added.
    assert result["options"] == {CONF_PERSONS: [IVAN]}
    assert result["result"].unique_id == PHONE
    assert [c.args for c in client.submit.await_args_list] == [(PHONE,), ("1234",), ("secret",)]


async def test_a_wrong_code_asks_again(hass: HomeAssistant, client: MagicMock) -> None:
    client.submit.side_effect = ["pin", LoginRejected("pin"), "password", None]
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_PHONE: PHONE})

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"pin": "0000"})
    assert result["step_id"] == "pin"
    assert result["errors"] == {"base": "invalid_pin"}

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"pin": "1234"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"password": "secret"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_an_invalid_phone_is_refused_before_the_site(
    hass: HomeAssistant, client: MagicMock
) -> None:
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_PHONE: "123"})

    assert result["errors"] == {CONF_PHONE: "invalid_phone"}
    client.start_login.assert_not_awaited()


async def test_the_site_down_is_said(hass: HomeAssistant, client: MagicMock) -> None:
    client.start_login.side_effect = CannotConnect("down")
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_PHONE: PHONE})

    assert result["step_id"] == "account"
    assert result["errors"] == {"base": "cannot_connect"}


async def test_an_unknown_step_aborts(hass: HomeAssistant, client: MagicMock) -> None:
    client.submit.side_effect = ["captcha"]
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_PHONE: PHONE})

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "unsupported_login"


async def test_the_same_account_twice_is_refused(hass: HomeAssistant, client: MagicMock) -> None:
    MockConfigEntry(domain=DOMAIN, unique_id=PHONE, data={CONF_PHONE: PHONE}).add_to_hass(hass)
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_PHONE: PHONE})

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth_replaces_the_session(hass: HomeAssistant, client: MagicMock) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=PHONE,
        title="Тестенко Іван Петрович",
        data={CONF_PHONE: PHONE, CONF_TOKEN: "old"},
    )
    entry.add_to_hass(hass)

    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "pin"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"pin": "1234"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"password": "secret"}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data == {CONF_PHONE: PHONE, CONF_TOKEN: TOKEN}
    assert client.submit.await_args_list[0].args == (PHONE,)


async def _sign_in(hass: HomeAssistant, result: dict) -> dict:
    """From the phone form (or the reauth confirmation) through the code and password."""
    if result["step_id"] == "account":
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PHONE: PHONE}
        )
    else:
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"pin": "1234"})
    return await hass.config_entries.flow.async_configure(result["flow_id"], {"password": "secret"})


def _ticked(result: dict) -> list[str]:
    """What the card form has ticked to begin with."""
    (key,) = result["data_schema"].schema
    return key.default()


async def test_the_cards_to_add_are_chosen(hass: HomeAssistant, client: MagicMock) -> None:
    client.list_persons.return_value = [IVAN, MARIA, OLENA]
    result = await _sign_in(hass, await _start(hass))

    assert result["step_id"] == "persons"
    assert _ticked(result) == []
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_PERSONS: []})
    assert result["errors"] == {CONF_PERSONS: "no_persons_chosen"}

    # Kept in the account's order, whatever order they were ticked in.
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PERSONS: [OLENA, IVAN]}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["options"] == {CONF_PERSONS: [IVAN, OLENA]}


async def test_an_account_without_cards_is_not_added(
    hass: HomeAssistant, client: MagicMock
) -> None:
    client.list_persons.return_value = []
    result = await _sign_in(hass, await _start(hass))

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_persons"


def _reauth_entry(hass: HomeAssistant, **options) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=PHONE,
        data={CONF_PHONE: PHONE, CONF_TOKEN: "old"},
        options=options,
    )
    entry.add_to_hass(hass)
    return entry


async def test_reauth_keeps_the_cards_when_sent_as_is(
    hass: HomeAssistant, client: MagicMock
) -> None:
    client.list_persons.return_value = [IVAN, MARIA, OLENA]
    entry = _reauth_entry(hass, **{CONF_PERSONS: [MARIA]})

    result = await _sign_in(hass, await entry.start_reauth_flow(hass))
    assert result["step_id"] == "reauth_persons"
    assert _ticked(result) == [MARIA]
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PERSONS: _ticked(result)}
    )

    assert result["reason"] == "reauth_successful"
    assert entry.options == {CONF_PERSONS: [MARIA]}
    assert entry.data[CONF_TOKEN] == TOKEN


async def test_reauth_can_change_the_cards(hass: HomeAssistant, client: MagicMock) -> None:
    client.list_persons.return_value = [IVAN, MARIA]
    entry = _reauth_entry(hass, **{CONF_PERSONS: [MARIA]})

    result = await _sign_in(hass, await entry.start_reauth_flow(hass))
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PERSONS: [IVAN, MARIA]}
    )

    assert result["reason"] == "reauth_successful"
    assert entry.options == {CONF_PERSONS: [IVAN, MARIA]}


async def test_reauth_of_an_entry_from_before_the_choice_ticks_every_card(
    hass: HomeAssistant, client: MagicMock
) -> None:
    client.list_persons.return_value = [IVAN, MARIA]
    entry = _reauth_entry(hass)

    result = await _sign_in(hass, await entry.start_reauth_flow(hass))

    assert result["step_id"] == "reauth_persons"
    assert _ticked(result) == [IVAN, MARIA]


async def test_reauth_saves_the_session_when_the_cards_cannot_be_read(
    hass: HomeAssistant, client: MagicMock
) -> None:
    client.list_persons.side_effect = UnexpectedPage("changed")
    entry = _reauth_entry(hass, **{CONF_PERSONS: [MARIA]})

    result = await _sign_in(hass, await entry.start_reauth_flow(hass))

    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_TOKEN] == TOKEN
    assert entry.options == {CONF_PERSONS: [MARIA]}


async def test_options_change_the_cards(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=PHONE,
        data={CONF_KIND: KIND_ACCOUNT, CONF_PHONE: PHONE, CONF_TOKEN: "t"},
        options={CONF_PERSONS: [IVAN]},
    )
    entry.add_to_hass(hass)
    with (
        patch(
            "custom_components.fajna_clinic.api.FajnaClient.list_persons",
            AsyncMock(return_value=[IVAN, MARIA]),
        ),
        patch("custom_components.fajna_clinic.api.FajnaClient.fetch_account", AsyncMock()),
        patch("custom_components.fajna_clinic.PLATFORMS", []),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        result = await hass.config_entries.options.async_init(entry.entry_id)
        assert result["step_id"] == "persons"
        assert _ticked(result) == [IVAN]
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_PERSONS: [MARIA]}
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {CONF_PERSONS: [MARIA]}


async def test_options_need_a_working_account(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=PHONE, data={CONF_KIND: KIND_ACCOUNT, CONF_PHONE: PHONE}
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "account_not_loaded"
