# /// script
# requires-python = ">=3.12,<3.14"
# dependencies = ["aiohttp>=3.10", "beautifulsoup4>=4.12"]
# ///
"""Прототип: чи можна пройти вхід у кабінет fajna.clinic і забрати дані без браузера.

Запуск (у своєму терміналі, бо скрипт питає номер, SMS-код і пароль):

    uv run prototype/probe.py            # повний вхід
    uv run prototype/probe.py --reuse    # без входу, зі збереженою сесією (перевірка, чи вона ще жива)

Що він перевіряє:
  1. вхід (телефон -> SMS -> пароль) простими HTTP-запитами, без Playwright;
  2. які cookie ставить сайт і чи достатньо однієї `accountQuery`;
  3. що сторінка пацієнта віддає візити й посилання на PDF;
  4. що PDF скачується за посиланням.

Секрети на екран не виводяться. Усе, що скрипт зберігає (HTML сторінок, сесія, PDF),
лягає в `prototype/out/`, яка в .gitignore: там ваші персональні дані.
"""

from __future__ import annotations

import asyncio
import base64
from datetime import UTC
import getpass
import hashlib
import json
from pathlib import Path
import sys
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import aiohttp
from bs4 import BeautifulSoup

BASE = "https://fajna.clinic"
LOGIN_URL = f"{BASE}/account/login"
ACCOUNT_URL = f"{BASE}/account"
SESSION_COOKIE = "accountQuery"

OUT = Path(__file__).parent / "out"
SESSION_FILE = OUT / "session.json"

# Звичайний браузерний User-Agent: сайт стоїть за Cloudflare.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "uk-UA,uk;q=0.9",
}
# Next.js перевіряє Origin у Server Action: без нього POST відхиляється.
POST_HEADERS = {**HEADERS, "Origin": BASE, "Referer": LOGIN_URL}

SECRET_HINTS = ("password", "pass", "code", "pin", "sms", "otp")
MAX_STEPS = 6


# --------------------------------------------------------------------------- helpers


def say(msg: str = "") -> None:
    print(msg, flush=True)


def is_cloudflare_challenge(status: int, html: str) -> bool:
    return status in (403, 503) and ("cf-chl" in html or "Just a moment" in html)


def find_form(html: str, page_url: str) -> dict | None:
    """Форма входу на сторінці: адреса, кодування і всі поля (разом із прихованими)."""
    soup = BeautifulSoup(html, "html.parser")
    for form in soup.find_all("form"):
        fields = []
        for el in form.find_all(["input", "select", "textarea"]):
            name = el.get("name")
            if not name:
                continue
            fields.append(
                {
                    "name": name,
                    "type": (el.get("type") or "text").lower(),
                    "value": el.get("value") or "",
                    "label": el.get("placeholder") or el.get("aria-label") or "",
                }
            )
        if not fields:
            continue
        return {
            "action": urljoin(page_url, form.get("action") or page_url),
            "enctype": (form.get("enctype") or "").lower(),
            "fields": fields,
            "text": " ".join(form.get_text(" ", strip=True).split())[:200],
        }
    return None


def is_hidden(field: dict) -> bool:
    # Службові поля Server Action ($ACTION_ID_…, $ACTION_REF_…, $ACTION_KEY) повертаємо як є.
    return field["type"] in ("hidden", "submit") or field["name"].startswith("$")


def ask(field: dict) -> str:
    name = field["name"]
    hint = f" [{field['label']}]" if field["label"] else ""
    default = f" (зараз: {field['value']})" if field["value"] else ""
    prompt = f"  Значення для поля «{name}»{hint}{default}: "
    secret = field["type"] == "password" or any(h in name.lower() for h in SECRET_HINTS)
    value = getpass.getpass(prompt) if secret else input(prompt)
    return value or field["value"]


async def submit(session: aiohttp.ClientSession, form: dict, data: list[tuple[str, str]]):
    if "multipart" in form["enctype"]:
        body = aiohttp.MultipartWriter("form-data")
        for key, value in data:
            part = body.append(value)
            part.set_content_disposition("form-data", name=key)
    else:
        body = aiohttp.FormData(data)
    return await session.post(
        form["action"], data=body, headers=POST_HEADERS, allow_redirects=False
    )


