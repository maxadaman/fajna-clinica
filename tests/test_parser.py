"""The account's markup, read from pages shaped like the real ones."""

from __future__ import annotations

from datetime import date, datetime

import pytest

from custom_components.fajna_clinic.const import CLINIC_TZ
from custom_components.fajna_clinic.parser import (
    ParseError,
    is_login_page,
    parse_login_form,
    parse_person,
    parse_persons,
)

from .conftest import fixture


def test_persons_are_read_from_the_cards_once() -> None:
    # The page repeats a name inside Next's <script> payload; only the cards count.
    assert parse_persons(fixture("account.html")) == [
        "Тестенко Іван Петрович",
        "Тестенко Марія Іванівна",
    ]


def test_planned_visits_carry_their_time_in_kyiv() -> None:
    person = parse_person("Тестенко Іван Петрович", fixture("person.html"))

    assert [v.start for v in person.planned] == [
        datetime(2026, 11, 27, 18, 40, tzinfo=CLINIC_TZ),
        datetime(2026, 12, 2, 9, 5, tzinfo=CLINIC_TZ),
    ]
    first = person.planned[0]
    assert first.service == "Масаж спини"
    assert first.doctor == "Лікаренко Олег Олегович"
    assert first.instructions == "Натщесерце"
    assert first.payment == ""


def test_planned_visit_uid_is_stable_and_distinct() -> None:
    html = fixture("person.html")
    once = parse_person("x", html).planned
    again = parse_person("x", html).planned

    assert [v.uid for v in once] == [v.uid for v in again]
    assert once[0].uid != once[1].uid


def test_finished_visits_keep_each_service_and_its_documents() -> None:
    person = parse_person("x", fixture("person.html"))

    assert [(v.date, v.service) for v in person.finished] == [
        (date(2026, 11, 16), "Консультація дерматолога"),
        (date(2026, 9, 14), "Аналіз крові"),
        (date(2026, 9, 14), "Консультація сімейного лікаря"),
    ]
    derm = person.finished[0]
    assert derm.status == "Виконано"
    assert derm.payment == "1116 грн. (до оплати)"
    (doc,) = derm.documents
    assert doc.name == "Консультаційний висновок дерматолога.pdf"
    assert doc.url.startswith("https://fajna.clinic/uk/account/")
    assert doc.date == date(2026, 11, 16)
    assert person.finished[2].documents == ()


def test_a_document_without_a_date_takes_its_visits() -> None:
    lab = parse_person("x", fixture("person.html")).finished[1]

    assert lab.documents[0].date == date(2026, 9, 14)


def test_a_page_that_is_not_a_card_is_refused() -> None:
    with pytest.raises(ParseError):
        parse_person("x", fixture("account.html"))


@pytest.mark.parametrize(
    ("page", "field"),
    [("login_phone.html", "phone"), ("login_pin.html", "pin"), ("login_password.html", "password")],
)
def test_login_form_names_its_step(page: str, field: str) -> None:
    form = parse_login_form(fixture(page))

    assert form.field == field
    assert form.hidden == (("$ACTION_ID_40d472daa329f068eb938768ab6da07fbab6bb56fe", ""),)


def test_the_card_step_offers_every_card() -> None:
    form = parse_login_form(fixture("login_cards.html"))

    assert form.field == "name"
    assert form.choices == ("Тестенко Іван Петрович", "Тестенко Марія Іванівна")
    assert form.hidden == (("$ACTION_ID_40d472daa329f068eb938768ab6da07fbab6bb56fe", ""),)
    assert not is_login_page(fixture("login_cards.html"))


def test_logout_form_is_not_a_login_step() -> None:
    with pytest.raises(ParseError):
        parse_login_form(fixture("account.html"))


def test_login_page_is_told_from_the_account() -> None:
    assert is_login_page(fixture("login_phone.html"))
    assert is_login_page(fixture("login_pin.html"))
    assert not is_login_page(fixture("account.html"))
    assert not is_login_page(fixture("person.html"))
