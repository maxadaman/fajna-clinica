"""Visits kept in step with Google Calendar: what an event says, and what a sync changes."""

from __future__ import annotations

from datetime import datetime, timedelta
import json
from typing import Any
from urllib.parse import unquote

import pytest

from custom_components.fajna_clinic.const import CLINIC_TZ
from custom_components.fajna_clinic.google_calendar import (
    GoogleApiError,
    GoogleCalendarApi,
    GoogleCalendarSync,
    async_accept_invitations,
    event_body,
    first_name,
    person_key,
    plan_sync,
)
from custom_components.fajna_clinic.models import AccountData, PersonData, PlannedVisit

ACCOUNT = "+380991112233"
WIFE = "partner@example.com"
NOW = datetime(2026, 11, 20, 12, 0, tzinfo=CLINIC_TZ)

MASSAGE = PlannedVisit(
    datetime(2026, 11, 27, 18, 40, tzinfo=CLINIC_TZ), "Масаж спини", "Лікаренко Петро", "", ""
)
THERAPIST = PlannedVisit(
    datetime(2026, 12, 2, 9, 5, tzinfo=CLINIC_TZ), "Консультація", "Анна", "Натщесерце", "500"
)


def stored(body: dict[str, Any], event_id: str) -> dict[str, Any]:
    """An event as Google answers it: the body plus an id, times in another offset form."""
    event = json.loads(json.dumps(body))
    event["id"] = event_id
    for edge in ("start", "end"):
        moment = datetime.fromisoformat(event[edge]["dateTime"]).astimezone(CLINIC_TZ)
        event[edge] = {"dateTime": moment.isoformat(), "timeZone": "Europe/Kyiv"}
    if not event["description"]:
        del event["description"]  # Google leaves an empty field out
    return event


# --------------------------------------------------------------------------- event


@pytest.mark.parametrize(
    ("full", "first"),
    [
        ("Тестенко Іван Петрович", "Іван"),
        ("Тестенко Марія Іванівна", "Марія"),
        ("Тестенко Олег", "Олег"),
        ("Олег", "Олег"),
    ],
)
def test_first_name_is_the_second_word(full: str, first: str) -> None:
    assert first_name(full) == first


def test_event_names_the_patient_and_invites_quietly() -> None:
    body = event_body(ACCOUNT, "Тестенко Марія Іванівна", THERAPIST, [WIFE, WIFE])

    assert body["summary"] == "[Марія] Консультація"
    assert body["location"] == "Файна Клініка"
    assert body["description"] == "Лікар: Анна\nІнструкції: Натщесерце\nОплата: 500"
    assert body["start"] == {"dateTime": "2026-12-02T09:05:00+02:00", "timeZone": "Europe/Kyiv"}
    assert body["end"]["dateTime"] == "2026-12-02T10:05:00+02:00"
    assert body["attendees"] == [{"email": WIFE}]
    assert body["extendedProperties"]["private"] == {
        "fajna_uid": THERAPIST.uid,
        "fajna_account": ACCOUNT,
    }


def test_event_without_details_has_an_empty_description() -> None:
    assert (
        event_body(ACCOUNT, "Тестенко Іван", MASSAGE, [])["description"] == "Лікар: Лікаренко Петро"
    )


# --------------------------------------------------------------------------- plan


def _wanted(*visits: PlannedVisit, attendees=(WIFE,)) -> dict[str, dict[str, Any]]:
    return {v.uid: event_body(ACCOUNT, "Тестенко Іван", v, attendees) for v in visits}


def test_new_visits_are_inserted() -> None:
    wanted = _wanted(MASSAGE, THERAPIST)

    plan = plan_sync(wanted, [], NOW)

    assert plan.insert == list(wanted.values())
    assert not plan.update and not plan.delete


def test_an_unchanged_event_is_left_alone() -> None:
    wanted = _wanted(MASSAGE)

    assert not plan_sync(wanted, [stored(wanted[MASSAGE.uid], "e1")], NOW)


def test_a_changed_detail_is_updated() -> None:
    before = _wanted(MASSAGE, attendees=())
    after = _wanted(MASSAGE)  # the wife was added to the invitees since

    plan = plan_sync(after, [stored(before[MASSAGE.uid], "e1")], NOW)

    assert plan.update == [("e1", after[MASSAGE.uid])]
    assert not plan.insert and not plan.delete


def test_a_cancelled_visit_is_deleted_before_it_starts() -> None:
    old = _wanted(MASSAGE)

    plan = plan_sync({}, [stored(old[MASSAGE.uid], "e1")], NOW)

    assert plan.delete == ["e1"]