def cookie_names(session: aiohttp.ClientSession) -> list[str]:
    return sorted({c.key for c in session.cookie_jar})


def jwt_summary(token: str) -> dict:
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
    except Exception as exc:
        return {"error": f"не JWT: {exc}"}
    return {"keys": sorted(payload), "exp": payload.get("exp"), "name": payload.get("name")}


# --------------------------------------------------------------------------- login


async def login(session: aiohttp.ClientSession) -> None:
    say("== Вхід ==")
    resp = await session.get(LOGIN_URL, headers=HEADERS)
    html = await resp.text()
    page_url = str(resp.url)
    if is_cloudflare_challenge(resp.status, html):
        sys.exit(
            "Cloudflare віддав перевірку на бота: без браузера не пройти. Це важливий результат."
        )

    for step in range(1, MAX_STEPS + 1):
        form = find_form(html, page_url)
        if not form:
            (OUT / f"login-step{step}-noform.html").write_text(html)
            sys.exit(f"Крок {step}: форми на сторінці немає (збережено в out/). Зупиняюсь.")

        visible = [f for f in form["fields"] if not is_hidden(f)]
        say(f"\n-- Крок {step}: «{form['text']}»")
        say(f"   POST {form['action']}  enctype={form['enctype'] or 'urlencoded'}")
        say(f"   поля: {[f['name'] for f in form['fields']]}")

        data = [
            (f["name"], f["value"])
            for f in form["fields"]
            if is_hidden(f) and f["type"] != "submit"
        ]
        data += [(f["name"], ask(f)) for f in visible]

        resp = await submit(session, form, data)
        location = resp.headers.get("Location")
        say(
            f"   -> {resp.status}{' -> ' + location if location else ''}; cookie: {cookie_names(session)}"
        )

        if 300 <= resp.status < 400 and location:
            target = urljoin(form["action"], location)
            if urlparse(target).path.rstrip("/").endswith("/account") or "person=" in target:
                say("\nВхід пройдено.")
                return
            resp = await session.get(target, headers=HEADERS)

        html = await resp.text()
        page_url = str(resp.url)
        (OUT / f"login-step{step}.html").write_text(html)
        if resp.status >= 400:
            sys.exit(f"Крок {step}: сервер відповів {resp.status} (сторінку збережено в out/).")

    sys.exit(f"За {MAX_STEPS} кроків вхід не завершився. Сторінки кроків — у out/.")


# --------------------------------------------------------------------------- probes


async def check_cookie_alone(token: str) -> bool:
    """Чи пускає сайт з однією cookie `accountQuery`, без решти."""
    async with aiohttp.ClientSession(cookies={SESSION_COOKIE: token}) as bare:
        async with bare.get(ACCOUNT_URL, headers=HEADERS, allow_redirects=False) as resp:
            location = resp.headers.get("Location", "")
            ok = resp.status == 200 and "login" not in location
        say(
            f"Лише `{SESSION_COOKIE}`: {resp.status} {location} -> {'достатньо' if ok else 'НЕ достатньо'}"
        )
        return ok


