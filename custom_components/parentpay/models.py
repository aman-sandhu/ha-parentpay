"""Dataclass models for ParentPay data."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import TypedDict


@dataclass(frozen=True, slots=True)
class Child:
    id: str
    name: str


@dataclass(frozen=True, slots=True)
class Balance:
    child_id: str      # numeric ParentPay ConsumerId, e.g. "11111111"
    child_name: str
    amount: Decimal


@dataclass(frozen=True, slots=True)
class PaymentItem:
    child_id: str
    child_name: str
    payment_item_id: str     # ParentPay numeric item id, e.g. "9000001"
    name: str
    price: Decimal
    availability: str | None
    is_new: bool


@dataclass(frozen=True, slots=True)
class ArchiveRow:
    child_id: str
    child_name: str
    date_paid: date
    item: str
    amount_pence: int
    payment_method: str
    status: str | None
    receipt_url: str | None

    @property
    def is_meal(self) -> bool:
        return self.payment_method == "Meal"

    @property
    def is_parent_payment(self) -> bool:
        return self.payment_method == "Parent Account"


@dataclass(frozen=True, slots=True)
class HomeSnapshot:
    """Result of a single GET against the ParentPay home page."""

    balances: list[Balance]
    recent_payments: list[ArchiveRow]


@dataclass(frozen=True, slots=True)
class PaymentDetailItem:
    """One line item parsed from PaymentDetailsViewerFX.aspx."""

    tid: str           # per-line transaction id (e.g. "1355911504")
    payment_id: str    # shared parent-payment id (the ?U= query param)
    child_id: str      # from first-name match against sidebar data-consumer-data
    child_name: str
    item: str          # full item name, e.g. "English Macbeth - The Complete Play"
    amount_pence: int
    date_paid: date
    status: str | None


@dataclass(frozen=True, slots=True)
class WebFormsState:
    """ASP.NET WebForms hidden state tokens needed to round-trip a postback."""

    viewstate: str
    viewstategenerator: str
    eventvalidation: str


class MenuItem(TypedDict):
    """One 'Taken' meal item parsed from the MenusAndChoices weekly grid.

    Plain dict (not a dataclass) so it round-trips through coordinator data and
    diagnostics as JSON without custom serialisation. Amount is int pence, per
    the domain invariant.
    """

    session: str | None
    item: str
    amount_pence: int
