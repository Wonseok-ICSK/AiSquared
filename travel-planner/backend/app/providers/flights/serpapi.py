"""SerpApi Google Flights provider (DESIGN §13.1, docs/research/flight-apis.md).

One ``GET https://serpapi.com/search.json?engine=google_flights`` call per :class:`FlightQuery`
(one SerpApi credit; cached and errored searches are free). Metro codes are expanded to
comma-separated airport lists (``SEL`` -> ``ICN,GMP``), which SerpApi accepts natively.

Price basis: SerpApi documents ``price`` only as "This ticket price in the selected currency".
It mirrors the Google Flights result list, whose displayed price is the **total for all
passengers** (verified for Google Flights in the research notes: 2 adults = exactly 2x the
1-adult price). So ``price`` is stored as ``price_total`` and ``price_per_person`` =
total / adults.

Round trips: the first call returns *outbound* options, each priced at the cheapest
round-trip total; offers therefore have ``inbound=None`` and ``return_date`` set. Each offer's
``raw`` keeps the ``departure_token`` (and the request parameters, without the API key) so
:meth:`SerpApiProvider.get_flight_details` can fetch the return options later (one extra
credit). A return flight is attached only when one is priced exactly at the offer's listed
total, so an enriched offer's price always belongs to its outbound+return pair. One-way
options carry a ``booking_token`` instead.

``raw["price_insights"]`` holds the small summary (``lowest_price``, ``price_level``,
``typical_price_range``) on every offer; the ``price_history`` (latest
:data:`PRICE_HISTORY_MAX_POINTS` points) is stored once per search, on its cheapest offer.

Quota: SerpApi answers HTTP 429 both for the hourly limit and for a used-up monthly plan. After
a 429 the free Account API (``account.json``) tells the two apart and the provider pauses
itself for that API key (in memory): :meth:`SerpApiProvider.status` then reports it as not
usable until the next hour, or until the plan renews, so scheduled runs skip it instead of
failing every check. A later successful call lifts the pause.

Response parsing lives in pure functions (:func:`parse_offers`, :func:`parse_leg`,
:func:`check_response`) unit-tested against captured-shape fixtures; the HTTP call is the thin
:meth:`SerpApiProvider._fetch`. The API key travels only as a query parameter (SerpApi has no
header auth) and is never logged: ``main.configure_logging`` quiets ``httpx``, a filter masks
``api_key=`` in ``httpx`` records anyway (:func:`install_httpx_log_redaction`), and error
messages built here are redacted with :func:`redact`.

Options (``RuntimeSettings.provider_options["serpapi"]``): ``deep_search`` (bool, default
False) — results identical to the Google Flights browser page, slower responses.
"""

from __future__ import annotations

import copy
import hashlib
import logging
import re
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from typing import Any, NamedTuple
from urllib.parse import urlencode

import httpx

from app.core.airports import airports_for
from app.providers.flights.base import (
    FlightLeg,
    FlightOffer,
    FlightProvider,
    FlightQuery,
    FlightSegment,
    ProviderBlocked,
    ProviderError,
    ProviderNotConfigured,
    ProviderStatus,
    leg_key,
    normalize_flight_number,
    query_for_offer,
)
from app.providers.flights.registry import register
from app.timeutil import utc_to_local

logger = logging.getLogger(__name__)

SERPAPI_URL = "https://serpapi.com/search.json"
ACCOUNT_URL = "https://serpapi.com/account.json"  # free; not counted against the quota
ENGINE = "google_flights"
LANGUAGE = "en"
COUNTRY = "kr"

TRIP_TYPE_ROUND = 1
TRIP_TYPE_ONE_WAY = 2
TRAVEL_CLASS: dict[str, int] = {"economy": 1, "premium_economy": 2, "business": 3, "first": 4}
# SerpApi ``stops``: 0 = any, 1 = nonstop only, 2 = 1 stop or fewer, 3 = 2 stops or fewer.
STOPS_ANY = 0
STOPS_NONSTOP = 1
_MAX_STOPS_PARAM = 3

RESULT_SECTIONS = ("best_flights", "other_flights")
INSIGHT_SUMMARY_KEYS = ("lowest_price", "price_level", "typical_price_range")
PRICE_HISTORY_MAX_POINTS = 30  # Google reports ~daily points; keep about the last month
PRICE_MATCH_TOLERANCE = 0.5  # SerpApi prices are whole units of the currency

