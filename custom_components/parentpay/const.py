"""Constants for the ParentPay integration."""
from __future__ import annotations

from typing import Final

DOMAIN: Final = "parentpay"
MANUFACTURER: Final = "ParentPay"

# --- Endpoints (see docs/endpoints.md) -----------------------------------

LOGIN_URL: Final = "https://app.parentpay.com/public/api/security/authentication/login"
HOME_URL: Final = "https://app.parentpay.com/V3Payer4W3/Payer/Default.aspx"
PAYMENT_ITEMS_URL: Final = "https://app.parentpay.com/V3Payer4W3/Home/PaymentItems/PaymentItems.aspx"
ARCHIVE_URL: Final = "https://app.parentpay.com/V3Payer4VBW3/Consumer/MS_Archive.aspx"
# Weekly meal grid with the real per-item morning/lunch choices and prices.
# GET ?ConsumerId={cid}&Date={YYYY-MM-DD} returns the Mon-Fri week of that date.
MENU_CHOICES_URL: Final = "https://app.parentpay.com/V3Payer4W3/Payer/MenusAndChoices.aspx"

# --- Login form field names ---------------------------------------------

LOGIN_FIELD_USER: Final = "username"
LOGIN_FIELD_PASSWORD: Final = "password"

# NOTE: Archive deep-history (date-range filtering) is an ASP.NET WebForms
# postback and is explicitly deferred to v2. v1 uses MS_Archive.aspx GET
# (recent rows) + the "Recent payments" table on HOME_URL.

# --- Defaults -------------------------------------------------------------

DEFAULT_USER_AGENT: Final = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/145.0.0.0 Safari/537.36"
)

# Rolling window for the per-poll archive search. Wide enough to still
# overlap real transactions across the ~7.5-week summer holiday.
ARCHIVE_WINDOW_DAYS: Final = 60

DEFAULT_POLL_INTERVAL_MIN: Final = 30
DEFAULT_POLL_WINDOW_START: Final = "08:00"
DEFAULT_POLL_WINDOW_END: Final = "16:00"
DEFAULT_PURCHASES_LIST_DEPTH: Final = 10

# --- Config keys ----------------------------------------------------------

# Config key names are deliberately `username` (not `email`) — ParentPay's
# login API accepts either an email address or a numeric username in the
# same `username` field.

CONF_USERNAME: Final = "username"
CONF_PASSWORD: Final = "password"
CONF_POLL_INTERVAL_MIN: Final = "poll_interval_minutes"
CONF_POLL_WINDOW_START: Final = "poll_window_start"
CONF_POLL_WINDOW_END: Final = "poll_window_end"
CONF_PURCHASES_LIST_DEPTH: Final = "purchases_list_depth"

# --- Store keys (versioned) ----------------------------------------------

STORE_KEY_MEALS: Final = "parentpay.meals_v1"
STORE_KEY_PURCHASES: Final = "parentpay.purchases_v1"
STORE_KEY_PAYMENT_DETAILS: Final = "parentpay.payment_details_v1"
STORE_KEY_DISMISSALS: Final = "parentpay.dismissals_v1"
STORE_KEY_BACKFILL: Final = "parentpay.backfill_v1"
STORE_VERSION: Final = 3  # v3: adds dismissals + backfill flag, drops home-page meal cache pollution
