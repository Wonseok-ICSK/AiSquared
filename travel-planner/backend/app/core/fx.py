"""Currency conversion: Frankfurter API (live, cached 12h) with a static offline fallback.

There is no implicit FX anywhere in the app; call :func:`convert` explicitly (budgets).
Frankfurter v2 (``/v2/rates``) covers ~160 currencies incl. TWD/VND; the legacy
``api.frankfurter.app`` endpoint is tried if v2 fails.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Literal

import httpx

logger = logging.getLogger(__name__)

RateSource = Literal["live", "static"]

FRANKFURTER_V2_URL = "https://api.frankfurter.dev/v2/rates"
FRANKFURTER_LEGACY_URL = "https://api.frankfurter.app/latest"
HTTP_TIMEOUT_S = 10.0
LIVE_TTL_S = 12 * 3600
STATIC_TTL_S = 10 * 60  # retry live after this long when offline

# APPROXIMATE units per 1 USD (Frankfurter snapshot, 2026-10-06), rounded. Used ONLY when
# the live API is unreachable; results flagged source="static". Refresh occasionally.
STATIC_USD_RATES: dict[str, float] = {
    "USD": 1.0, "KRW": 1346.0, "JPY": 158.0, "CNY": 6.70, "TWD": 31.8, "HKD": 7.85,
    "MOP": 8.09, "MNT": 3598.0, "THB": 33.6, "VND": 25953.0, "PHP": 62.6, "SGD": 1.28,
    "MYR": 4.09, "IDR": 17900.0, "KHR": 4063.0, "LAK": 22363.0, "MMK": 2099.0, "BND": 1.28,
    "INR": 96.2, "LKR": 331.0, "NPR": 154.0, "BDT": 123.0, "PKR": 278.0, "MVR": 15.4,
    "KZT": 451.0, "UZS": 11807.0, "KGS": 87.5, "AED": 3.6725, "QAR": 3.64, "SAR": 3.75,
    "KWD": 0.309, "BHD": 0.376, "OMR": 0.3845, "JOD": 0.709, "ILS": 3.06, "TRY": 49.1,
    "AUD": 1.44, "NZD": 1.78, "FJD": 2.26, "XPF": 106.6, "CAD": 1.42, "MXN": 18.2,
    "BRL": 5.12, "ARS": 1523.0, "CLP": 985.0, "PEN": 3.44, "COP": 3251.0, "EUR": 0.890,
    "GBP": 0.756, "CHF": 0.830, "NOK": 9.62, "SEK": 10.04, "DKK": 6.66, "ISK": 122.2,
    "PLN": 3.90, "CZK": 21.8, "HUF": 328.0, "RON": 4.76, "RSD": 104.7, "RUB": 84.3,
    "UAH": 45.0, "GEL": 2.60, "EGP": 52.4, "MAD": 9.95, "ZAR": 16.6, "KES": 129.6,
    "TZS": 2639.0, "MUR": 48.1,
}


class FxError(ValueError):
    """No live or static rate is available for the currency pair."""


_cache: dict[tuple[str, str], tuple[float, RateSource, float]] = {}
_cache_lock = threading.Lock()


def clear_cache() -> None:
    """Forget cached rates (tests)."""
    with _cache_lock:
        _cache.clear()


def static_rate(from_ccy: str, to_ccy: str) -> float | None:
    """Approximate offline rate (1 ``from_ccy`` = x ``to_ccy``), or ``None`` if unknown."""
    src, dst = from_ccy.upper(), to_ccy.upper()
    if src == dst:
        return 1.0
    if src not in STATIC_USD_RATES or dst not in STATIC_USD_RATES:
        return None
    return STATIC_USD_RATES[dst] / STATIC_USD_RATES[src]


def _fetch_v2(client: httpx.Client, src: str, dst: str) -> float:
    response = client.get(FRANKFURTER_V2_URL, params={"base": src, "quotes": dst})
    response.raise_for_status()
    for row in response.json():
        if row.get("quote") == dst:
            return float(row["rate"])
    raise KeyError(dst)


def _fetch_legacy(client: httpx.Client, src: str, dst: str) -> float:
    response = client.get(FRANKFURTER_LEGACY_URL, params={"from": src, "to": dst})
    response.raise_for_status()
    return float(response.json()["rates"][dst])


def _fetch_live(src: str, dst: str) -> float | None:
    with httpx.Client(timeout=HTTP_TIMEOUT_S, follow_redirects=True) as client:
        for fetch in (_fetch_v2, _fetch_legacy):
            try:
                return fetch(client, src, dst)
            except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
                logger.info("FX %s->%s via %s failed: %s", src, dst, fetch.__name__, exc)
    return None


def get_rate(from_ccy: str, to_ccy: str) -> tuple[float, RateSource]:
    """Rate for 1 ``from_ccy`` in ``to_ccy`` and its source (same currency = ``(1.0, "live")``).

    Raises :class:`FxError` when neither the API nor the static table knows the pair.
    """
    src, dst = from_ccy.upper(), to_ccy.upper()
    if src == dst:
        return 1.0, "live"
    now = time.monotonic()
    with _cache_lock:
        cached = _cache.get((src, dst))
    if cached and cached[2] > now:
        return cached[0], cached[1]

    live = _fetch_live(src, dst)
    if live is not None:
        result: tuple[float, RateSource, float] = (live, "live", now + LIVE_TTL_S)
    else:
        fallback = static_rate(src, dst)
        if fallback is None:
            raise FxError(f"no exchange rate available for {src}->{dst}")
        logger.warning("Using approximate static FX rate for %s->%s", src, dst)
        result = (fallback, "static", now + STATIC_TTL_S)
    with _cache_lock:
        _cache[(src, dst)] = result
    return result[0], result[1]


def convert(amount: float, from_ccy: str, to_ccy: str) -> float:
    """Convert ``amount`` between currencies (identity when equal)."""
    rate, _ = get_rate(from_ccy, to_ccy)
    return amount * rate
