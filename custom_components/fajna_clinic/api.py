"""HTTP client for the Fajna Clinic patient account. No Home Assistant imports.

The site has no API. Login is a Next.js Server Action posted once per step
(phone -> SMS code -> password, then which card to open when the account has several),
and the session is one cookie, `accountQuery`.
The client keeps that cookie itself rather than in a cookie jar, so two accounts on
one session can never mix: the session it is given must not store cookies.
"""

from __future__ import annotations

import base64
from collections.abc import Collection
import json
import logging
from typing import Any
from urllib.parse import urljoin, urlparse

import aiohttp

from .const import ACCOUNT_PATH, BASE_URL, LOGIN_PATH, SESSION_COOKIE
from .models import AccountData
from .parser import (
    LoginForm,
    ParseError,
    is_login_page,
    parse_login_form,
    parse_person,
    parse_persons,
)

_LOGGER = logging.getLogger(__name__)

_TIMEOUT = aiohttp.ClientTimeout(total=30)
_HEADERS = {
    # A browser's agent: the site is behind Cloudflare.
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "uk-UA,uk;q=0.9",
}
# A Server Action is refused without an Origin of the site's own.
_POST_HEADERS = {**_HEADERS, "Origin": BASE_URL, "Referer": BASE_URL + LOGIN_PATH}


class FajnaError(Exception):
    """Base error of this client."""


class CannotConnect(FajnaError):
    """The site did not answer, or answered with a server error."""


class AuthExpired(FajnaError):
    """The session is gone: the account answers with its login page."""


class LoginRejected(FajnaError):
    """A login step was answered with the same step again: a wrong code or password."""


class UnexpectedPage(FajnaError):
    """The page is not one this client knows: the site has probably changed."""


def decode_token(token: str) -> dict[str, Any]:
    """The session JWT's claims (`name`, `phone`, …). Read, never verified: it is ours."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (IndexError, ValueError) as err:
        raise UnexpectedPage("the session cookie is not a JWT") from err
    if not isinstance(claims, dict):
        raise UnexpectedPage("the session JWT has no claims")
    return claims


class FajnaClient:
    """One account of the clinic."""

    def __init__(self, session: aiohttp.ClientSession, token: str | None = None) -> None:
        self._session = session
        self._token = token
        self._form: LoginForm | None = None

    @property
    def token(self) -> str | None:
        return self._token

    # ------------------------------------------------------------------ transport

    def _cookies(self) -> dict[str, str]:
        return {SESSION_COOKIE: self._token} if self._token else {}

    def _remember_cookie(self, resp: aiohttp.ClientResponse) -> None:
        morsel = resp.cookies.get(SESSION_COOKIE)
        if morsel is not None and morsel.value:
            self._token = morsel.value

    async def _request(self, method: str, path: str, **kwargs: Any) -> tuple[int, str, str | None]:
        """Status, body and `Location`. Redirects are not followed: a redirect is the answer."""
        headers = _POST_HEADERS if method == "POST" else _HEADERS
        try:
            async with self._session.request(
                method,
                urljoin(BASE_URL, path),
                headers=headers,
                cookies=self._cookies(),
                allow_redirects=False,
                timeout=_TIMEOUT,
                **kwargs,
            ) as resp:
                self._remember_cookie(resp)
                body = await resp.text()
                if resp.status >= 500:
                    raise CannotConnect(f"{method} {path}: {resp.status}")
                return resp.status, body, resp.headers.get("Location")
        except (aiohttp.ClientError, TimeoutError) as err:
            raise CannotConnect(f"{method} {path}: {err}") from err

    # ------------------------------------------------------------------ login

    async def start_login(self) -> str:
        """Open the login page afresh. Answers the first step's field, `phone`."""
        self._token = None
        _, html, _ = await self._request("GET", LOGIN_PATH)
        return self._read_form(html)

    async def submit(self, value: str) -> str | None:
        """Answer the current step. Answers the next step's field, or None once signed in."""
        if self._form is None:
            raise FajnaError("start_login() first")
        step = self._form.field
        # React posts a Server Action's form as multipart/form-data.
        body = aiohttp.MultipartWriter("form-data")
        for name, field_value in (*self._form.hidden, (step, value)):
            part = body.append(field_value)
            part.set_content_disposition("form-data", name=name)

        status, _, location = await self._request("POST", LOGIN_PATH, data=body)
        if status not in (302, 303, 307) or not location:
            raise UnexpectedPage(f"login step '{step}' answered {status}")

        target = urlparse(urljoin(BASE_URL, location)).path.rstrip("/")
        if target == ACCOUNT_PATH:
            self._form = None
            if not self._token:
                raise UnexpectedPage("signed in, but no session cookie was set")
            return None

        _, html, _ = await self._request("GET", LOGIN_PATH)
        next_field = self._read_form(html)
        if next_field == step:
            raise LoginRejected(step)
        assert self._form is not None
        if self._form.choices:
            # Several cards: the site asks which one to open, as the last step. Any will do:
            # the session is the account's, and every card is then read by its name.
            return await self.submit(self._form.choices[0])
        return next_field

    def _read_form(self, html: str) -> str:
        try:
            self._form = parse_login_form(html)
        except ParseError as err:
            raise UnexpectedPage(str(err)) from err
        return self._form.field

    # ------------------------------------------------------------------ account

    async def _page(self, params: dict[str, str] | None = None) -> str:
        if not self._token:
            raise AuthExpired("no session")
        status, html, location = await self._request("GET", ACCOUNT_PATH, params=params)
        if (location and LOGIN_PATH in location) or is_login_page(html):
            raise AuthExpired("the account answered with its login page")
        if status != 200:
            raise UnexpectedPage(f"{ACCOUNT_PATH} answered {status}")
        return html

    async def list_persons(self) -> list[str]:
        """The names on the account's patient cards, as the account lists them."""
        try:
            return parse_persons(await self._page())
        except ParseError as err:
            raise UnexpectedPage(str(err)) from err

    async def fetch_account(self, persons: Collection[str] | None = None) -> AccountData:
        """The patient cards of the account, with their visits: all, or those in `persons`.

        A chosen card the account no longer lists is left out, not an error.
        """
        data = AccountData()
        try:
            names = parse_persons(await self._page())
            for name in names:
                if persons is not None and name not in persons:
                    continue
                html = await self._page({"person": name})
                data.persons[name] = parse_person(name, html)
        except ParseError as err:
            raise UnexpectedPage(str(err)) from err
        _LOGGER.debug("Read %d patient card(s)", len(data.persons))
        return data
