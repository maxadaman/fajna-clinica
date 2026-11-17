"""What the patient account holds, as plain data."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
import hashlib


@dataclass(frozen=True, slots=True)
class Document:
    """A file attached to a finished visit (a conclusion, a lab result)."""

    name: str
    url: str
    date: date | None


@dataclass(frozen=True, slots=True)
class PlannedVisit:
    """A visit booked for the future."""

    start: datetime
    service: str
    doctor: str
    instructions: str = ""
    payment: str = ""

    @property
    def uid(self) -> str:
        """A stable id: the page has none, so it is derived from what the visit is."""
        raw = f"{self.start.isoformat()}|{self.service}|{self.doctor}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class FinishedVisit:
    """A visit that took place, with its documents."""

    date: date
    service: str
    doctor: str
    status: str = ""
    payment: str = ""
    documents: tuple[Document, ...] = ()


@dataclass(slots=True)
class PersonData:
    """One patient card of the account."""

    name: str
    planned: list[PlannedVisit] = field(default_factory=list)
    finished: list[FinishedVisit] = field(default_factory=list)


@dataclass(slots=True)
class AccountData:
    """Everything one account shows, by patient name."""

    persons: dict[str, PersonData] = field(default_factory=dict)