def test_a_visit_under_way_is_not_deleted() -> None:
    old = _wanted(MASSAGE)
    during = MASSAGE.start + timedelta(minutes=10)

    assert not plan_sync({}, [stored(old[MASSAGE.uid], "e1")], during)


def test_a_rescheduled_visit_replaces_its_event() -> None:
    moved = PlannedVisit(MASSAGE.start + timedelta(days=1), MASSAGE.service, MASSAGE.doctor)
    old = _wanted(MASSAGE)
    new = _wanted(moved)

    plan = plan_sync(new, [stored(old[MASSAGE.uid], "e1")], NOW)

    assert plan.insert == [new[moved.uid]]
    assert plan.delete == ["e1"]


def test_a_second_event_for_one_visit_is_removed() -> None:
    wanted = _wanted(MASSAGE)
    body = wanted[MASSAGE.uid]

    plan = plan_sync(wanted, [stored(body, "e1"), stored(body, "e2")], NOW)

    assert plan.delete == ["e2"]
    assert not plan.insert and not plan.update


def test_an_event_that_is_not_ours_is_never_touched() -> None:
    mine = {"id": "x", "summary": "Day off", "start": {"date": "2026-11-28"}}

    assert not plan_sync({}, [mine], NOW)


# --------------------------------------------------------------------------- api


class FakeResponse:
    def __init__(self, status: int, payload: Any = None) -> None:
        self.status = status
        self._text = json.dumps(payload) if payload is not None else ""

    async def text(self) -> str:
        return self._text


