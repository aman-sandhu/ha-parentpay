"""DataUpdateCoordinator for ParentPay."""
from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Coroutine
from datetime import date, time, timedelta
from typing import Any, TypeVar

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .client import ParentPayClient
from .const import (
    CONF_POLL_INTERVAL_MIN,
    CONF_POLL_WINDOW_END,
    CONF_POLL_WINDOW_START,
    CONF_PURCHASES_LIST_DEPTH,
    DEFAULT_POLL_INTERVAL_MIN,
    DEFAULT_POLL_WINDOW_END,
    DEFAULT_POLL_WINDOW_START,
    DEFAULT_PURCHASES_LIST_DEPTH,
    DOMAIN,
)
from .exceptions import ParentPayAuthError, ParentPayError
from .models import ArchiveRow, MenuItem
from .parsers import extract_receipt_ids, parse_menu_choices
from .store import ParentPayStore

_LOGGER = logging.getLogger(__name__)

_T = TypeVar("_T")

# Only itemise meals for recent weeks. The backfill holds ~12 months of meal
# days and the card shows ~30 days, so fetching every historical week's
# MenusAndChoices page would be a request storm for no visible benefit.
_MEAL_DETAIL_WEEKS = 10


class ParentPayCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    def __init__(
        self,
        hass: HomeAssistant,
        *,
        client: ParentPayClient,
        options: dict[str, Any],
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(
                minutes=options.get(CONF_POLL_INTERVAL_MIN, DEFAULT_POLL_INTERVAL_MIN)
            ),
        )
        self._client = client
        self._options = options
        self.store = ParentPayStore(hass)
        self._first_run_done = False
        # In-memory meal itemisation cache (child_id|date_iso -> items). Historical
        # weeks are immutable so they are fetched once per HA run; the current week
        # refreshes every poll. Not persisted — repopulated cheaply on restart.
        self._meal_details: dict[str, list[MenuItem]] = {}
        self._meal_weeks_done: set[str] = set()
        # Names of supplementary fetches that failed on the most recent
        # poll and fell back to stale/empty data. Surfaced in diagnostics.
        self.degraded_legs: list[str] = []

    async def async_setup(self) -> None:
        await self.store.async_load()

    def _in_window(self) -> bool:
        window_start = _parse_hhmm(
            self._options.get(CONF_POLL_WINDOW_START, DEFAULT_POLL_WINDOW_START)
        )
        window_end = _parse_hhmm(
            self._options.get(CONF_POLL_WINDOW_END, DEFAULT_POLL_WINDOW_END)
        )
        now_t = dt_util.now().time()
        return window_start <= now_t <= window_end

    async def _async_update_data(self) -> dict[str, Any]:
        if self._first_run_done and not self._in_window():
            _LOGGER.debug("Skipping poll — outside poll window")
            return self.data or {
                "balances": [],
                "items": [],
                "meals": [],
                "purchases": [],
                "meal_items": {},
            }

        previous = self.data or {}
        self.degraded_legs = []

        try:
            await self._maybe_run_backfill()

            # Home is the primary leg — balances have no persistent store to
            # fall back on, so a failure here is a genuine poll failure.
            home = await self._client.fetch_home()

            # Supplementary legs degrade independently. A ParentPay-side
            # failure in one must not discard what the others returned: that
            # is what turned a single empty archive window into a blackout of
            # every entity, balances included (see CLAUDE.md, 2026-08-22).
            items = await self._fetch_optional(
                self._client.fetch_payment_items(),
                leg="payment_items",
                fallback=previous.get("items", []),
            )
            archive_rows = await self._fetch_optional(
                self._client.fetch_archive(),
                leg="archive",
                fallback=[],
            )

            enriched_payments = await self._enrich_recent_payments(
                home.recent_payments
            )

            await self.store.async_merge(enriched_payments)
            await self.store.async_merge(archive_rows)

            # Itemise recent meals from MenusAndChoices (best-effort; degrades
            # to the coarse archive summary and never fails the poll).
            await self._enrich_meal_details()

            self._first_run_done = True
            return {
                "balances": home.balances,
                "items": items,
                "meals": self.store.meals,
                "purchases": self.store.purchases,
                "meal_items": dict(self._meal_details),
            }
        except ParentPayAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except ParentPayError as err:
            raise UpdateFailed(str(err)) from err

    async def _fetch_optional(
        self,
        coro: Coroutine[Any, Any, _T],
        *,
        leg: str,
        fallback: _T,
    ) -> _T:
        """Await a supplementary fetch, degrading to ``fallback`` on failure.

        Auth errors are re-raised: those mean the session is genuinely dead and
        HA should start a reauth flow rather than quietly serve stale data.
        Every other ParentPay error is logged at WARNING (every poll, unlike
        HA's coordinator-level error which logs only on the first failure) and
        the rest of the poll continues.
        """
        try:
            return await coro
        except ParentPayAuthError:
            raise
        except ParentPayError as err:
            self.degraded_legs.append(leg)
            _LOGGER.warning(
                "ParentPay %s fetch failed, continuing with the rest of the poll: %s",
                leg,
                err,
            )
            return fallback

    async def _enrich_meal_details(self) -> None:
        """Fetch per-day meal itemisation for the recent meal days we hold.

        One MenusAndChoices request returns a whole Mon-Fri week, so we fetch at
        most once per (child, week). Historical weeks are cached for the life of
        the process; the current week is refreshed every poll. Weeks older than
        ``_MEAL_DETAIL_WEEKS`` are skipped entirely. Any failure degrades to the
        coarse archive data already in the store and never fails the poll.
        """
        meals = self.store.meals
        if not meals:
            return

        today = dt_util.now().date()
        current_monday = today - timedelta(days=today.weekday())
        cutoff_monday = current_monday - timedelta(weeks=_MEAL_DETAIL_WEEKS)

        weeks: dict[tuple[str, date], list[str]] = defaultdict(list)
        for row in meals:
            child_id = str(row.get("child_id") or "")
            day_iso = str(row.get("date") or "")
            if not child_id or not day_iso:
                continue
            try:
                day = date.fromisoformat(day_iso)
            except ValueError:
                continue
            monday = day - timedelta(days=day.weekday())
            if monday < cutoff_monday:
                continue
            weeks[(child_id, monday)].append(day_iso)

        for (child_id, monday), day_isos in weeks.items():
            week_key = f"{child_id}|{monday.isoformat()}"
            is_current = monday >= current_monday
            if week_key in self._meal_weeks_done and not is_current:
                continue
            try:
                html = await self._client.fetch_menu_choices(child_id, max(day_isos))
                parsed = parse_menu_choices(html, monday.year)
            except Exception as err:  # itemisation is best-effort, never fatal
                # Never fail the poll for meal itemisation: the coarse archive
                # summary is already in the store and shown as a fallback.
                if "meal_details" not in self.degraded_legs:
                    self.degraded_legs.append("meal_details")
                _LOGGER.warning(
                    "ParentPay meal-detail fetch failed for %s: %s", week_key, err
                )
                continue
            for day_iso, item_list in parsed.items():
                self._meal_details[f"{child_id}|{day_iso}"] = item_list
            if not is_current:
                self._meal_weeks_done.add(week_key)

    async def _maybe_run_backfill(self) -> None:
        """Run the one-shot 12-month backfill if it hasn't succeeded yet.

        On failure, log a warning and leave the done flag unset so the next
        scheduled poll retries the whole sequence. No exponential backoff —
        the poll interval already throttles retries.
        """
        if self.store.backfill_done:
            return
        today = dt_util.now().date()
        start = today - timedelta(days=365)
        try:
            rows = await self._client.fetch_archive_range(start, today)
        except ParentPayError as err:
            _LOGGER.warning(
                "Archive backfill failed, will retry next poll: %s", err
            )
            return
        await self.store.async_merge(rows)
        await self.store.async_mark_backfill_done()

    async def _enrich_recent_payments(
        self, rows: list[ArchiveRow]
    ) -> list[ArchiveRow]:
        """Replace truncated home-page payment rows with enriched detail.

        The home-page "Recent payments" mini-table truncates item names and
        doesn't expose child ids. We resolve each row via the receipt URL
        (which points at PaymentDetailsViewerFX.aspx) and cache the result
        by TID so each transaction is fetched at most once.

        Rows that can't be enriched (no receipt URL, fetch fails, or the
        receipt doesn't list the expected TID) are dropped rather than
        polluting the store with truncated, unassigned entries.
        """
        out: list[ArchiveRow] = []
        for row in rows:
            if not row.receipt_url:
                continue
            ids = extract_receipt_ids(row.receipt_url)
            if ids is None:
                continue
            tid, u = ids
            cached = self.store.get_payment_detail(tid)
            if cached is None:
                try:
                    details = await self._client.fetch_payment_detail(tid, u)
                except ParentPayError as err:
                    _LOGGER.debug(
                        "Failed to enrich payment TID=%s U=%s: %s", tid, u, err
                    )
                    continue
                await self.store.async_store_payment_details(details)
                cached = self.store.get_payment_detail(tid)
            if cached is None or not cached.get("child_id"):
                continue
            out.append(
                ArchiveRow(
                    child_id=str(cached["child_id"]),
                    child_name=str(cached.get("child_name") or ""),
                    date_paid=date.fromisoformat(str(cached["date"])),
                    item=str(cached["item"]),
                    amount_pence=int(cached["amount_pence"]),
                    payment_method="Parent Account",
                    status=cached.get("status"),
                    receipt_url=row.receipt_url,
                )
            )
        return out

    def meals_for_child(self, child_id: str) -> list[dict[str, Any]]:
        """Return one event dict per (child_id, date) with all item names concatenated."""
        groups: dict[str, list[str]] = defaultdict(list)
        for row in self.store.meals:
            if row["child_id"] != child_id:
                continue
            groups[row["date"]].append(row["item"])
        events: list[dict[str, Any]] = []
        for date_str, items in sorted(groups.items()):
            events.append(
                {
                    "date": date_str,
                    "summary": ", ".join(items),
                }
            )
        return events

    def purchases_for_child(self, child_id: str) -> list[dict[str, Any]]:
        """Return purchases for child, newest-first, capped at purchases_list_depth."""
        depth = self._options.get(CONF_PURCHASES_LIST_DEPTH, DEFAULT_PURCHASES_LIST_DEPTH)
        rows = [r for r in self.store.purchases if r["child_id"] == child_id]
        rows.sort(key=lambda r: r["date"], reverse=True)
        return rows[:depth]


def _parse_hhmm(value: str) -> time:
    hh, mm = value.split(":")
    return time(int(hh), int(mm))