def parse_documents(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    docs = []
    for a in soup.select('a[href*=".pdf"]'):
        url = urljoin(BASE, a["href"])
        query = parse_qs(urlparse(url).query)
        docs.append(
            {
                "file": unquote(urlparse(url).path.rsplit("/", 1)[-1]),
                "date": (query.get("date") or [""])[0],
                "url": url,
            }
        )
    return docs


async def probe_data(session: aiohttp.ClientSession, person: str) -> None:
    say("\n== Дані ==")
    resp = await session.get(ACCOUNT_URL, headers=HEADERS)
    cards_html = await resp.text()
    (OUT / "account.html").write_text(cards_html)
    if "login" in str(resp.url):
        sys.exit("Сесія не діє: сайт перекинув на вхід.")
    say(f"/account: {resp.status}, кнопок «Відкрити картку»: {cards_html.count('Відкрити картку')}")

    resp = await session.get(ACCOUNT_URL, params={"person": person}, headers=HEADERS)
    html = await resp.text()
    (OUT / "person.html").write_text(html)
    say(f"/account?person=…: {resp.status}, {len(html)} байт")
    say(f"  «Запланований візит»: {html.count('Запланований візит')} шт.")

    docs = parse_documents(html)
    say(f"  посилань на PDF: {len(docs)}")
    for doc in docs[:5]:
        say(f"    {doc['date']}  {doc['file']}")
    if not docs:
        return

    first = docs[0]
    resp = await session.get(first["url"], headers=HEADERS)
    body = await resp.read()
    kind = "PDF" if body.startswith(b"%PDF-") else f"НЕ PDF ({resp.headers.get('Content-Type')})"
    path = OUT / f"{first['date']}_{first['file']}"
    path.write_bytes(body)
    say(
        f"  перший PDF: {resp.status}, {kind}, {len(body)} байт, sha256 {hashlib.sha256(body).hexdigest()[:12]}…"
    )
    say(f"  збережено: {path}")


# --------------------------------------------------------------------------- main


async def main() -> None:
    OUT.mkdir(exist_ok=True)
    reuse = "--reuse" in sys.argv

    async with aiohttp.ClientSession() as session:
        if reuse:
            if not SESSION_FILE.exists():
                sys.exit("Збереженої сесії немає: спершу запустіть без --reuse.")
            saved = json.loads(SESSION_FILE.read_text())
            session.cookie_jar.update_cookies(saved["cookies"], aiohttp.helpers.URL(BASE))
            say(f"Сесія з {saved['saved_at']}, cookie: {sorted(saved['cookies'])}")
        else:
            await login(session)

        cookies = {
            c.key: c.value for c in session.cookie_jar if "fajna" in c["domain"] or not c["domain"]
        }
        token = cookies.get(SESSION_COOKIE)
        if not token:
            sys.exit(f"Cookie `{SESSION_COOKIE}` не отримано. Є: {sorted(cookies)}")

        summary = jwt_summary(token)
        say(f"\n`{SESSION_COOKIE}`: поля JWT {summary.get('keys')}, exp={summary.get('exp')}")
        say(f"Усі cookie сайту: {sorted(cookies)}")

        if not reuse:
            from datetime import datetime

            SESSION_FILE.write_text(
                json.dumps({"saved_at": datetime.now(UTC).isoformat(), "cookies": cookies})
            )

        await check_cookie_alone(token)
        await probe_data(session, summary.get("name") or "")

    if "--watch" in sys.argv:
        await watch(token, int(sys.argv[sys.argv.index("--watch") + 1]))


async def watch(token: str, minutes: int) -> None:
    """Опитує кабінет кожні `minutes` хвилин, доки сесія жива: скільки вона живе."""
    from datetime import datetime

    started = datetime.now(UTC)
    log = OUT / "watch.log"
    say(f"\n== Спостереження: кожні {minutes} хв, журнал у {log}. Ctrl+C — зупинити ==")
    while True:
        async with (
            aiohttp.ClientSession(cookies={SESSION_COOKIE: token}) as bare,
            bare.get(ACCOUNT_URL, headers=HEADERS, allow_redirects=False) as resp,
        ):
            alive = resp.status == 200
            fresh = resp.cookies.get(SESSION_COOKIE)
            if fresh is not None and fresh.value and fresh.value != token:
                token = fresh.value  # сайт видав новий токен: далі ходимо з ним
        now = datetime.now(UTC)
        line = (
            f"{now:%Y-%m-%d %H:%M} UTC  +{(now - started).total_seconds() / 3600:.1f} год  "
            f"{resp.status} {'жива' if alive else 'НЕЖИВА'}"
            f"{'  (новий токен)' if fresh is not None and fresh.value else ''}"
        )
        say(line)
        with log.open("a") as f:
            f.write(line + "\n")
        if not alive:
            return
        await asyncio.sleep(minutes * 60)


if __name__ == "__main__":
    asyncio.run(main())
