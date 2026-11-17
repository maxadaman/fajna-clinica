"""The client against a scripted site: what it sends, and what it makes of the answers."""

from __future__ import annotations

from dataclasses import dataclass, field
from http.cookies import SimpleCookie
from typing import Any

import aiohttp
import pytest

from custom_components.fajna_clinic.api import (
    AuthExpired,
    CannotConnect,
    FajnaClient,
    LoginRejected,
    UnexpectedPage,
    decode_token,
)

from .conftest import fixture, make_token


@dataclass
class Reply:
    status: int = 200
    text: str = ""
    location: str | None = None
    cookie: str | None = None


@dataclass
class Sent:
    method: str
    url: str
    cookies: dict[str, str]
    params: dict[str, str] | None
    body: Any


class FakeResponse:
    def __init__(self, reply: Reply) -> None:
        self.status = reply.status
        self._text = reply.text
        self.headers = {"Location": reply.location} if reply.location else {}
        self.cookies: SimpleCookie = SimpleCookie()
        if reply.cookie:
            self.cookies["accountQuery"] = reply.cookie

    async def text(self) -> str:
        return self._text

    async def __aenter__(self) -> FakeResponse:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


@dataclass
class FakeSession:
    """Answers requests in order, and remembers what was sent."""

    replies: list[Reply | Exception]
    sent: list[Sent] = field(default_factory=list)

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.sent.append(
            Sent(method, url, kwargs.get("cookies") or {}, kwargs.get("params"), kwargs.get("data"))
        )
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return FakeResponse(reply)


def form_fields(body: aiohttp.MultipartWriter) -> dict[str, str]:
    """The multipart body's fields, by name."""
    fields = {}
    for part, _, _ in body._parts:
        name = part.headers["Content-Disposition"].split('name="')[1].rstrip('"')
        fields[name] = part._value.decode()
    return fields


ACTION = "$ACTION_ID_40d472daa329f068eb938768ab6da07fbab6bb56fe"


async def test_login_walks_the_three_steps_and_keeps_the_session() -> None:
    token = make_token(name="Тестенко Іван Петрович")
    session = FakeSession(
        [
            Reply(text=fixture("login_phone.html")),
            Reply(303, location="/account/login", cookie="state-1"),
            Reply(text=fixture("login_pin.html")),
            Reply(303, location="/account/login", cookie="state-2"),
            Reply(text=fixture("login_password.html")),
            Reply(303, location="/account", cookie=token),
        ]
    )
    client = FajnaClient(session)

    assert await client.start_login() == "phone"
    assert await client.submit("+380991112233") == "pin"
    assert await client.submit("1234") == "password"
    assert await client.submit("secret") is None
    assert client.token == token

    posts = [s for s in session.sent if s.method == "POST"]
    assert [form_fields(p.body) for p in posts] == [
        {ACTION: "", "phone": "+380991112233"},
        {ACTION: "", "pin": "1234"},
        {ACTION: "", "password": "secret"},
    ]
    # Each step carries the state cookie the previous one set.
    assert [p.cookies for p in posts] == [
        {},
        {"accountQuery": "state-1"},
        {"accountQuery": "state-2"},
    ]


async def test_a_step_answered_with_itself_is_a_rejection() -> None:
    session = FakeSession(
        [
            Reply(text=fixture("login_pin.html")),
            Reply(303, location="/account/login"),
            Reply(text=fixture("login_pin.html")),
        ]
    )
    client = FajnaClient(session)
    await client.start_login()

    with pytest.raises(LoginRejected):
        await client.submit("0000")


async def test_a_login_step_that_does_not_redirect_is_unexpected() -> None:
    session = FakeSession([Reply(text=fixture("login_phone.html")), Reply(200, text="<html/>")])
    client = FajnaClient(session)
    await client.start_login()

    with pytest.raises(UnexpectedPage):
        await client.submit("+380991112233")


async def test_account_reads_every_card() -> None:
    session = FakeSession(
        [
            Reply(text=fixture("account.html")),
            Reply(text=fixture("person.html")),
            Reply(text=fixture("person.html")),
        ]
    )
    client = FajnaClient(session, token="t")

    data = await client.fetch_account()

    assert list(data.persons) == ["Тестенко Іван Петрович", "Тестенко Марія Іванівна"]
    assert len(data.persons["Тестенко Марія Іванівна"].planned) == 2
    assert [s.params for s in session.sent] == [
        None,
        {"person": "Тестенко Іван Петрович"},
        {"person": "Тестенко Марія Іванівна"},
    ]
    assert all(s.cookies == {"accountQuery": "t"} for s in session.sent)


@pytest.mark.parametrize(
    "reply",
    [Reply(307, location="/account/login"), Reply(200, text=fixture("login_phone.html"))],
)
async def test_a_dead_session_is_auth_expired(reply: Reply) -> None:
    client = FajnaClient(FakeSession([reply]), token="old")

    with pytest.raises(AuthExpired):
        await client.fetch_account()


async def test_no_token_is_auth_expired_without_a_request() -> None:
    session = FakeSession([])

    with pytest.raises(AuthExpired):
        await FajnaClient(session).fetch_account()
    assert session.sent == []


@pytest.mark.parametrize(
    "reply", [aiohttp.ClientConnectionError("down"), TimeoutError(), Reply(502, text="bad")]
)
async def test_network_failures_are_cannot_connect(reply: Reply | Exception) -> None:
    with pytest.raises(CannotConnect):
        await FajnaClient(FakeSession([reply]), token="t").fetch_account()


async def test_a_card_page_the_parser_does_not_know_is_unexpected() -> None:
    session = FakeSession([Reply(text=fixture("account.html")), Reply(text="<html></html>")])

    with pytest.raises(UnexpectedPage):
        await FajnaClient(session, token="t").fetch_account()


def test_token_claims_are_read() -> None:
    assert decode_token(make_token(name="Іван"))["name"] == "Іван"
    with pytest.raises(UnexpectedPage):
        decode_token("not-a-jwt")