RATE_LIMIT_PAUSE = timedelta(hours=1)
"""Pause after HTTP 429 when the account still has searches left (hourly limit)."""
EXHAUSTED_PAUSE = timedelta(hours=24)
"""Pause when the searches are used up and the Account API gives no renewal date."""
MAX_PAUSE = timedelta(days=32)
ACCOUNT_TIMEOUT_S = 10.0

_NO_RESULTS_RE = re.compile(r"hasn['’]?t returned any results", re.IGNORECASE)
_CARRIER_RE = re.compile(r"^([A-Z0-9]{2})\d")
_LOCAL_TIME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})[ T](\d{1,2}):(\d{2})")
_API_KEY_PARAM_RE = re.compile(r"(api_key=)[^&\s\"']+")
_BAGGAGE_RE = re.compile(r"\bbag", re.IGNORECASE)
_AUTH_STATUSES = frozenset({401, 403})
_RATE_LIMIT_STATUS = 429
_REDACTED = "***"


# --------------------------------------------------------------------------- request building


def stops_param(query: FlightQuery) -> int:
    """SerpApi ``stops`` value for the query (``direct_only`` wins over ``max_stops``)."""
    if query.direct_only:
        return STOPS_NONSTOP
    if query.max_stops is None or query.max_stops < 0:
        return STOPS_ANY
    param = query.max_stops + 1
    return param if param <= _MAX_STOPS_PARAM else STOPS_ANY


def airport_ids(code: str) -> str:
    """``departure_id``/``arrival_id`` value: metro codes expand to ``"ICN,GMP"``."""
    return ",".join(airports_for(code))


def build_params(query: FlightQuery, *, deep_search: bool = False) -> dict[str, Any]:
    """Search parameters for one query, **without** ``api_key`` (safe to log / store)."""
    params: dict[str, Any] = {
        "engine": ENGINE,
        "departure_id": airport_ids(query.origin),
        "arrival_id": airport_ids(query.destination),
        "outbound_date": query.depart_date.isoformat(),
        "type": TRIP_TYPE_ROUND if query.is_round_trip else TRIP_TYPE_ONE_WAY,
        "travel_class": TRAVEL_CLASS[query.cabin],
        "adults": query.adults,
        "stops": stops_param(query),
        "currency": query.currency.upper(),
        "hl": LANGUAGE,
        "gl": COUNTRY,
    }
    if query.return_date is not None:
        params["return_date"] = query.return_date.isoformat()
    if deep_search:
        params["deep_search"] = "true"
    return params


def redact(text: str, secret: str | None) -> str:
    """``text`` with every occurrence of ``secret`` (the API key) masked."""
    if not secret:
        return text
    return text.replace(secret, _REDACTED)


