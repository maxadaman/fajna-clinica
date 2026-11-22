"""Adding to the integration: a clinic account, or the Google Calendar visits go to.

**An account** signs in the way the site's own form does, step by step — phone, SMS code,
password. The password is posted to the site and not stored. What is kept is the
session cookie, and when the site ends that session HA asks for the steps again (reauth):
signing in needs a fresh SMS code. The account's options can store the password so that
it signs in by itself, with the SMS passed on to HA (see `relogin.py`).

One phone can hold several patient cards (a family's). Once signed in, the person picks
which of them to add; signing in again shows the same choice, already ticked, and the
account's options change it later.

**Google Calendar** is HA's own OAuth with the person's application credentials, then the
patient cards whose visits it holds, the calendar to write to, and who is invited to every
visit. One link per Google account; a card is in one link at most, so no visit is written
twice.
"""

from __future__ import annotations

from collections.abc import Mapping
import logging
import re
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    SOURCE_REAUTH,
    ConfigEntry,
    ConfigEntryState,
    ConfigFlowResult,
    OptionsFlow,
    OptionsFlowWithReload,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.config_entry_oauth2_flow import AbstractOAuth2FlowHandler
from homeassistant.helpers.selector import (
    BooleanSelector,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from homeassistant.util import dt as dt_util

from . import create_client, entry_kind
from .api import (
    AuthExpired,
    CannotConnect,
    FajnaClient,
    FajnaError,
    LoginRejected,
    decode_token,
)
from .const import (
    CONF_ATTENDEES,
    CONF_CALENDAR_ID,
    CONF_CARDS,
    CONF_KIND,
    CONF_PASSWORD,
    CONF_PERSONS,
    CONF_PHONE,
    CONF_TOKEN,
    CONF_TOKEN_AT,
    DEFAULT_CALENDAR_ID,
    DOMAIN,
    FIELD_PASSWORD,
    FIELD_PHONE,
    FIELD_PIN,
    GOOGLE_SCOPES,
    GOOGLE_USERINFO_URL,
    KIND_ACCOUNT,
    KIND_GOOGLE,
)
from .google_calendar import person_key
from .relogin import SMS_TIMEOUT

_LOGGER = logging.getLogger(__name__)

_PHONE_SCHEMA = vol.Schema(
    {vol.Required(CONF_PHONE): TextSelector(TextSelectorConfig(type=TextSelectorType.TEL))}
)
_PIN_SCHEMA = vol.Schema({vol.Required(FIELD_PIN): str})
_PASSWORD_SCHEMA = vol.Schema(
    {vol.Required(FIELD_PASSWORD): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))}
)
CONF_AUTO_RELOGIN = "auto_relogin"
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def normalize_phone(raw: str) -> str | None:
    """`+380XXXXXXXXX`, from whatever a person types: `0XX…`, `380XX…`, spaces, dashes."""
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 10 and digits.startswith("0"):
        digits = "38" + digits
    if len(digits) == 12 and digits.startswith("380"):
        return "+" + digits
    return None


def parse_attendees(raw: str) -> list[str] | None:
    """Addresses separated by commas, semicolons or spaces; None if one is not an address."""
    emails = [part.strip().lower() for part in re.split(r"[,;\s]+", raw) if part.strip()]
    if any(not _EMAIL.match(email) for email in emails):
        return None
    return sorted(set(emails))


def _persons_schema(persons: list[str], chosen: list[str]) -> vol.Schema:
    """Ticks, one per card on the account, `chosen` already ticked."""
    return vol.Schema(
        {
            vol.Required(CONF_PERSONS, default=[p for p in chosen if p in persons]): SelectSelector(
                SelectSelectorConfig(
                    options=persons, multiple=True, mode=SelectSelectorMode.LIST, sort=False
                )
            )
        }
    )


def _read_persons(
    persons: list[str], user_input: dict[str, Any], errors: dict[str, str]
) -> list[str] | None:
    """The ticked cards in the account's order; None, with the error, if there are none."""
    chosen = [p for p in persons if p in user_input.get(CONF_PERSONS, [])]
    if not chosen:
        errors[CONF_PERSONS] = "no_persons_chosen"
        return None
    return chosen


def known_cards(hass: HomeAssistant) -> dict[str, str]:
    """The cards of the accounts that are up, by `person_key`: their names."""
    cards: dict[str, str] = {}
    for entry in hass.config_entries.async_loaded_entries(DOMAIN):
        if entry_kind(entry) == KIND_ACCOUNT and entry.runtime_data.data:
            for name in entry.runtime_data.data.persons:
                cards[person_key(str(entry.unique_id), name)] = name
    return cards


