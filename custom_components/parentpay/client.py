"""HTTP client for ParentPay."""
from __future__ import annotations

import logging
from datetime import date, timedelta

import aiohttp

from .const import (
    ARCHIVE_URL,
    ARCHIVE_WINDOW_DAYS,
    DEFAULT_USER_AGENT,
    HOME_URL,
    LOGIN_FIELD_PASSWORD,
    LOGIN_FIELD_USER,
    LOGIN_URL,
    MENU_CHOICES_URL,
    PAYMENT_ITEMS_URL,
)
from .exceptions import ParentPayAuthError
from .models import ArchiveRow, HomeSnapshot, PaymentDetailItem, PaymentItem
from .parsers import (
    parse_archive,
    parse_home_balances,
    parse_home_recent_payments,
    parse_login_response,
    parse_payment_detail,
    parse_payment_items,
    parse_webforms_state,
)

_PAYMENT_DETAIL_URL = (
    "https://app.parentpay.com/V3Payer4VBW3/Consumer/PaymentDetailsViewerFX.aspx"
)

_LOGGER = logging.getLogger(__name__)

_AUTHENTICATED_MARKER = "data-consumer-data"


class ParentPayClient:
    """Async client that encapsulates auth + scraping."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        username: str,
        password: str,
    ) -> None:
        self._session = session
        self._username = username
        self._password = password
        self._logged_in = False

    @property
    def is_logged_in(self) -> bool:
        return self._logged_in

    def _headers(self) -> dict[str, str]:
        return {"User-Agent": DEFAULT_USER_AGENT}

    async def login(self) -> None:
        payload = {
            LOGIN_FIELD_USER: self._username,
            LOGIN_FIELD_PASSWORD: self._password,
        }
        _LOGGER.debug("POST %s (login)", LOGIN_URL)
        async with self._session.post(
            LOGIN_URL,
            data=payload,
            headers=self._headers(),
            allow_redirects=True,
        ) as resp:
            status = resp.status
            try:
                body = await resp.json(content_type=None)
            except aiohttp.ContentTypeError:
                body = {}
        ok, reason = parse_login_response(status=status, payload=body)
        if not ok:
            self._logged_in = False
            raise ParentPayAuthError(reason or "Login failed")
        self._logged_in = True

    async def _authed_get(self, url: str) -> str:
        return await self._authed_request("GET", url)

    async def _authed_post(self, url: str, *, data: dict[str, str]) -> str:
        return await self._authed_request("POST", url, data=data)

    async def _authed_request(
        self,
        method: str,
        url: str,
        *,
        data: dict[str, str] | None = None,
    ) -> str:
        """Perform a request, re-logging-in once if the response looks like the login page."""
        if not self._logged_in:
            await self.login()
        body = await self._raw_request(method, url, data=data)
        if not self._looks_authenticated(body):
            _LOGGER.debug("Session appears expired, re-logging in and retrying %s", url)
            self._logged_in = False
            await self.login()
            body = await self._raw_request(method, url, data=data)
            if not self._looks_authenticated(body):
                raise ParentPayAuthError("Session retry failed")
        return body

    async def _raw_request(
        self,
        method: str,
        url: str,
        *,
        data: dict[str, str] | None = None,
    ) -> str:
        async with self._session.request(
            method,
            url,
            data=data,
            headers=self._headers(),
            allow_redirects=True,
        ) as resp:
            return await resp.text()

    @staticmethod
    def _looks_authenticated(body: str) -> bool:
        return _AUTHENTICATED_MARKER in body

    async def fetch_home(self) -> HomeSnapshot:
        """One-shot fetch: balances + recent parent-account payments.

        The home page also lists today's meal price as a generic placeholder, but
        v2 ignores that — meal data comes from the archive store (backfill plus
        ongoing GETs) so we always have real item names.
        """
        body = await self._authed_get(HOME_URL)
        return HomeSnapshot(
            balances=parse_home_balances(body),
            recent_payments=parse_home_recent_payments(body),
        )

    async def fetch_payment_items(self) -> list[PaymentItem]:
        body = await self._authed_get(PAYMENT_ITEMS_URL)
        return parse_payment_items(body)

    async def fetch_menu_choices(self, consumer_id: str, date_str: str) -> str:
        """Fetch the weekly MenusAndChoices grid HTML for one child.

        ``date_str`` is any ``YYYY-MM-DD`` in the wanted week — the page returns
        the whole Mon-Fri week it falls in. Returns raw HTML for
        ``parsers.parse_menu_choices`` (kept separate so the parser stays pure).
        """
        url = f"{MENU_CHOICES_URL}?ConsumerId={consumer_id}&Date={date_str}"
        return await self._authed_get(url)

    async def fetch_payment_detail(
        self, tid: str, u: str
    ) -> list[PaymentDetailItem]:
        """Fetch a single receipt page and return all its line items.

        One receipt URL (TID + U) can list multiple line items sharing the
        same parent payment. The caller should cache results by TID so each
        transaction is fetched at most once.
        """
        url = f"{_PAYMENT_DETAIL_URL}?TID={tid}&U={u}"
        body = await self._authed_get(url)
        return parse_payment_detail(body)

    async def fetch_archive(self) -> list[ArchiveRow]:
        """Fetch the last ~60 days of archive rows.

        As of 2026, ParentPay's raw GET of MS_Archive.aspx returns an empty
        "No results found" panel — rows only come back after a cmdSearch POST
        with a date range. This method posts a rolling window so the
        coordinator still picks up new meals + purchases between polls.

        The window is 60 days rather than 30 so it still overlaps real data
        across a long school holiday (the summer break runs ~7.5 weeks). An
        empty window is handled gracefully by ``parse_archive`` regardless —
        the wider window is about keeping the rolling poll useful, not about
        avoiding a crash.
        """
        today = date.today()
        return await self.fetch_archive_range(
            today - timedelta(days=ARCHIVE_WINDOW_DAYS), today
        )

    async def fetch_archive_range(
        self, start: date, end: date
    ) -> list[ArchiveRow]:
        """Fetch the archive grid filtered to a date range.

        Two HTTP calls: GET to scrape the WebForms hidden state, then POST
        with __EVENTTARGET=ctl00$cmdSearch and the date strings to trigger the
        server-side filter. Returns the rows parsed from the POST response.
        """
        get_body = await self._authed_get(ARCHIVE_URL)
        state = parse_webforms_state(get_body)
        post_body = {
            "__EVENTTARGET": "ctl00$cmdSearch",
            "__EVENTARGUMENT": "",
            "__VIEWSTATE": state.viewstate,
            "__VIEWSTATEGENERATOR": state.viewstategenerator,
            "__EVENTVALIDATION": state.eventvalidation,
            "ctl00$selChoosePupil": "0",
            "ctl00$selChooseService": "0",
            "ctl00$txtChooseStartDate": start.strftime("%d/%m/%Y"),
            "ctl00$txtChooseEndDate": end.strftime("%d/%m/%Y"),
        }
        body = await self._authed_post(ARCHIVE_URL, data=post_body)
        return parse_archive(body)