class _RedactApiKeyFilter(logging.Filter):
    """Masks ``api_key=...`` in ``httpx`` records (httpx logs full request URLs at INFO)."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if "api_key=" in message:
            record.msg = _API_KEY_PARAM_RE.sub(rf"\1{_REDACTED}", message)
            record.args = None
        return True


def install_httpx_log_redaction() -> None:
    """Attach the ``api_key`` redaction filter to the ``httpx`` logger (idempotent).

    ``main.configure_logging`` already raises ``httpx`` to WARNING; this is defence in depth
    for other entry points (scripts, tests) that log at INFO.
    """
    httpx_logger = logging.getLogger("httpx")
    if not any(type(f).__name__ == _RedactApiKeyFilter.__name__ for f in httpx_logger.filters):
        httpx_logger.addFilter(_RedactApiKeyFilter())


# --------------------------------------------------------------------------- response checks


def error_message(data: Mapping[str, Any]) -> str | None:
    """The top-level ``error`` message of a SerpApi response, if any."""
    message = data.get("error")
    if message is None:
        return None
    return str(message).strip() or None


def is_no_results_message(message: str | None) -> bool:
    """True for "Google Flights hasn't returned any results for this query." (an empty result)."""
    return bool(message) and _NO_RESULTS_RE.search(message or "") is not None


def check_response(status_code: int, data: Mapping[str, Any]) -> bool:
    """Raise the matching ``ProviderError`` for a failed search.

    Returns ``True`` when the search succeeded but Google returned no flights (SerpApi reports
    that as an ``error`` message on an otherwise successful search), ``False`` when results
    should be parsed.
    """
    message = error_message(data)
    detail = f": {message}" if message else ""
    if status_code in _AUTH_STATUSES:
        raise ProviderNotConfigured(f"SerpApi rejected the API key (HTTP {status_code}){detail}")
    if status_code == _RATE_LIMIT_STATUS:
        raise ProviderBlocked(
            f"SerpApi hourly limit reached or searches used up (HTTP 429){detail}"
        )
    if is_no_results_message(message):
        return True
    if status_code >= 400 or message:
        raise ProviderError(f"SerpApi error (HTTP {status_code}){detail or ': unknown error'}")
    status = (data.get("search_metadata") or {}).get("status")
    if status == "Error":
        raise ProviderError("SerpApi search failed (search_metadata.status = Error)")
    return False


# --------------------------------------------------------------------------- response parsing


def local_iso(value: Any) -> str | None:
    """SerpApi local time ``"2026-11-20 09:05"`` -> ``"2026-11-20T09:05"`` (never converted)."""
    match = _LOCAL_TIME_RE.match(value.strip()) if isinstance(value, str) else None
    if match is None:
        return None
    day, hour, minute = match.groups()
    return f"{day}T{int(hour):02d}:{minute}"


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _price(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    return float(value)


def parse_segment(flight: Mapping[str, Any]) -> FlightSegment | None:
    """One ``flights[]`` entry -> :class:`FlightSegment` (``None`` if airports/times are missing)."""
    departure = flight.get("departure_airport") or {}
    arrival = flight.get("arrival_airport") or {}
    dep_code, arr_code = departure.get("id"), arrival.get("id")
    dep_time, arr_time = local_iso(departure.get("time")), local_iso(arrival.get("time"))
    if not (dep_code and arr_code and dep_time and arr_time):
        return None
    raw_number = flight.get("flight_number")
    has_number = isinstance(raw_number, str) and raw_number.strip()
    number = normalize_flight_number(raw_number) if has_number else None
    carrier = _CARRIER_RE.match(number) if number else None
    return FlightSegment(
        carrier_code=carrier.group(1) if carrier else None,
        carrier_name=flight.get("airline") or None,
        flight_number=number,
        departure_airport=str(dep_code).upper(),
        arrival_airport=str(arr_code).upper(),
        departure_time=dep_time,
        arrival_time=arr_time,
        duration_minutes=_int_or_none(flight.get("duration")),
        operating_carrier=flight.get("plane_and_crew_by") or None,
        aircraft=flight.get("airplane") or None,
    )


def _layover_entries(item: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [entry for entry in item.get("layovers") or [] if isinstance(entry, Mapping)]


def _leg_duration(item: Mapping[str, Any], segments: list[FlightSegment]) -> int | None:
    """``total_duration`` (flights + layovers), else the sum of its parts when all are known."""
    total = _int_or_none(item.get("total_duration"))
    if total is not None:
        return total
    parts = [s.duration_minutes for s in segments]
    parts += [_int_or_none(entry.get("duration")) for entry in _layover_entries(item)]
    if any(part is None for part in parts):
        return None
    return sum(part for part in parts if part is not None)


def parse_leg(item: Mapping[str, Any]) -> FlightLeg | None:
    """A ``best_flights``/``other_flights`` item -> :class:`FlightLeg` (``None`` if unusable)."""
    flights = item.get("flights") or []
    parsed = [parse_segment(f) for f in flights if isinstance(f, Mapping)]
    if not parsed or any(s is None for s in parsed):
        return None
    segments: list[FlightSegment] = [s for s in parsed if s is not None]
    layovers = [
        str(entry["id"]).upper() for entry in _layover_entries(item) if entry.get("id")
    ] or [s.arrival_airport for s in segments[:-1]]
    return FlightLeg(
        departure_airport=segments[0].departure_airport,
        arrival_airport=segments[-1].arrival_airport,
        departure_time=segments[0].departure_time,
        arrival_time=segments[-1].arrival_time,
        duration_minutes=_leg_duration(item, segments),
        stops=len(segments) - 1,
        airlines=list(dict.fromkeys(s.carrier_name for s in segments if s.carrier_name)),
        flight_numbers=[s.flight_number for s in segments if s.flight_number],
        segments=segments,
        layovers=layovers,
    )


def trim_price_insights(insights: Any, *, with_history: bool = True) -> dict[str, Any] | None:
    """``price_insights`` reduced for ``raw``: the summary keys, plus (``with_history``) the
    latest :data:`PRICE_HISTORY_MAX_POINTS` ``price_history`` points. ``None`` if empty."""
    if not isinstance(insights, Mapping):
        return None
    trimmed = {key: copy.deepcopy(insights[key]) for key in INSIGHT_SUMMARY_KEYS if key in insights}
    history = insights.get("price_history")
    if with_history and isinstance(history, list) and history:
        trimmed["price_history"] = copy.deepcopy(history[-PRICE_HISTORY_MAX_POINTS:])
    return trimmed or None


def _baggage(item: Mapping[str, Any]) -> str | None:
    notes = [str(e) for e in item.get("extensions") or [] if _BAGGAGE_RE.search(str(e))]
    return "; ".join(notes) or None


def fallback_search_url(query: FlightQuery) -> str:
    """Google Flights link used when the response lacks ``search_metadata.google_flights_url``."""
    text = (
        f"Flights from {airport_ids(query.origin)} to {airport_ids(query.destination)}"
        f" on {query.depart_date.isoformat()}"
    )
    if query.return_date is not None:
        text += f" through {query.return_date.isoformat()}"
    return "https://www.google.com/travel/flights?" + urlencode(
        {"q": text, "curr": query.currency.upper(), "hl": LANGUAGE, "gl": COUNTRY}
    )


def _offer_raw(
    item: Mapping[str, Any],
    section: str,
    insights: dict[str, Any] | None,
    search_id: str | None,
    request_params: Mapping[str, Any] | None,
) -> dict[str, Any]:
    raw: dict[str, Any] = {"source": section, "type": item.get("type")}
    for token in ("departure_token", "booking_token"):
        if item.get(token):
            raw[token] = item[token]
    if insights:
        raw["price_insights"] = copy.deepcopy(insights)
    if search_id:
        raw["search_id"] = search_id
    if request_params:
        raw["request"] = {k: v for k, v in request_params.items() if k != "api_key"}
    return raw


def parse_offers(
    data: Mapping[str, Any],
    query: FlightQuery,
    *,
    request_params: Mapping[str, Any] | None = None,
    provider_key: str = "serpapi",
) -> list[FlightOffer]:
    """All priced itineraries of a SerpApi Google Flights response, cheapest per ``offer_key``.

    ``best_flights`` come first, then ``other_flights``; rows without a price or with
    incomplete segments are skipped. ``price`` is the total for all passengers (see module doc).
    Every offer gets the ``price_insights`` summary; only the cheapest one also gets the
    (capped) ``price_history``.
    """
    metadata = data.get("search_metadata") or {}
    url = metadata.get("google_flights_url") or fallback_search_url(query)
    currency = str((data.get("search_parameters") or {}).get("currency") or query.currency).upper()
    insights = trim_price_insights(data.get("price_insights"), with_history=False)
    return_date = query.return_date.isoformat() if query.return_date is not None else None
    adults = max(query.adults, 1)

    offers: dict[str, FlightOffer] = {}
    skipped = 0
    for section in RESULT_SECTIONS:
        for item in data.get(section) or []:
            price = _price(item.get("price")) if isinstance(item, Mapping) else None
            leg = parse_leg(item) if price is not None else None
            if price is None or leg is None:
                skipped += 1
                continue
            offer = FlightOffer(
                provider=provider_key,
                outbound=leg,
                inbound=None,
                price_total=price,
                price_per_person=round(price / adults, 2),
                currency=currency,
                cabin=query.cabin,
                adults=adults,
                baggage=_baggage(item),
                booking_url=url,
                source_url=url,
                return_date=return_date,
                raw=_offer_raw(item, section, insights, metadata.get("id"), request_params),
            )
            key = offer.offer_key()
            if key not in offers or offer.price_total < offers[key].price_total:
                offers[key] = offer
    if skipped:
        logger.debug("SerpApi: skipped %d unpriced/incomplete result rows", skipped)
    result = list(offers.values())
    _attach_price_history(result, data.get("price_insights"))
    return result


def _attach_price_history(offers: list[FlightOffer], insights: Any) -> None:
    """Store the full (capped) ``price_insights`` once per search, on its cheapest offer."""
    full = trim_price_insights(insights)
    if not offers or not full or "price_history" not in full:
        return
    cheapest = min(offers, key=lambda offer: offer.price_total)  # first one on ties
    cheapest.raw["price_insights"] = full


class ReturnOption(NamedTuple):
    """One return flight of a ``departure_token`` response, priced at the round-trip total."""

    price: float
    leg: FlightLeg
    item: Mapping[str, Any]


def return_options(data: Mapping[str, Any]) -> list[ReturnOption]:
    """Priced, complete return options of a ``departure_token`` response, in list order."""
    options: list[ReturnOption] = []
    for section in RESULT_SECTIONS:
        for item in data.get(section) or []:
            price = _price(item.get("price")) if isinstance(item, Mapping) else None
            leg = parse_leg(item) if price is not None else None
            if price is not None and leg is not None:
                options.append(ReturnOption(price, leg, item))
    return options


def pick_return_option(
    options: Sequence[ReturnOption], round_trip_total: float
) -> ReturnOption | None:
    """The first return option priced at ``round_trip_total`` (the outbound offer's listed
    total), or ``None`` when that fare is no longer offered. Never falls back to another
    price: the chosen pair must cost exactly what the offer says."""
    for option in options:
        if abs(option.price - round_trip_total) < PRICE_MATCH_TOLERANCE:
            return option
    return None


def round_trip_match_key(offer: FlightOffer) -> str:
    """``offer_key`` with the inbound leg reduced to ``RT:<return date>``.

    Search results never carry an inbound leg, so this is the key to compare when re-finding
    an offer whose inbound was filled by ``get_flight_details``. Equals ``offer.offer_key()``
    for one-way offers and for round trips without inbound details.
    """
    if offer.inbound is None:
        return offer.offer_key()
    return_date = offer.return_date or offer.inbound.departure_time[:10]
    return replace(offer, inbound=None, return_date=return_date).offer_key()


# --------------------------------------------------------------------------- quota pause


@dataclass(frozen=True)
class QuotaPause:
    """The provider is paused for one API key until ``until`` (aware UTC) after HTTP 429."""

    until: datetime
    reason: str

    def describe(self) -> str:
        """Human-readable status line (local time)."""
        resume = utc_to_local(self.until).strftime("%Y-%m-%d %H:%M")
        return f"SerpApi paused until {resume}: {self.reason}"


_pauses: dict[str, QuotaPause] = {}
_pauses_lock = threading.Lock()


def _renewal_start(value: Any) -> datetime | None:
    """``plan_renewal_date`` ``"2026-11-06"`` -> that day 00:00 UTC (``None`` if missing/bad)."""
    if not isinstance(value, str):
        return None
    try:
        day = date.fromisoformat(value.strip()[:10])
    except ValueError:
        return None
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


def quota_pause(account: Mapping[str, Any] | None, now: datetime) -> QuotaPause:
    """How long to pause after HTTP 429, judged from the Account API response (or ``None``).

    No searches left -> until the plan renews (at least :data:`RATE_LIMIT_PAUSE`, at most
    :data:`MAX_PAUSE`; :data:`EXHAUSTED_PAUSE` without a renewal date). Otherwise (hourly
    limit, or the account could not be read) -> :data:`RATE_LIMIT_PAUSE`.
    """
    left = _int_or_none(account.get("total_searches_left")) if account else None
    if account is None or left is None or left > 0:
        reason = "hourly search limit reached" if left is not None else "search limit reached"
        return QuotaPause(now + RATE_LIMIT_PAUSE, f"{reason} (HTTP 429)")
    renewal = _renewal_start(account.get("plan_renewal_date"))
    until = renewal if renewal is not None else now + EXHAUSTED_PAUSE
    until = min(max(until, now + RATE_LIMIT_PAUSE), now + MAX_PAUSE)
    renews = f"; plan renews {renewal.date().isoformat()}" if renewal is not None else ""
    return QuotaPause(until, f"no SerpApi searches left this month (HTTP 429{renews})")


def _key_id(api_key: str) -> str:
    """Stable, non-reversible id of an API key (pauses are per key, keys never stored)."""
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]


def active_pause(api_key: str, now: datetime | None = None) -> QuotaPause | None:
    """The current pause for ``api_key``, if any (expired pauses are dropped)."""
    now = now or datetime.now(UTC)
    key_id = _key_id(api_key)
    with _pauses_lock:
        pause = _pauses.get(key_id)
        if pause is not None and pause.until <= now:
            del _pauses[key_id]
            return None
        return pause


def _set_pause(api_key: str, pause: QuotaPause | None) -> None:
    with _pauses_lock:
        if pause is None:
            _pauses.pop(_key_id(api_key), None)
        else:
            _pauses[_key_id(api_key)] = pause


def clear_quota_pauses() -> None:
    """Forget every quota pause (tests; a restart does the same)."""
    with _pauses_lock:
        _pauses.clear()


# --------------------------------------------------------------------------- provider


@register
class SerpApiProvider(FlightProvider):
    """Google Flights results through the SerpApi JSON API (API key ``serpapi``)."""

    key = "serpapi"
    display_name = "Google Flights (SerpApi)"
    description = (
        "Google Flights results via SerpApi's JSON API. Needs a SerpApi key "
        "(free plan: 250 searches/month, 50/hour; one search per date/airport combination). "
        "Prices are Google's displayed fares, round-trip total for all passengers."
    )
    requires_api_key = True
    api_key_name = "serpapi"
    supports_metro_codes = True
    # Free plan = 250 searches/month: one search per check (exact dates) keeps two daily
    # checks of ~4 trips inside the quota. Flexible dates are covered by google_flights.
    max_queries_per_run = 1
    enabled_by_default = True  # reports "not configured" (and is skipped) without a key

    def status(self) -> ProviderStatus:
        """Usable when the ``serpapi`` secret is set and no quota pause is active."""
        api_key = self._api_key()
        if api_key is None:
            return ProviderStatus(
                configured=False,
                message=(
                    "SerpApi API key is not set "
                    "(Settings → API keys; free plan 250 searches/month)"
                ),
            )
        pause = active_pause(api_key)
        if pause is not None:
            return ProviderStatus(configured=False, message=pause.describe())
        return ProviderStatus(configured=True, message="API key configured")

    def search_flights(self, query: FlightQuery) -> list[FlightOffer]:
        """One SerpApi call for ``query``; outbound options priced at the round-trip total."""
        params = build_params(query, deep_search=self._deep_search())
        data = self._fetch(params)
        if data is None:
            return []
        offers = parse_offers(data, query, request_params=params, provider_key=self.key)
        logger.info(
            "SerpApi %s->%s %s%s: %d offers",
            params["departure_id"],
            params["arrival_id"],
            params["outbound_date"],
            f"/{params['return_date']}" if "return_date" in params else "",
            len(offers),
        )
        return offers

    def get_flight_details(self, offer: FlightOffer) -> FlightOffer:
        """Fill the inbound leg of a round-trip offer via its ``departure_token`` (1 credit).

        Attaches the return flight priced exactly at ``offer.price_total`` (so the enriched
        offer's price belongs to its outbound+return pair). Best-effort: returns ``offer``
        unchanged when it already has an inbound leg, is one-way, has no token, the call fails,
        or that fare is no longer offered (all logged). Never mutates ``offer``.
        """
        token = offer.raw.get("departure_token")
        if offer.inbound is not None or not offer.return_date or not token:
            return offer
        try:
            options = self._return_options(offer, str(token))
        except ProviderError as exc:
            logger.warning("SerpApi return-flight lookup failed: %s", exc)
            return offer
        if not options:
            logger.info("SerpApi returned no return options for %s", offer.offer_key())
            return offer
        picked = pick_return_option(options, offer.price_total)
        if picked is None:
            logger.info(
                "SerpApi: listed fare %s %.0f for %s is no longer offered (return options now "
                "from %.0f); return flight left unknown",
                offer.currency,
                offer.price_total,
                offer.offer_key(),
                min(option.price for option in options),
            )
            return offer
        raw = dict(offer.raw)
        if picked.item.get("booking_token"):
            raw["booking_token"] = picked.item["booking_token"]
        return replace(offer, inbound=picked.leg, raw=raw)

    def get_price(self, offer: FlightOffer) -> float | None:
        """Current per-person price of ``offer``'s itinerary, ``None`` if no longer offered.

        Re-searches (1 credit) and matches on :func:`round_trip_match_key` (outbound leg +
        return date), because search results never carry an inbound leg. When ``offer.inbound``
        is known (filled by :meth:`get_flight_details`), the matched outbound's
        ``departure_token`` is followed (1 more credit) to price that exact return flight.
        Raises ``ProviderError`` subclasses like :meth:`search_flights`.
        """
        target = round_trip_match_key(offer)
        match = next(
            (
                candidate
                for candidate in self.search_flights(query_for_offer(offer))
                if round_trip_match_key(candidate) == target
            ),
            None,
        )
        if match is None or offer.inbound is None:
            return match.price_per_person if match is not None else None
        return self._exact_pair_price(match, offer.inbound)

    # ------------------------------------------------------------------ internals

    def _return_options(self, offer: FlightOffer, token: str) -> list[ReturnOption]:
        """Return flights for ``offer``'s outbound (one ``departure_token`` call, 1 credit)."""
        request = offer.raw.get("request") or build_params(
            query_for_offer(offer), deep_search=self._deep_search()
        )
        data = self._fetch({**request, "departure_token": token})
        return return_options(data) if data is not None else []

    def _exact_pair_price(self, outbound: FlightOffer, inbound: FlightLeg) -> float | None:
        """Per-person round-trip price of ``outbound`` + the return flight ``inbound``."""
        token = outbound.raw.get("departure_token")
        if not token:
            return None
        wanted = leg_key(inbound)
        for option in self._return_options(outbound, str(token)):
            if leg_key(option.leg) == wanted:
                return round(option.price / max(outbound.adults, 1), 2)
        return None

    def _api_key(self) -> str | None:
        value = self.ctx.get_secret(self.api_key_name or self.key)
        return value.strip() if value and value.strip() else None

    def _deep_search(self) -> bool:
        return bool(self.ctx.options.get("deep_search", False))

    def _fetch(self, params: Mapping[str, Any]) -> dict[str, Any] | None:
        """GET the search; returns the JSON body, or ``None`` when Google found no flights."""
        api_key = self._api_key()
        if api_key is None:
            raise ProviderNotConfigured("SerpApi API key is not set")
        install_httpx_log_redaction()
        started = time.monotonic()
        try:
            with httpx.Client(timeout=self.ctx.http_timeout) as client:
                response = client.get(SERPAPI_URL, params={**params, "api_key": api_key})
        except httpx.TimeoutException:
            raise ProviderError(
                f"SerpApi request timed out after {self.ctx.http_timeout:g} s"
            ) from None
        except httpx.HTTPError as exc:
            reason = redact(str(exc) or type(exc).__name__, api_key)
            raise ProviderError(f"SerpApi request failed: {reason}") from None
        try:
            data = response.json()
        except ValueError:
            raise ProviderError(
                f"SerpApi returned a non-JSON response (HTTP {response.status_code})"
            ) from None
        if not isinstance(data, dict):
            raise ProviderError(
                f"SerpApi returned unexpected JSON (HTTP {response.status_code})"
            )
        try:
            empty = check_response(response.status_code, data)
        except ProviderBlocked as exc:
            pause = self._pause_after_rate_limit(api_key)
            message = f"{redact(str(exc), api_key)}; {pause.describe()}"
            raise ProviderBlocked(message) from None
        except ProviderError as exc:
            raise type(exc)(redact(str(exc), api_key)) from None
        _set_pause(api_key, None)
        logger.debug(
            "SerpApi search %s done in %.1f s (HTTP %d)",
            (data.get("search_metadata") or {}).get("id"),
            time.monotonic() - started,
            response.status_code,
        )
        return None if empty else data

    def _pause_after_rate_limit(self, api_key: str) -> QuotaPause:
        """Record (and return) the quota pause for ``api_key`` after an HTTP 429."""
        pause = quota_pause(self._fetch_account(api_key), datetime.now(UTC))
        _set_pause(api_key, pause)
        logger.warning("%s", pause.describe())
        return pause

    def _fetch_account(self, api_key: str) -> Mapping[str, Any] | None:
        """The Account API response (free, not counted), ``None`` on any failure.

        The response contains the API key and account e-mail: it is never logged or kept;
        callers read only ``total_searches_left`` and ``plan_renewal_date``.
        """
        install_httpx_log_redaction()
        try:
            with httpx.Client(timeout=min(self.ctx.http_timeout, ACCOUNT_TIMEOUT_S)) as client:
                response = client.get(ACCOUNT_URL, params={"api_key": api_key})
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            reason = redact(str(exc) or type(exc).__name__, api_key)
            logger.warning("SerpApi account lookup failed: %s", reason)
            return None
        if response.status_code != 200 or not isinstance(data, dict):
            logger.warning("SerpApi account lookup failed (HTTP %d)", response.status_code)
            return None
        return data
