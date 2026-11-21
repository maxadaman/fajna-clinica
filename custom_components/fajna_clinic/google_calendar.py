"""Booked visits, kept in step with Google Calendar.

Each linked Google account holds the visits of the patient cards chosen for it (an entry
from before the choice holds every card). One event per visit, in that calendar, with the
attendees invited to it: Google shows it in their calendars as well, and a change or a
cancellation reaches all of them at once. Invitations are sent without email
(`sendUpdates=none`).

An attendee who has linked their own Google account here answers the invitation at once:
the integration accepts it on their behalf, in their main calendar, unless they have
answered it themselves.

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
# Invitations land in the attendee's main calendar, whichever one their visits go to.
_INVITES = _API.format(calendar="primary")
_PROP_UID = "fajna_uid"
_PROP_ACCOUNT = "fajna_account"
# Without email: the event simply appears in the attendees' calendars.
_QUIET = {"sendUpdates": "none"}

type Requester = Callable[..., Awaitable[ClientResponse]]


class GoogleApiError(Exception):
    """Google answered a request with an error."""


# --------------------------------------------------------------------------- pure


def person_key(account: str, person: str) -> str:
    """A card across every account: the account's phone and the card's full name."""
    return f"{account}:{person}"


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
    # The account's events in the calendar, as Google answered them (with the attendees'
    # answers): the ones left as they are; a sync adds the ones it wrote.
    current: list[dict[str, Any]] = field(default_factory=list)

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
        else:
            plan.current.append(dict(event))
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

    async def insert(self, body: dict[str, Any]) -> dict[str, Any]:
        return await self._call("POST", self._url, params=_QUIET, json=body) or {}

    async def update(self, event_id: str, body: dict[str, Any]) -> dict[str, Any]:
        url = f"{self._url}/{quote(event_id)}"
        return await self._call("PATCH", url, params=_QUIET, json=body) or {}

    async def delete(self, event_id: str) -> None:
        await self._call("DELETE", f"{self._url}/{quote(event_id)}", params=_QUIET)

    async def accept(self, ical_uid: str) -> bool:
        """Accept, as this account, the invitation to an event in its main calendar.

        False when there is nothing to accept: the invitation has not arrived yet, or it
        has been answered already (an answer of the person's own is theirs to keep).
        """
        page = await self._call("GET", _INVITES, params={"iCalUID": ical_uid}) or {}
        accepted = False
        for event in page.get("items", []):
            me = next((a for a in event.get("attendees", []) if a.get("self")), None)
            if me is None or me.get("responseStatus", "needsAction") != "needsAction":
                continue
            # With the attendees left out, only our own answer is changed.
            body = {
                "attendeesOmitted": True,
                "attendees": [{"email": me["email"], "responseStatus": "accepted"}],
            }
            url = f"{_INVITES}/{quote(event['id'])}"
            await self._call("PATCH", url, params=_QUIET, json=body)
            accepted = True
        return accepted


# --------------------------------------------------------------------------- sync


class GoogleCalendarSync:
    """Writes the booked visits of its patient cards to one Google account's calendar."""

    def __init__(
        self,
        api: GoogleCalendarApi,
        attendees: Iterable[str],
        email: str = "",
        persons: Iterable[str] | None = None,
    ) -> None:
        self._api = api
        self._attendees = tuple(attendees)
        self.email = email.lower()
        # The cards (`person_key`) this calendar holds; None: every card, as before.
        self._persons = None if persons is None else frozenset(persons)
        # Two accounts refreshing at once must not interleave their requests.
        self._lock = asyncio.Lock()

    def holds(self, account: str, person: str) -> bool:
        return self._persons is None or person_key(account, person) in self._persons

    async def async_sync_account(self, account: str, data: AccountData, now: datetime) -> Plan:
        """Write the account's cards this calendar holds; the rest of its events go.

        The plan's `current` ends up as every event of the account in the calendar, written
        or not, as Google answered it — the invitations to answer are read there.
        """
        wanted = {
            visit.uid: event_body(account, person.name, visit, self._attendees)
            for person in data.persons.values()
            if self.holds(account, person.name)
            for visit in person.planned
            # Already over, though the clinic still lists it: not ours to add any more.
            if visit.start + DEFAULT_VISIT_DURATION > now
        }
        async with self._lock:
            plan = plan_sync(wanted, await self._api.account_events(account, now), now)
            written = [await self._api.insert(body) for body in plan.insert]
            for event_id, body in plan.update:
                written.append(await self._api.update(event_id, body))
            for event_id in plan.delete:
                await self._api.delete(event_id)
            plan.current.extend(written)
        if plan:
            _LOGGER.info(
                "Google Calendar: %d added, %d changed, %d removed",
                len(plan.insert),
                len(plan.update),
                len(plan.delete),
            )
        return plan

    async def async_accept(self, ical_uid: str) -> bool:
        async with self._lock:
            return await self._api.accept(ical_uid)


async def async_accept_invitations(
    events: Iterable[Mapping[str, Any]], syncs: Mapping[str, GoogleCalendarSync]
) -> int:
    """Answer, for every linked attendee, the invitations they have not answered yet.

    `events` are as the organizer's calendar answers them, so each attendee's answer is
    there to read; `syncs` are the linked Google accounts by address. Returns how many
    invitations were accepted.
    """
    accepted = 0
    for event in events:
        ical_uid = event.get("iCalUID")
        if not ical_uid:
            continue
        for attendee in event.get("attendees", []):
            sync = syncs.get(attendee.get("email", "").lower())
            if sync is None or attendee.get("self") or attendee.get("organizer"):
                continue
            if attendee.get("responseStatus", "needsAction") != "needsAction":
                continue
            if await sync.async_accept(ical_uid):
                accepted += 1
    return accepted
