"""Pure-function parsers for ParentPay responses."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from datetime import date
from decimal import Decimal
from typing import Any

from bs4 import BeautifulSoup, Tag

from .exceptions import ParentPayParseError
from .models import (
    ArchiveRow,
    Balance,
    MenuItem,
    PaymentDetailItem,
    PaymentItem,
    WebFormsState,
)

_BALANCE_SPAN_RE = re.compile(r"£\s*(?P<amount>\d+\.\d{2})")


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


def _has_attr(name: str) -> Callable[[Tag], bool]:
    """Tag filter for "has this attribute at all".

    The equivalent `attrs={name: True}` form no longer type-checks: bs4 4.15 declares
    `attrs` as an invariant `dict`, so a `dict[str, bool]` literal is rejected even
    though `bool` is in the value union.
    """
    return lambda tag: tag.has_attr(name)


def parse_login_response(*, status: int, payload: Mapping[str, Any]) -> tuple[bool, str | None]:
    """Classify a login API response.

    Success: HTTP 200 and payload.isActivated is True.
    Failure: HTTP 400 (bad credentials) or HTTP 200 with isActivated != True.
    Any other combination raises ParentPayParseError — the caller shouldn't
    guess.
    """
    if status == 200 and payload.get("isActivated") is True:
        return True, None
    if status == 400 and isinstance(payload.get("message"), str):
        return False, payload["message"]
    if status == 200:
        return False, "Login response reports account not activated"
    raise ParentPayParseError(
        f"Unexpected login response: status={status} payload_keys={sorted(payload)}"
    )


def _build_child_name_map(soup: BeautifulSoup) -> dict[str, str]:
    """ConsumerId → name, from sidebar data-consumer-data attributes."""
    out: dict[str, str] = {}
    for el in soup.find_all(_has_attr("data-consumer-data")):
        try:
            raw = str(el.get("data-consumer-data") or "").replace("'", '"')
            obj = json.loads(raw)
        except (ValueError, KeyError):
            continue
        cid = str(obj.get("id") or "").strip()
        name = str(obj.get("name") or "").strip()
        if cid and name:
            out[cid] = name
    return out


def parse_home_balances(html: str) -> list[Balance]:
    """Extract per-child dinner-money balance from the Default.aspx home page."""
    soup = _soup(html)
    name_by_id = _build_child_name_map(soup)
    balances: list[Balance] = []
    # Balance span id IS the ConsumerId
    for span in soup.select("span.large.text-primary[id]"):
        cid = str(span.get("id", "")).strip()
        if not cid or not cid.isdigit():
            continue
        m = _BALANCE_SPAN_RE.search(span.get_text(" ", strip=True))
        if not m:
            continue
        balances.append(
            Balance(
                child_id=cid,
                child_name=name_by_id.get(cid, cid),
                amount=Decimal(m.group("amount")),
            )
        )
    if not balances:
        raise ParentPayParseError("No balances found on home page", snippet=html[:500])
    return balances


def parse_home_recent_payments(html: str) -> list[ArchiveRow]:
    """Extract the 'Recent payments' table from the home (Default.aspx) page.

    The home-page table has a different structure from the archive page — it
    shows 4 columns (Date, Type, Details, Amount) with no per-row consumer ID.
    All rows in this table are Parent Account transactions.
    """
    soup = _soup(html)
    rows: list[ArchiveRow] = []
    # Locate the "Recent payments" table by its summary attribute
    table = soup.find("table", attrs={"summary": "Recent payments"})
    if table is None:
        return rows
    for tr in table.find_all("tr"):
        cells = tr.find_all("td")
        if len(cells) < 4:
            continue
        date_text = cells[0].get_text(" ", strip=True)
        amount_text = cells[3].get_text(" ", strip=True)
        try:
            the_date = _parse_short_date(date_text)
        except ParentPayParseError:
            continue
        # Item name from the anchor's data-gtm-label or link text
        receipt_a = cells[2].find("a", href=re.compile(r"PaymentDetailsViewerFX\.aspx"))
        if receipt_a:
            item_name = str(receipt_a.get("data-gtm-label") or receipt_a.get_text(" ", strip=True))
            receipt_url = str(receipt_a["href"])
        else:
            item_name = cells[2].get_text(" ", strip=True)
            receipt_url = None
        rows.append(
            ArchiveRow(
                child_id="",
                child_name="",
                date_paid=the_date,
                item=item_name,
                amount_pence=_amount_to_pence(amount_text),
                payment_method="Parent Account",
                status=None,
                receipt_url=receipt_url,
            )
        )
    return rows


_DATE_RE = re.compile(r"(\d{1,2})\s+([A-Za-z]{3})\s+(\d{2,4})")
_MONTHS = {
    "Jan": 1,
    "Feb": 2,
    "Mar": 3,
    "Apr": 4,
    "May": 5,
    "Jun": 6,
    "Jul": 7,
    "Aug": 8,
    "Sep": 9,
    "Oct": 10,
    "Nov": 11,
    "Dec": 12,
}


def _parse_short_date(text: str) -> date:
    m = _DATE_RE.search(text)
    if not m:
        raise ParentPayParseError(f"Unparseable date: {text!r}")
    day = int(m.group(1))
    month_key = m.group(2).title()
    if month_key not in _MONTHS:
        raise ParentPayParseError(f"Unknown month in {text!r}")
    year = int(m.group(3))
    if year < 100:
        year += 2000
    return date(year, _MONTHS[month_key], day)


def _amount_to_pence(text: str) -> int:
    cleaned = text.replace("£", "").replace(",", "").strip()
    if not cleaned:
        return 0
    return round(Decimal(cleaned) * 100)


def _parse_archive_rows(html: str, *, only_parent_account: bool = False) -> list[ArchiveRow]:
    """Parse <tr cid="..."> rows from either home-page or archive-page markup.

    Used by parse_home_recent_payments (only_parent_account=True) and
    parse_archive (only_parent_account=False).
    """
    soup = _soup(html)
    name_by_id = _build_child_name_map(soup)
    rows: list[ArchiveRow] = []
    for tr in soup.find_all("tr", attrs={"cid": True}):
        cid = str(tr.get("cid") or "").strip()
        if not cid.isdigit() or int(cid) == 0:
            continue
        cells = tr.find_all("td")
        if len(cells) < 7:
            continue
        item_name = cells[1].get_text(" ", strip=True)
        date_text = cells[2].get_text(" ", strip=True)
        meal_desc = cells[4].get_text(" ", strip=True)
        status_text = cells[5].get_text(" ", strip=True)
        amount_text = cells[6].get_text(" ", strip=True)
        try:
            the_date = _parse_short_date(date_text)
        except ParentPayParseError:
            continue
        is_meal = bool(meal_desc)
        if only_parent_account and is_meal:
            continue
        payment_method = "Meal" if is_meal else "Parent Account"
        effective_item = meal_desc if is_meal else item_name
        receipt_a = tr.find("a", href=re.compile(r"PaymentDetailsViewerFX\.aspx"))
        receipt_url = str(receipt_a["href"]) if receipt_a else None
        rows.append(
            ArchiveRow(
                child_id=cid,
                child_name=name_by_id.get(cid, cid),
                date_paid=the_date,
                item=effective_item,
                amount_pence=_amount_to_pence(amount_text),
                payment_method=payment_method,
                status=status_text or None,
                receipt_url=receipt_url,
            )
        )
    return rows


def parse_archive(html: str) -> list[ArchiveRow]:
    """Parse the payment-history (archive) page into ArchiveRow instances.

    Empty-but-valid is distinguished from broken. Two shapes count as a
    legitimate empty result:

    * ``<table summary="Payments">`` present with no data rows.
    * No table at all, but the explicit ``No results found`` panel — which is
      what ParentPay actually renders when the date range holds no
      transactions (e.g. a 7.5-week summer holiday with a 60-day window).

    A response with neither marker (auth redirect, UI change, server error)
    still raises so regressions don't silently swallow missing data.
    """
    rows = _parse_archive_rows(html, only_parent_account=False)
    if rows:
        return rows
    if 'summary="Payments"' in html or _EMPTY_ARCHIVE_MARKER in html:
        return []
    raise ParentPayParseError("No archive rows parsed", snippet=html[:500])


# ParentPay renders this panel (and no payments table at all) when a search
# returns zero transactions.
_EMPTY_ARCHIVE_MARKER = "No results found"

_PRICE_RE = re.compile(r"£\s*(?P<amount>-?\d+(?:\.\d{2})?)")


def _first_text(el: Any) -> str:
    return el.get_text(" ", strip=True) if el is not None else ""


def parse_payment_items(html: str) -> list[PaymentItem]:
    """Parse the Payment Items page into per-child PaymentItem rows."""
    soup = _soup(html)
    name_by_id = _build_child_name_map(soup)
    results: list[PaymentItem] = []
    for container in soup.select("div.well.payment-item"):
        ids_el = container.find(_has_attr("data-payment-item-id"))
        if ids_el is None:
            continue
        raw_item_id = ids_el.get("data-payment-item-id") or ""
        raw_cid = ids_el.get("data-consumer-id") or ""
        payment_item_id = (raw_item_id if isinstance(raw_item_id, str) else raw_item_id[0]).strip()
        child_id = (raw_cid if isinstance(raw_cid, str) else raw_cid[0]).strip()
        if not payment_item_id or not child_id:
            continue
        # Item name: inside div.payment-item-list > dl > dd.large
        name_el = container.select_one("div.payment-item-list dd.large")
        name = _first_text(name_el)
        # Price: span inside the [data-payment-item-id] div
        price_el = ids_el.select_one("span.text-nowrap.large.text-primary")
        cost_text = _first_text(price_el) if price_el is not None else _first_text(ids_el)
        price_match = _PRICE_RE.search(cost_text)
        price = Decimal(price_match.group("amount")) if price_match else Decimal("0")
        availability_label = container.find(string=lambda s: s and "Availability" in s)
        availability: str | None = None
        if availability_label:
            sibling = availability_label.find_next(string=True)
            availability = sibling.strip() if sibling else None
        is_new = bool(
            container.find(string=lambda s: s and s.strip() == "New!")
            or container.select_one(".badge-new, .new-badge")
        )
        results.append(
            PaymentItem(
                child_id=child_id,
                child_name=name_by_id.get(child_id, child_id),
                payment_item_id=payment_item_id,
                name=name,
                price=price,
                availability=availability,
                is_new=is_new,
            )
        )
    if not results:
        raise ParentPayParseError("No payment items found", snippet=html[:500])
    return results


# --- PaymentDetailsViewerFX.aspx receipt page ------------------------------

_RECEIPT_CODE_RE = re.compile(r"PP(\d+)")
_DETAIL_DATE_RE = re.compile(r"(\d{1,2})\s+([A-Za-z]{3})\s+(\d{4})")
_RECEIPT_URL_RE = re.compile(
    r"[?&]TID=(?P<tid>\d+).*?[?&]U=(?P<u>\d+)"
    r"|[?&]U=(?P<u2>\d+).*?[?&]TID=(?P<tid2>\d+)"
)


def extract_receipt_ids(url: str) -> tuple[str, str] | None:
    """Return (tid, u) from a PaymentDetailsViewerFX URL, or None if unparseable."""
    m = _RECEIPT_URL_RE.search(url)
    if not m:
        return None
    tid = m.group("tid") or m.group("tid2") or ""
    u = m.group("u") or m.group("u2") or ""
    if not tid or not u:
        return None
    return tid, u


def _parse_detail_date(text: str) -> date:
    m = _DETAIL_DATE_RE.search(text)
    if not m:
        raise ParentPayParseError(f"Unparseable date: {text!r}")
    day = int(m.group(1))
    month_key = m.group(2).title()
    if month_key not in _MONTHS:
        raise ParentPayParseError(f"Unknown month in {text!r}")
    return date(int(m.group(3)), _MONTHS[month_key], day)


def parse_payment_detail(html: str) -> list[PaymentDetailItem]:
    """Parse a PaymentDetailsViewerFX.aspx receipt page into line items.

    One receipt can list multiple line items — each gets its own TID, and the
    `PP{TID}` receipt codes are in the same positional order as the rows.
    """
    soup = _soup(html)
    name_by_id = _build_child_name_map(soup)
    # Map first name → consumer id for fuzzy lookup ("Lauren Emmerson" → "18416154")
    first_name_to_cid = {name.split()[0].lower(): cid for cid, name in name_by_id.items()}

    # Payment ID (the `U` query param, shared across all line items)
    payment_id = ""
    for dt_el in soup.find_all("dt"):
        if dt_el.get_text(strip=True) == "Payment ID":
            dd = dt_el.find_next("dd")
            if dd is not None:
                payment_id = dd.get_text(strip=True)
            break

    # Receipt codes → per-line TIDs in row order
    tids: list[str] = []
    for dt_el in soup.find_all("dt"):
        if dt_el.get_text(strip=True) == "Receipt code":
            dd = dt_el.find_next("dd")
            if dd is not None:
                tids = _RECEIPT_CODE_RE.findall(dd.get_text(" ", strip=True))
            break

    # Line-items table: <table summary="Account statements" class="...tbHistoryDesktop">
    table = None
    for t in soup.find_all("table"):
        classes: list[str] = list(t.get("class") or [])
        if t.get("summary") == "Account statements" and "tbHistoryDesktop" in classes:
            table = t
            break
    if table is None:
        raise ParentPayParseError(
            "No line-items table found on receipt page", snippet=html[:500]
        )

    items: list[PaymentDetailItem] = []
    body_rows = [tr for tr in table.find_all("tr") if tr.find("td")]
    for idx, tr in enumerate(body_rows):
        cells = tr.find_all("td")
        if len(cells) < 7:
            continue
        date_text = cells[0].get_text(" ", strip=True)
        pupil_name = cells[1].get_text(" ", strip=True)
        item_name = cells[2].get_text(" ", strip=True)
        amount_text = cells[5].get_text(" ", strip=True)
        status_text = cells[6].get_text(" ", strip=True)
        try:
            the_date = _parse_detail_date(date_text)
        except ParentPayParseError:
            continue
        first_name = pupil_name.split()[0] if pupil_name else ""
        cid = first_name_to_cid.get(first_name.lower(), "")
        tid = tids[idx] if idx < len(tids) else ""
        items.append(
            PaymentDetailItem(
                tid=tid,
                payment_id=payment_id,
                child_id=cid,
                child_name=name_by_id.get(cid, first_name),
                item=item_name,
                amount_pence=_amount_to_pence(amount_text),
                date_paid=the_date,
                status=status_text or None,
            )
        )
    if not items:
        raise ParentPayParseError(
            "No line items parsed from receipt page", snippet=html[:500]
        )
    return items


def parse_webforms_state(html: str) -> WebFormsState:
    """Extract __VIEWSTATE, __VIEWSTATEGENERATOR, __EVENTVALIDATION from a WebForms page.

    All three tokens must be present — the search POST will be rejected without
    them — so a missing token is a parse error, not an empty-default.
    """
    soup = _soup(html)
    fields: dict[str, str] = {}
    for name in ("__VIEWSTATE", "__VIEWSTATEGENERATOR", "__EVENTVALIDATION"):
        el = soup.find("input", {"name": name})
        if el is None:
            raise ParentPayParseError(f"Missing WebForms hidden field: {name}")
        value = el.get("value")
        fields[name] = str(value) if value is not None else ""
    return WebFormsState(
        viewstate=fields["__VIEWSTATE"],
        viewstategenerator=fields["__VIEWSTATEGENERATOR"],
        eventvalidation=fields["__EVENTVALIDATION"],
    )


# --- MenusAndChoices.aspx weekly meal grid ---------------------------------

_MENU_MONTHS: dict[str, int] = {
    m: i
    for i, m in enumerate(
        ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"],
        1,
    )
}
_MENU_SESSION_LABELS: frozenset[str] = frozenset(
    {"Morning", "Lunch time", "Afternoon", "Break", "Breakfast"}
)
_MENU_HEADER_DATE_RE = re.compile(r"(\d{1,2})\s+([A-Za-z]{3})")
_MENU_PRICE_TAIL_RE = re.compile(r"(\d+(?:\.\d{1,2})?)\s*$")
_MENU_PRICE_STRIP_RE = re.compile(r"\s*-?\s*£?\s*\d+(?:\.\d{1,2})?\s*$")


def _menu_header_dates(header_cells: list[Tag], ref_year: int) -> list[date | None]:
    """Map each column header ("Fri 9 Oct") to a date.

    Headers omit the year, so ``ref_year`` seeds it; if the month decreases
    across columns the week straddles 31 Dec and the year rolls forward.
    """
    dates: list[date | None] = []
    prev_month: int | None = None
    year = ref_year
    for th in header_cells:
        m = _MENU_HEADER_DATE_RE.search(th.get_text(" ", strip=True))
        if not m:
            dates.append(None)
            continue
        month = _MENU_MONTHS.get(m.group(2).title())
        if month is None:
            dates.append(None)
            continue
        if prev_month is not None and month < prev_month:
            year += 1
        prev_month = month
        try:
            dates.append(date(year, month, int(m.group(1))))
        except ValueError:
            dates.append(None)
    return dates


def parse_menu_choices(html: str, ref_year: int) -> dict[str, list[MenuItem]]:
    """Parse the weekly MenusAndChoices grid into the taken items per date.

    Returns ``{date_iso: [MenuItem, ...]}``. Only cells flagged "Taken" (a tick
    image or the ``success`` cell class) are included — those are the items the
    pupil actually had and was charged for. A session label ("Morning", "Lunch
    time") carries down to the items beneath it in the same column.

    The columns are Mon-Fri of the requested week; a cell can be empty (no
    choice that session). Prices are embedded in the item text in mixed forms
    ("Cheese Pizza - 1.95", "Wedges £1.60", "Hash Browns x2 1.40") and parsed
    to int pence.
    """
    soup = BeautifulSoup(html, "html.parser")

    grid: Tag | None = None
    for table in soup.find_all("table"):
        header_cells = table.select("thead th")
        if header_cells and any(
            _MENU_HEADER_DATE_RE.search(th.get_text(" ", strip=True)) for th in header_cells
        ):
            grid = table
            break
    if grid is None:
        return {}

    col_dates = _menu_header_dates(grid.select("thead th"), ref_year)
    out: dict[str, list[MenuItem]] = {}
    session: list[str | None] = [None] * len(col_dates)

    for row in grid.select("tbody > tr"):
        for col, cell in enumerate(row.find_all("td", recursive=False)):
            if col >= len(col_dates):
                continue
            day = col_dates[col]
            if day is None:
                continue
            strong = cell.find("strong")
            if strong is None:
                continue
            text = strong.get_text(" ", strip=True)
            if text in _MENU_SESSION_LABELS:
                session[col] = text
                continue
            classes = list(cell.get("class") or [])
            taken = cell.find("img", alt="Taken") is not None or "success" in classes
            if not taken:
                continue
            price_match = _MENU_PRICE_TAIL_RE.search(text)
            amount_pence = round(float(price_match.group(1)) * 100) if price_match else 0
            name = _MENU_PRICE_STRIP_RE.sub("", text).strip() or text
            out.setdefault(day.isoformat(), []).append(
                MenuItem(session=session[col], item=name, amount_pence=amount_pence)
            )
    return out
