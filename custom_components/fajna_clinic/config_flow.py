"""Adding to the integration: a clinic account, or the Google Calendar visits go to.

**An account** signs in the way the site's own form does, step by step — phone, SMS code,
password. The password is posted to the site once and never stored. What is kept is the
session cookie, and when the site ends that session HA asks for the steps again (reauth):
signing in needs a fresh SMS code, so it cannot happen without the person.

**Google Calendar** is HA's own OAuth with the person's application credentials, then the
calendar to write to and who is invited to every visit. One link serves every account.
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
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.config_entry_oauth2_flow import AbstractOAuth2FlowHandler
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from . import create_client, entry_kind
from .api import CannotConnect, FajnaClient, FajnaError, LoginRejected, decode_token
from .const import (
    CONF_ATTENDEES,
    CONF_CALENDAR_ID,
    CONF_KIND,
    CONF_PHONE,
    CONF_TOKEN,
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

_LOGGER = logging.getLogger(__name__)

_PHONE_SCHEMA = vol.Schema(
    {vol.Required(CONF_PHONE): TextSelector(TextSelectorConfig(type=TextSelectorType.TEL))}
)
_PIN_SCHEMA = vol.Schema({vol.Required(FIELD_PIN): str})
_PASSWORD_SCHEMA = vol.Schema(
    {vol.Required(FIELD_PASSWORD): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))}
)
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


def _google_schema(calendar_id: str, attendees: list[str]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_CALENDAR_ID, default=calendar_id): str,
            vol.Optional(CONF_ATTENDEES, default=", ".join(attendees)): str,
        }
    )


def _read_google_settings(
    user_input: dict[str, Any], errors: dict[str, str]
) -> dict[str, Any] | None:
    attendees = parse_attendees(user_input.get(CONF_ATTENDEES, ""))
    if attendees is None:
        errors[CONF_ATTENDEES] = "invalid_email"
        return None
    calendar_id = user_input[CONF_CALENDAR_ID].strip() or DEFAULT_CALENDAR_ID
    return {CONF_CALENDAR_ID: calendar_id, CONF_ATTENDEES: attendees}


class FajnaConfigFlow(AbstractOAuth2FlowHandler, domain=DOMAIN):
    """An account per phone number; one Google Calendar, keyed by its address."""

    DOMAIN = DOMAIN
    VERSION = 1

    def __init__(self) -> None:
        super().__init__()
        self._client: FajnaClient | None = None
        self._phone: str | None = None
        self._google: dict[str, Any] | None = None

    @property
    def logger(self) -> logging.Logger:
        return _LOGGER

    @property
    def extra_authorize_data(self) -> dict[str, Any]:
        # Offline and consent, or Google gives no refresh token and the link dies in an hour.
        return {"scope": " ".join(GOOGLE_SCOPES), "access_type": "offline", "prompt": "consent"}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> GoogleOptionsFlow:
        return GoogleOptionsFlow()

    @classmethod
    @callback
    def async_supports_options_flow(cls, config_entry: ConfigEntry) -> bool:
        return entry_kind(config_entry) == KIND_GOOGLE

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
        assert self._client is not None and self._client.token and self._phone
        token = self._client.token
        if self.source == SOURCE_REAUTH:
            return self.async_update_reload_and_abort(
                self._get_reauth_entry(), data_updates={CONF_TOKEN: token}
            )
        try:
            title = decode_token(token).get("name") or self._phone
        except FajnaError:
            title = self._phone
        return self.async_create_entry(
            title=str(title),
            data={CONF_KIND: KIND_ACCOUNT, CONF_PHONE: self._phone, CONF_TOKEN: token},
        )

    # ------------------------------------------------------------------ google

    async def async_step_google(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if any(entry_kind(e) == KIND_GOOGLE for e in self._async_current_entries()):
            # Every account writes to the one calendar: a second link would be a second copy.
            return self.async_abort(reason="google_already_configured")
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
            settings = _read_google_settings(user_input, errors)
            if settings is not None:
                assert self._google is not None
                return self.async_create_entry(
                    title=f"Google Calendar ({self.unique_id})",
                    data=self._google,
                    options=settings,
                )
        return self.async_show_form(
            step_id="google_settings",
            data_schema=_google_schema(DEFAULT_CALENDAR_ID, []),
            errors=errors,
        )


class GoogleOptionsFlow(OptionsFlowWithReload):
    """Which calendar, and who is invited — changeable without linking Google again."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            settings = _read_google_settings(user_input, errors)
            if settings is not None:
                return self.async_create_entry(data=settings)
        options = self.config_entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=_google_schema(
                options.get(CONF_CALENDAR_ID, DEFAULT_CALENDAR_ID),
                options.get(CONF_ATTENDEES, []),
            ),
            errors=errors,
        )
