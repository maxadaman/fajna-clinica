"""Reads the patient account's HTML. Pure functions: no network, no Home Assistant.

The account has no API: the server renders everything into the page, so this module is
the one place that knows its markup. When the clinic changes its layout, this is the
file to change, and `tests/fixtures/` holds the pages it is checked against.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import re
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup, Tag

from .const import BASE_URL, CLINIC_TZ
from .models import Document, FinishedVisit, PersonData, PlannedVisit

_ISO_DATE = re.compile(r"(\d{4}-\d{2}-\d{2})")
_TIME = re.compile(r"\b(\d{1,2}):(\d{2})\b")

# Column titles as the page writes them, mapped to our field names.
_COLUMNS = {
    "Послуга": "service",
    "Виконавець/Лікар": "doctor",
    "Інструкції": "instructions",
    "Оплата": "payment",
    "Статус": "status",
    "Документи/Результати": "documents",
}


class ParseError(Exception):
    """The page is not what this parser knows: the markup has probably changed."""


def _soup(html: str) -> BeautifulSoup:
    soup = BeautifulSoup(html, "html.parser")
    # Next.js repeats the page's data inside <script>; only the rendered DOM counts.
    for tag in soup.find_all(["script", "style", "svg"]):
        tag.decompose()
    return soup


def _text(node: Tag | None) -> str:
    return " ".join(node.get_text(" ", strip=True).split()) if node else ""


def _iso_date(text: str) -> date | None:
    match = _ISO_DATE.search(text)
    return date.fromisoformat(match.group(1)) if match else None


def _items(group: Tag) -> dict[str, Tag]:
    """A visit's cells by field name: each cell is its column title plus the value."""
    cells: dict[str, Tag] = {}
    for item in group.select(".account-list__item"):
        title = item.select_one(".account-list__item-title")
        key = _COLUMNS.get(_text(title))
        if key:
            title.extract()
            cells[key] = item
    return cells


@dataclass(frozen=True, slots=True)
class LoginForm:
    """The login page's form at its current step.

    Every step is the same Server Action, so the form is the state: the hidden inputs
    (`$ACTION_ID_…`, and whatever Next adds) go back as they are, and the one visible
    input says which step this is — `phone`, `pin` or `password`.

    An account with several patient cards has one more step after the password: a submit
    button per card, all named `name`, each with a card's full name as its value. Then
    `field` is the buttons' name and `choices` their values.
    """

    hidden: tuple[tuple[str, str], ...]
    field: str
    choices: tuple[str, ...] = ()


def parse_login_form(html: str) -> LoginForm:
    soup = BeautifulSoup(html, "html.parser")
    for form in soup.find_all("form"):
        hidden, visible = [], []
        for el in form.find_all(["input", "select", "textarea"]):
            name = el.get("name")
            if not name:
                continue
            if el.get("type") == "hidden" or name.startswith("$"):
                hidden.append((name, el.get("value") or ""))
            else:
                visible.append(name)
        if not any(name.startswith("$ACTION_ID_") for name, _ in hidden):
            continue
        if len(visible) == 1:
            return LoginForm(hidden=tuple(hidden), field=visible[0])
        # The card step: named buttons with values. The logout form's button has neither.
        buttons = [
            (b["name"], b["value"])
            for b in form.find_all("button")
            if b.get("name") and b.get("value")
        ]
        if not visible and buttons and len({name for name, _ in buttons}) == 1:
            return LoginForm(
                hidden=tuple(hidden),
                field=buttons[0][0],
                choices=tuple(value for _, value in buttons),
            )
    raise ParseError("no login form on the page")


def is_login_page(html: str) -> bool:
    """The account answers a dead session with its login form rather than an error."""
    soup = BeautifulSoup(html, "html.parser")
    return soup.find("input", attrs={"name": "phone"}) is not None or (
        soup.find("input", attrs={"name": "pin"}) is not None
    )


def parse_persons(html: str) -> list[str]:
    """The patient cards on `/account`, by full name — the only id the site has."""
    soup = _soup(html)
    names = []
    for button in soup.find_all("button", string=re.compile("Відкрити картку")):
        card = button.find_parent("li")
        title = card.find("div") if card else None
        name = _text(title)
        if name and name not in names:
            names.append(name)
    return names


def parse_planned(soup: BeautifulSoup) -> list[PlannedVisit]:
    visits = []
    for block in soup.select("details.account-list--planned-visits"):
        day = _iso_date(_text(block.find("summary")))
        if day is None:
            raise ParseError("planned visit without a date")
        for wrap in block.select(".account-list__group-wrap"):
            header = wrap.find("span")
            time = _TIME.search(_text(header))
            hour, minute = (int(time.group(1)), int(time.group(2))) if time else (0, 0)
            start = datetime(day.year, day.month, day.day, hour, minute, tzinfo=CLINIC_TZ)
            for group in wrap.select(".account-list__group"):
                cells = _items(group)
                visits.append(
                    PlannedVisit(
                        start=start,
                        service=_text(cells.get("service")),
                        doctor=_text(cells.get("doctor")),
                        instructions=_text(cells.get("instructions")),
                        payment=_text(cells.get("payment")),
                    )
                )
    return visits


def _documents(cell: Tag | None, fallback: date) -> tuple[Document, ...]:
    if cell is None:
        return ()
    docs = []
    for link in cell.find_all("a", href=True):
        url = urljoin(BASE_URL, link["href"])
        query_date = (parse_qs(urlparse(url).query).get("date") or [""])[0]
        docs.append(
            Document(
                name=_text(link),
                url=url,
                date=_iso_date(query_date) or fallback,
            )
        )
    return tuple(docs)


def parse_finished(soup: BeautifulSoup) -> list[FinishedVisit]:
    visits = []
    for block in soup.select("details.account-list--finished-visits"):
        day = _iso_date(_text(block.find("summary")))
        if day is None:
            raise ParseError("finished visit without a date")
        for group in block.select(".account-list__group"):
            cells = _items(group)
            visits.append(
                FinishedVisit(
                    date=day,
                    service=_text(cells.get("service")),
                    doctor=_text(cells.get("doctor")),
                    status=_text(cells.get("status")),
                    payment=_text(cells.get("payment")),
                    documents=_documents(cells.get("documents"), day),
                )
            )
    return visits


def parse_person(name: str, html: str) -> PersonData:
    """One patient's card: the visits booked and the visits that took place."""
    soup = _soup(html)
    if soup.find(string=re.compile("Ваші візити та документи")) is None:
        # Not even an empty card: a page this parser does not know.
        raise ParseError("not a patient card")
    return PersonData(name=name, planned=parse_planned(soup), finished=parse_finished(soup))
