"""Constants for the Fajna Clinic integration."""

from __future__ import annotations

from datetime import timedelta
from zoneinfo import ZoneInfo

DOMAIN = "fajna_clinic"
MANUFACTURER = "Файна Клініка"

BASE_URL = "https://fajna.clinic"
LOGIN_PATH = "/account/login"
ACCOUNT_PATH = "/account"

# The session lives in this one cookie (a JWT without `exp`); nothing else is needed.
SESSION_COOKIE = "accountQuery"

# The clinic is in Kyiv, and the page writes times without a zone.
CLINIC_TZ = ZoneInfo("Europe/Kyiv")

# The page gives a visit's start only; a calendar event needs an end.
DEFAULT_VISIT_DURATION = timedelta(hours=1)

UPDATE_INTERVAL = timedelta(minutes=30)

CONF_PHONE = "phone"
CONF_TOKEN = "token"

# One domain, two kinds of entry: a clinic account, and the Google Calendar the accounts'
# visits are written to. An entry without a kind is an account (the first version's).
CONF_KIND = "kind"
KIND_ACCOUNT = "account"
KIND_GOOGLE = "google"

CONF_CALENDAR_ID = "calendar_id"
CONF_ATTENDEES = "attendees"
DEFAULT_CALENDAR_ID = "primary"

GOOGLE_SCOPES = (
    "https://www.googleapis.com/auth/calendar.events",
    # Which Google account was linked: the entry is keyed by its address.
    "https://www.googleapis.com/auth/userinfo.email",
)
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"

# The page names no branch, so an event says the clinic and nothing more precise.
EVENT_LOCATION = "Файна Клініка"

# The login form's own field names, one per step: phone -> SMS code -> password.
FIELD_PHONE = "phone"
FIELD_PIN = "pin"
FIELD_PASSWORD = "password"