def free_cards(hass: HomeAssistant, cards: dict[str, str], entry_id: str | None) -> dict[str, str]:
    """The cards no other Google link holds. A link from before the choice holds them all."""
    taken: set[str] = set()
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry_kind(entry) == KIND_GOOGLE and entry.entry_id != entry_id:
            held = entry.options.get(CONF_CARDS)
            taken |= set(cards) if held is None else set(held)
    return {key: name for key, name in cards.items() if key not in taken}


def _google_schema(
    calendar_id: str, attendees: list[str], cards: dict[str, str], chosen: list[str]
) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_CARDS, default=[c for c in chosen if c in cards]): SelectSelector(
                SelectSelectorConfig(
                    options=[SelectOptionDict(value=k, label=v) for k, v in cards.items()],
                    multiple=True,
                    mode=SelectSelectorMode.LIST,
                    sort=False,
                )
            ),
            vol.Required(CONF_CALENDAR_ID, default=calendar_id): str,
            vol.Optional(CONF_ATTENDEES, default=", ".join(attendees)): str,
        }
    )


def _read_google_settings(
    cards: dict[str, str], user_input: dict[str, Any], errors: dict[str, str]
) -> dict[str, Any] | None:
    chosen = [key for key in cards if key in user_input.get(CONF_CARDS, [])]
    if not chosen:
        errors[CONF_CARDS] = "no_persons_chosen"
    attendees = parse_attendees(user_input.get(CONF_ATTENDEES, ""))
    if attendees is None:
        errors[CONF_ATTENDEES] = "invalid_email"
    if errors:
        return None
    calendar_id = user_input[CONF_CALENDAR_ID].strip() or DEFAULT_CALENDAR_ID
    return {CONF_CARDS: chosen, CONF_CALENDAR_ID: calendar_id, CONF_ATTENDEES: attendees}