class FakeGoogle:
    """A calendar in memory, answering the Calendar API the way Google does."""

    def __init__(self) -> None:
        self.events: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self._next = 0

    async def __call__(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        params = kwargs.get("params", {})
        self.calls.append((method, url, params))
        event_id = url.rsplit("/events/", 1)[1] if "/events/" in url else None
        if method == "GET":
            prop = params["privateExtendedProperty"].split("=", 1)[1]
            time_min = datetime.fromisoformat(params["timeMin"])
            items = [
                e
                for e in self.events.values()
                if e["extendedProperties"]["private"]["fajna_account"] == prop
                and datetime.fromisoformat(e["end"]["dateTime"]) > time_min
            ]
            return FakeResponse(200, {"items": items})
        if method == "POST":
            self._next += 1
            event = stored(kwargs["json"], f"e{self._next}")
            event["iCalUID"] = f"e{self._next}@google.com"
            self.events[event["id"]] = event
            return FakeResponse(200, event)
        if method == "PATCH":
            ical_uid = self.events[event_id].get("iCalUID")
            self.events[event_id] = {**stored(kwargs["json"], event_id), "iCalUID": ical_uid}
            return FakeResponse(200, self.events[event_id])
        if method == "DELETE":
            if self.events.pop(event_id, None) is None:
                return FakeResponse(410, {"error": "gone"})
            return FakeResponse(204)
        raise AssertionError(method)


def _account(*visits: PlannedVisit, name: str = "Тестенко Іван Петрович") -> AccountData:
    return AccountData(persons={name: PersonData(name=name, planned=list(visits))})


async def test_sync_writes_then_keeps_the_calendar_in_step() -> None:
    google = FakeGoogle()
    sync = GoogleCalendarSync(GoogleCalendarApi(google, "primary"), [WIFE])

    await sync.async_sync_account(ACCOUNT, _account(MASSAGE, THERAPIST), NOW)
    assert sorted(e["summary"] for e in google.events.values()) == [
        "[Іван] Консультація",
        "[Іван] Масаж спини",
    ]

    # Nothing changed: nothing is written.
    google.calls.clear()
    plan = await sync.async_sync_account(ACCOUNT, _account(MASSAGE, THERAPIST), NOW)
    assert not plan
    assert [c[0] for c in google.calls] == ["GET"]

    # The massage is cancelled.
    await sync.async_sync_account(ACCOUNT, _account(THERAPIST), NOW)
    assert [e["summary"] for e in google.events.values()] == ["[Іван] Консультація"]


async def test_every_write_is_without_email() -> None:
    google = FakeGoogle()
    sync = GoogleCalendarSync(GoogleCalendarApi(google, "primary"), [WIFE])

    await sync.async_sync_account(ACCOUNT, _account(MASSAGE), NOW)
    await sync.async_sync_account(ACCOUNT, _account(), NOW)

    writes = [params for method, _, params in google.calls if method != "GET"]
    assert writes == [{"sendUpdates": "none"}, {"sendUpdates": "none"}]


async def test_accounts_do_not_touch_each_others_events() -> None:
    google = FakeGoogle()
    sync = GoogleCalendarSync(GoogleCalendarApi(google, "primary"), [WIFE])
    wife_account = "+380671112233"

    await sync.async_sync_account(ACCOUNT, _account(MASSAGE), NOW)
    await sync.async_sync_account(
        wife_account, _account(THERAPIST, name="Тестенко Олег Іванович"), NOW
    )
    # My account has nothing booked now; the son's visit on the wife's account stays.
    await sync.async_sync_account(ACCOUNT, _account(), NOW)

    assert [e["summary"] for e in google.events.values()] == ["[Олег] Консультація"]


async def test_a_visit_already_over_is_not_added() -> None:
    google = FakeGoogle()
    sync = GoogleCalendarSync(GoogleCalendarApi(google, "primary"), [])
    past = PlannedVisit(NOW - timedelta(hours=3), "Масаж спини", "Олег")

    await sync.async_sync_account(ACCOUNT, _account(past), NOW)

    assert google.events == {}


async def test_the_calendar_id_is_escaped_into_the_url() -> None:
    google = FakeGoogle()
    api = GoogleCalendarApi(google, "family#abc@group.calendar.google.com")

    await api.account_events(ACCOUNT, NOW)

    assert google.calls[0][1] == (
        "https://www.googleapis.com/calendar/v3/calendars/"
        "family%23abc%40group.calendar.google.com/events"
    )


async def test_an_error_from_google_is_raised() -> None:
    async def refuse(method: str, url: str, **kwargs: Any) -> FakeResponse:
        return FakeResponse(403, {"error": {"message": "Calendar API has not been used"}})

    with pytest.raises(GoogleApiError, match="403"):
        await GoogleCalendarApi(refuse, "primary").account_events(ACCOUNT, NOW)


async def test_listing_follows_pages() -> None:
    pages = [
        FakeResponse(200, {"items": [{"id": "a"}], "nextPageToken": "p2"}),
        FakeResponse(200, {"items": [{"id": "b"}]}),
    ]
    tokens = []

    async def paged(method: str, url: str, **kwargs: Any) -> FakeResponse:
        tokens.append(kwargs["params"].get("pageToken"))
        return pages.pop(0)

    events = await GoogleCalendarApi(paged, "primary").account_events(ACCOUNT, NOW)

    assert [e["id"] for e in events] == ["a", "b"]
    assert tokens == [None, "p2"]


# --------------------------------------------------------------------------- cards


async def test_a_calendar_holds_only_its_cards() -> None:
    google = FakeGoogle()
    maria = person_key(ACCOUNT, "Тестенко Марія Іванівна")
    sync = GoogleCalendarSync(GoogleCalendarApi(google, "primary"), [], persons=[maria])
    data = AccountData(
        persons={
            "Тестенко Іван Петрович": PersonData("Тестенко Іван Петрович", [MASSAGE]),
            "Тестенко Марія Іванівна": PersonData("Тестенко Марія Іванівна", [THERAPIST]),
        }
    )

    plan = await sync.async_sync_account(ACCOUNT, data, NOW)

    assert [e["summary"] for e in google.events.values()] == ["[Марія] Консультація"]
    # What was written is reported with what Google made of it.
    assert [e["iCalUID"] for e in plan.current] == ["e1@google.com"]


async def test_a_card_taken_out_of_a_calendar_leaves_it() -> None:
    google = FakeGoogle()
    api = GoogleCalendarApi(google, "primary")
    await GoogleCalendarSync(api, []).async_sync_account(ACCOUNT, _account(MASSAGE), NOW)

    # The card now has a calendar of its own: this one holds nobody of the account.
    await GoogleCalendarSync(api, [], persons=[]).async_sync_account(
        ACCOUNT, _account(MASSAGE), NOW
    )

    assert google.events == {}


async def test_the_events_left_alone_are_reported_too() -> None:
    google = FakeGoogle()
    sync = GoogleCalendarSync(GoogleCalendarApi(google, "primary"), [WIFE])
    await sync.async_sync_account(ACCOUNT, _account(MASSAGE), NOW)

    plan = await sync.async_sync_account(ACCOUNT, _account(MASSAGE), NOW)

    assert not plan
    assert [e["iCalUID"] for e in plan.current] == ["e1@google.com"]


# --------------------------------------------------------------------------- invitations


class FakeInvites:
    """An attendee's main calendar, holding the invitations it was sent."""

    def __init__(self, email: str, **answers: str) -> None:
        self.email = email
        self.answers = answers  # by iCalUID
        self.calls: list[tuple[str, str, dict[str, Any], Any]] = []

    async def __call__(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append((method, url, kwargs.get("params", {}), kwargs.get("json")))
        if method == "GET":
            uid = kwargs["params"]["iCalUID"]
            if uid not in self.answers:
                return FakeResponse(200, {"items": []})
            me = {"email": self.email, "self": True, "responseStatus": self.answers[uid]}
            other = {"email": "organizer@example.com", "organizer": True}
            return FakeResponse(200, {"items": [{"id": f"copy-{uid}", "attendees": [other, me]}]})
        if method == "PATCH":
            uid = unquote(url.rsplit("/copy-", 1)[1])
            self.answers[uid] = kwargs["json"]["attendees"][0]["responseStatus"]
            return FakeResponse(200, {})
        raise AssertionError(method)


def _linked(invites: FakeInvites) -> GoogleCalendarSync:
    return GoogleCalendarSync(GoogleCalendarApi(invites, "primary"), [], email=invites.email)


async def test_a_linked_attendee_accepts_quietly_in_their_main_calendar() -> None:
    me = FakeInvites("Me@Gmail.com", **{"e1@google.com": "needsAction"})
    event = {"iCalUID": "e1@google.com", "attendees": [{"email": "me@gmail.com"}]}

    accepted = await async_accept_invitations([event], {"me@gmail.com": _linked(me)})

    assert accepted == 1
    assert me.answers == {"e1@google.com": "accepted"}
    get, patch = me.calls
    assert get[1].endswith("/calendars/primary/events")
    assert patch[1].endswith("/calendars/primary/events/copy-e1%40google.com")
    assert patch[2] == {"sendUpdates": "none"}
    # Only our own answer: the other attendees are left out of the change.
    assert patch[3] == {
        "attendeesOmitted": True,
        "attendees": [{"email": "Me@Gmail.com", "responseStatus": "accepted"}],
    }


async def test_an_answered_invitation_is_not_asked_again() -> None:
    me = FakeInvites("me@gmail.com")
    event = {
        "iCalUID": "e1@google.com",
        "attendees": [{"email": "me@gmail.com", "responseStatus": "declined"}],
    }

    assert await async_accept_invitations([event], {"me@gmail.com": _linked(me)}) == 0
    assert me.calls == []


async def test_an_answer_of_the_persons_own_is_kept() -> None:
    # The organizer's copy still says «no answer», but the attendee has declined meanwhile.
    me = FakeInvites("me@gmail.com", **{"e1@google.com": "declined"})
    event = {"iCalUID": "e1@google.com", "attendees": [{"email": "me@gmail.com"}]}

    assert await async_accept_invitations([event], {"me@gmail.com": _linked(me)}) == 0
    assert me.answers == {"e1@google.com": "declined"}
    assert [c[0] for c in me.calls] == ["GET"]


async def test_an_invitation_not_arrived_yet_waits_for_the_next_sync() -> None:
    me = FakeInvites("me@gmail.com")
    event = {"iCalUID": "e1@google.com", "attendees": [{"email": "me@gmail.com"}]}

    assert await async_accept_invitations([event], {"me@gmail.com": _linked(me)}) == 0


async def test_attendees_not_linked_are_left_alone() -> None:
    me = FakeInvites("me@gmail.com", **{"e1@google.com": "needsAction"})
    event = {"iCalUID": "e1@google.com", "attendees": [{"email": WIFE}]}

    assert await async_accept_invitations([event], {"me@gmail.com": _linked(me)}) == 0
    assert me.calls == []


async def test_a_visit_written_with_a_linked_guest_is_accepted_end_to_end() -> None:
    """Valeria's massage, in her calendar, with me invited: I accept it at once."""
    hers = FakeGoogle()
    valeria = GoogleCalendarSync(
        GoogleCalendarApi(hers, "primary"), ["me@gmail.com"], email="valeria@gmail.com"
    )
    plan = await valeria.async_sync_account(ACCOUNT, _account(MASSAGE), NOW)
    me = FakeInvites("me@gmail.com", **{"e1@google.com": "needsAction"})

    syncs = {"valeria@gmail.com": valeria, "me@gmail.com": _linked(me)}
    accepted = await async_accept_invitations(plan.current, syncs)

    assert accepted == 1
    assert me.answers == {"e1@google.com": "accepted"}
