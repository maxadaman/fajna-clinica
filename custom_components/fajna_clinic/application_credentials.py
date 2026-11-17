"""Google's OAuth server, for the Calendar the visits are written to.

The client ID and secret are the person's own (Settings → Application credentials),
from a Google Cloud project with the Calendar API enabled.
"""

from __future__ import annotations

from homeassistant.components.application_credentials import AuthorizationServer
from homeassistant.core import HomeAssistant


async def async_get_authorization_server(hass: HomeAssistant) -> AuthorizationServer:
    return AuthorizationServer(
        authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
        token_url="https://oauth2.googleapis.com/token",
    )


async def async_get_description_placeholders(hass: HomeAssistant) -> dict[str, str]:
    return {
        "oauth_consent_url": "https://console.cloud.google.com/apis/credentials/consent",
        "oauth_creds_url": "https://console.cloud.google.com/apis/credentials",
        "calendar_api_url": "https://console.cloud.google.com/apis/library/calendar-json.googleapis.com",
    }