class FajnaConfigFlow(AbstractOAuth2FlowHandler, domain=DOMAIN):
    """An account per phone number; one Google Calendar, keyed by its address."""

    DOMAIN = DOMAIN
    VERSION = 1

    def __init__(self) -> None:
        super().__init__()
        self._client: FajnaClient | None = None
        self._phone: str | None = None
        self._persons: list[str] = []
        self._google: dict[str, Any] | None = None
        self._cards: dict[str, str] = {}

    @property
    def logger(self) -> logging.Logger:
        return _LOGGER

    @property
    def extra_authorize_data(self) -> dict[str, Any]:
        # Offline and consent, or Google gives no refresh token and the link dies in an hour.
        return {"scope": " ".join(GOOGLE_SCOPES), "access_type": "offline", "prompt": "consent"}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        if entry_kind(config_entry) == KIND_GOOGLE:
            return GoogleOptionsFlow()
        return AccountOptionsFlow()

    # ------------------------------------------------------------------ entry points

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(step_id="user", menu_options=["account", "google"])

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        if entry_data.get(CONF_KIND) == KIND_GOOGLE:
            return await self.async_step_reauth_google()
        self._phone = entry_data[CONF_PHONE]
        return await self.async_step_reauth_confirm()

    # ------------------------------------------------------------------ account

    async def async_step_account(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            phone = normalize_phone(user_input[CONF_PHONE])
            if phone is None:
                errors[CONF_PHONE] = "invalid_phone"
            else:
                await self.async_set_unique_id(phone)
                self._abort_if_unique_id_configured()
                self._phone = phone
                return await self._async_begin(errors)
        return self.async_show_form(step_id="account", data_schema=_PHONE_SCHEMA, errors=errors)

    async def async_step_pin(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self._async_answer(FIELD_PIN, _PIN_SCHEMA, user_input)

    async def async_step_password(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await self._async_answer(FIELD_PASSWORD, _PASSWORD_SCHEMA, user_input)

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return await self._async_begin({})
        return self.async_show_form(
            step_id="reauth_confirm", description_placeholders={"phone": self._phone or ""}
        )

    async def _async_begin(self, errors: dict[str, str]) -> ConfigFlowResult:
        """Open the site's login and send the phone: the site texts a code."""
        assert self._phone is not None
        self._client = create_client(self.hass)
        try:
            field = await self._client.start_login()
            if field != FIELD_PHONE:
                _LOGGER.warning("The login starts with '%s', not the phone", field)
                return self.async_abort(reason="unsupported_login")
            return await self._async_next(await self._client.submit(self._phone))
        except LoginRejected:
            errors["base"] = "invalid_phone"
        except CannotConnect:
            errors["base"] = "cannot_connect"
        except FajnaError:
            _LOGGER.exception("Unexpected answer from the clinic's login")
            errors["base"] = "unknown"
        if self.source == SOURCE_REAUTH:
            return self.async_show_form(
                step_id="reauth_confirm",
                description_placeholders={"phone": self._phone},
                errors=errors,
            )
        return self.async_show_form(step_id="account", data_schema=_PHONE_SCHEMA, errors=errors)

    async def _async_answer(
        self, field: str, schema: vol.Schema, user_input: dict[str, Any] | None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            assert self._client is not None
            try:
                return await self._async_next(await self._client.submit(user_input[field].strip()))
            except LoginRejected:
                errors["base"] = f"invalid_{field}"
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except FajnaError:
                _LOGGER.exception("Unexpected answer from the clinic's login")
                errors["base"] = "unknown"
        return self.async_show_form(
            step_id=field,
            data_schema=schema,
            description_placeholders={"phone": self._phone or ""},
            errors=errors,
        )

    async def _async_next(self, field: str | None) -> ConfigFlowResult:
        """The step the site asked for next, or the entry once it has signed us in."""
        if field == FIELD_PIN:
            return await self.async_step_pin()
        if field == FIELD_PASSWORD:
            return await self.async_step_password()
        if field is not None:
            _LOGGER.warning("The login asked for an unknown field '%s'", field)
            return self.async_abort(reason="unsupported_login")
        return await self._async_finish_account()

    async def _async_finish_account(self) -> ConfigFlowResult:
        """Signed in: which cards the account has, then which of them to add."""
        assert self._client is not None
        reauth = self.source == SOURCE_REAUTH
        try:
            self._persons = await self._client.list_persons()
        except FajnaError as err:
            if reauth:
                # The new session matters more than the choice: keep the choice as it was.
                _LOGGER.warning("Signed in again, but could not list the cards: %s", err)
                return self._async_save_account(None)
            if isinstance(err, CannotConnect):
                return self.async_abort(reason="cannot_connect")
            _LOGGER.exception("Unexpected answer from the clinic's account")
            return self.async_abort(reason="unknown")
        if not self._persons:
            if reauth:
                return self._async_save_account(None)
            return self.async_abort(reason="no_persons")
        if len(self._persons) == 1:
            # Nothing to choose from.
            return self._async_save_account(self._persons)
        return await (self.async_step_reauth_persons() if reauth else self.async_step_persons())

    async def async_step_persons(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self._async_choose_persons("persons", [], user_input)

    async def async_step_reauth_persons(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        # The entry's cards ticked, so sending the form as it is changes nothing.
        # An entry from before the choice read every card: all of them are ticked.
        chosen = self._get_reauth_entry().options.get(CONF_PERSONS, self._persons)
        return self._async_choose_persons("reauth_persons", chosen, user_input)

    def _async_choose_persons(
        self, step_id: str, chosen: list[str], user_input: dict[str, Any] | None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            persons = _read_persons(self._persons, user_input, errors)
            if persons is not None:
                return self._async_save_account(persons)
        return self.async_show_form(
            step_id=step_id,
            data_schema=_persons_schema(self._persons, chosen),
            description_placeholders={"phone": self._phone or ""},
            errors=errors,
        )

    def _async_save_account(self, persons: list[str] | None) -> ConfigFlowResult:
        """The entry, with the session and the chosen cards (None: leave them as they are)."""
        assert self._client is not None and self._client.token and self._phone
        token = self._client.token
        token_at = dt_util.utcnow().isoformat()
        if self.source == SOURCE_REAUTH:
            entry = self._get_reauth_entry()
            options = entry.options if persons is None else {**entry.options, CONF_PERSONS: persons}
            return self.async_update_reload_and_abort(
                entry, data_updates={CONF_TOKEN: token, CONF_TOKEN_AT: token_at}, options=options
            )
        try:
            title = decode_token(token).get("name") or self._phone
        except FajnaError:
            title = self._phone
        return self.async_create_entry(
            title=str(title),
            data={
                CONF_KIND: KIND_ACCOUNT,
                CONF_PHONE: self._phone,
                CONF_TOKEN: token,
                CONF_TOKEN_AT: token_at,
            },
            options={CONF_PERSONS: persons},
        )

    # ------------------------------------------------------------------ google

    async def async_step_google(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        # Asked before Google's consent, so nobody signs in to be told there is nothing to do.
        cards = known_cards(self.hass)
        if not cards:
            return self.async_abort(reason="add_account_first")
        self._cards = free_cards(self.hass, cards, None)
        if not self._cards:
            # A card in two calendars would be a visit written twice.
            return self.async_abort(reason="no_free_cards")
        return await self.async_step_pick_implementation()

    async def async_step_reauth_google(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is None:
            return self.async_show_form(step_id="reauth_google")
        return await self.async_step_pick_implementation()

    async def async_oauth_create_entry(self, data: dict[str, Any]) -> ConfigFlowResult:
        session = async_get_clientsession(self.hass)
        headers = {"Authorization": f"Bearer {data['token']['access_token']}"}
        async with session.get(GOOGLE_USERINFO_URL, headers=headers) as resp:
            if resp.status != 200:
                _LOGGER.error("Google did not say which account was linked: %s", resp.status)
                return self.async_abort(reason="oauth_error")
            email = (await resp.json()).get("email", "").lower()
        await self.async_set_unique_id(email)

        data = {CONF_KIND: KIND_GOOGLE, **data}
        if self.source == SOURCE_REAUTH:
            self._abort_if_unique_id_mismatch(reason="wrong_google_account")
            return self.async_update_reload_and_abort(self._get_reauth_entry(), data=data)
        self._abort_if_unique_id_configured()
        self._google = data
        return await self.async_step_google_settings()

    async def async_step_google_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            settings = _read_google_settings(self._cards, user_input, errors)
            if settings is not None:
                assert self._google is not None
                return self.async_create_entry(
                    title=f"Google Calendar ({self.unique_id})",
                    data=self._google,
                    options=settings,
                )
        return self.async_show_form(
            step_id="google_settings",
            data_schema=_google_schema(DEFAULT_CALENDAR_ID, [], self._cards, []),
            errors=errors,
        )


class GoogleOptionsFlow(OptionsFlowWithReload):
    """Whose cards, which calendar, who is invited — changeable without linking again."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        options = self.config_entry.options
        known = known_cards(self.hass)
        cards = free_cards(self.hass, known, self.config_entry.entry_id)
        held = options.get(CONF_CARDS)
        if user_input is not None:
            settings = _read_google_settings(cards, user_input, errors)
            if settings is not None:
                # A card of an account that is down right now is not offered, but kept.
                unseen = [key for key in held or [] if key not in known]
                settings[CONF_CARDS] = [*settings[CONF_CARDS], *unseen]
                return self.async_create_entry(data=settings)
        return self.async_show_form(
            step_id="init",
            data_schema=_google_schema(
                options.get(CONF_CALENDAR_ID, DEFAULT_CALENDAR_ID),
                options.get(CONF_ATTENDEES, []),
                cards,
                # A link from before the choice held every card: all of them are ticked.
                list(cards) if held is None else held,
            ),
            errors=errors,
        )


class AccountOptionsFlow(OptionsFlowWithReload):
    """Which of the account's cards are in Home Assistant, and whether it signs in by itself."""

    def __init__(self) -> None:
        self._persons: list[str] = []

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        # Not «init»: that step's texts are the Google link's options.
        return await self.async_step_account()

    async def async_step_account(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(step_id="account", menu_options=["cards", "auto_relogin"])

    async def async_step_auto_relogin(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """The password kept for signing in by itself, or forgotten. Not checked here:
        checking means signing in, and that means an SMS code."""
        entry = self.config_entry
        stored = entry.data.get(CONF_PASSWORD)
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {k: v for k, v in entry.data.items() if k != CONF_PASSWORD}
            if user_input[CONF_AUTO_RELOGIN]:
                # Left empty, the field keeps the password already stored.
                password = user_input.get(CONF_PASSWORD, "").strip() or stored
                if password:
                    data[CONF_PASSWORD] = password
                else:
                    errors[CONF_PASSWORD] = "password_required"
            if not errors:
                self.hass.config_entries.async_update_entry(entry, data=data)
                return self.async_create_entry(data=dict(entry.options))
        return self.async_show_form(
            step_id="auto_relogin",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_AUTO_RELOGIN, default=bool(stored)): BooleanSelector(),
                    vol.Optional(CONF_PASSWORD): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD)
                    ),
                }
            ),
            description_placeholders={
                "service": f"{DOMAIN}.submit_sms",
                "minutes": str(int(SMS_TIMEOUT.total_seconds() // 60)),
            },
            errors=errors,
        )

    async def async_step_cards(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """The cards the account lists now, read through the entry's own session."""
        entry = self.config_entry
        if entry.state is not ConfigEntryState.LOADED:
            return self.async_abort(reason="account_not_loaded")
        try:
            self._persons = await entry.runtime_data.client.list_persons()
        except AuthExpired:
            return self.async_abort(reason="account_not_loaded")
        except CannotConnect:
            return self.async_abort(reason="cannot_connect")
        except FajnaError:
            _LOGGER.exception("Unexpected answer from the clinic's account")
            return self.async_abort(reason="unknown")
        if not self._persons:
            return self.async_abort(reason="no_persons")
        return await self.async_step_persons()

    async def async_step_persons(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self.config_entry
        errors: dict[str, str] = {}
        if user_input is not None:
            persons = _read_persons(self._persons, user_input, errors)
            if persons is not None:
                return self.async_create_entry(data={**entry.options, CONF_PERSONS: persons})
        return self.async_show_form(
            step_id="persons",
            data_schema=_persons_schema(
                self._persons, entry.options.get(CONF_PERSONS, self._persons)
            ),
            errors=errors,
        )
