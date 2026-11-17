"""Booked visits, kept in step with a Google Calendar.

One event per visit, in one calendar, with the attendees invited to it: Google shows it in
their calendars as well, and a change or a cancellation reaches all of them at once.
Invitations are sent without email (`sendUpdates=none`).

Each event carries the visit's id and its account in private extended properties. That,
not a local list, is what says which events are ours — so nothing is created twice, even
after the integration is reinstalled, and nothing the person wrote themselves is touched.

Only what is still to come is synced. A visit that took place leaves the clinic's
"planned" list; its event stays in the calendar as the record that it happened.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
import json
import logging
from typing import Any
from urllib.parse import quote

from aiohttp import ClientResponse

from .const import CLINIC_TZ, DEFAULT_VISIT_DURATION, EVENT_LOCATION
from .models import AccountData, PlannedVisit

_LOGGER = logging.getLogger(__name__)

_API = "https://www.googleapis.com/calendar/v3/calendars/{calendar}/events"
_PROP_UID = "fajna_uid"
_PROP_ACCOUNT = "fajna_account"
# Without email: the event simply appears in the attendees' calendars.
_QUIET = {"sendUpdates": "none"}

type Requester = Callable[..., Awaitable[ClientResponse]]


class GoogleApiError(Exception):
    """Google answered a request with an error."""


# --------------------------------------------------------------------------- pure


def first_name(full_name: str) -> str:
    """`Прізвище Ім'я По батькові` → `Ім'я`; a name of one word stays as it is."""
    words = full_name.split()
    return words[1] if len(words) >= 2 else full_name.strip()


def event_body(
    account: str, person: str, visit: PlannedVisit, attendees: Iterable[str]
) -> dict[str, Any]:
    """The Google event for a visit: `[Ім'я] Послуга`, at the clinic, an hour long."""
    details = [
        ("Лікар", visit.doctor),
        ("Інструкції", visit.instructions),
        ("Оплата", visit.payment),
    ]
    end = visit.start + DEFAULT_VISIT_DURATION
    return {
        "summary": f"[{first_name(person)}] {visit.service or 'Візит'}",
        "location": EVENT_LOCATION,
        "description": "\n".join(f"{label}: {value}" for label, value in details if value),
        "start": {"dateTime": visit.start.isoformat(), "timeZone": str(CLINIC_TZ)},
        "end": {"dateTime": end.isoformat(), "timeZone": str(CLINIC_TZ)},
        "attendees": [{"email": email} for email in sorted(set(attendees))],
        "extendedProperties": {"private": {_PROP_UID: visit.uid, _PROP_ACCOUNT: account}},
    }


def _when(event: Mapping[str, Any], edge: str) -> datetime | None:
    raw = (event.get(edge) or {}).get("dateTime")
    return datetime.fromisoformat(raw) if raw else None


def _differs(event: Mapping[str, Any], body: Mapping[str, Any]) -> bool:
    if any(event.get(key, "") != body[key] for key in ("summary", "location", "description")):
        return True
    if any(_when(event, edge) != _when(body, edge) for edge in ("start", "end")):
        return True
    emails = {a.get("email", "").lower() for a in event.get("attendees", [])}
    return emails != {a["email"].lower() for a in body["attendees"]}


@dataclass(slots=True)
class Plan:
    """What one sync changes."""

    insert: list[dict[str, Any]] = field(default_factory=list)
    update: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    delete: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.insert or self.update or self.delete)


def plan_sync(
    wanted: Mapping[str, dict[str, Any]], existing: Iterable[Mapping[str, Any]], now: datetime
) -> Plan:
    """Turn the calendar's events of an account into the visits it should show.

    `wanted` is the visits' bodies by visit id; `existing` the account's events that have
    not ended yet. An event whose visit is gone is deleted only before it starts: one
    under way is the visit happening, which the clinic may already have moved to history.
    """
    plan = Plan()
    seen: set[str] = set()
    for event in existing:
        uid = event.get("extendedProperties", {}).get("private", {}).get(_PROP_UID)
        body = wanted.get(uid) if uid else None
        if body is None or uid in seen:
            # Not a visit any more, or a second event for one (a sync cut short).
            start = _when(event, "start")
            if start is not None and start > now:
                plan.delete.append(event["id"])
            continue
        seen.add(uid)
        if _differs(event, body):
            plan.update.append((event["id"], body))
    plan.insert = [body for uid, body in wanted.items() if uid not in seen]
    return plan


# --------------------------------------------------------------------------- api


class GoogleCalendarApi:
    """The few Calendar API calls a sync needs, over HA's OAuth session."""

    def __init__(self, request: Requester, calendar_id: str) -> None:
        self._request = request
        self._url = _API.format(calendar=quote(calendar_id, safe=""))

    async def _call(self, method: str, url: str, **kwargs: Any) -> dict[str, Any] | None:
        resp = await self._request(method, url, **kwargs)
        text = await resp.text()  # read whole, so the connection goes back to the pool
        if method == "DELETE" and resp.status == 410:
            return None  # already deleted
        if resp.status >= 400:
            raise GoogleApiError(f"{method} {resp.status}: {text[:300]}")
        return json.loads(text) if text else None

    async def account_events(self, account: str, time_min: datetime) -> list[dict[str, Any]]:
        """The account's events that end after `time_min`."""
        events: list[dict[str, Any]] = []
        params: dict[str, str] = {
            "privateExtendedProperty": f"{_PROP_ACCOUNT}={account}",
            "timeMin": time_min.isoformat(),
            "singleEvents": "true",
            "maxResults": "250",
        }
        while True:
            page = await self._call("GET", self._url, params=params) or {}
            events.extend(page.get("items", []))
            if not (token := page.get("nextPageToken")):
                return events
            params["pageToken"] = token

    async def insert(self, body: dict[str, Any]) -> None:
        await self._call("POST", self._url, params=_QUIET, json=body)

    async def update(self, event_id: str, body: dict[str, Any]) -> None:
        await self._call("PATCH", f"{self._url}/{quote(event_id)}", params=_QUIET, json=body)

    async def delete(self, event_id: str) -> None:
        await self._call("DELETE", f"{self._url}/{quote(event_id)}", params=_QUIET)


# --------------------------------------------------------------------------- sync


class GoogleCalendarSync:
    """Writes every account's booked visits to one calendar."""

    def __init__(self, api: GoogleCalendarApi, attendees: Iterable[str]) -> None:
        self._api = api
        self._attendees = tuple(attendees)
        # Two accounts refreshing at once must not interleave their requests.
        self._lock = asyncio.Lock()

    async def async_sync_account(self, account: str, data: AccountData, now: datetime) -> Plan:
        wanted = {
            visit.uid: event_body(account, person.name, visit, self._attendees)
            for person in data.persons.values()
            for visit in person.planned
            # Already over, though the clinic still lists it: not ours to add any more.
            if visit.start + DEFAULT_VISIT_DURATION > now
        }
        async with self._lock:
            plan = plan_sync(wanted, await self._api.account_events(account, now), now)
            for body in plan.insert:
                await self._api.insert(body)
            for event_id, body in plan.update:
                await self._api.update(event_id, body)
            for event_id in plan.delete:
                await self._api.delete(event_id)
        if plan:
            _LOGGER.info(
                "Google Calendar: %d added, %d changed, %d removed",
                len(plan.insert),
                len(plan.update),
                len(plan.delete),
            )
        return plan
