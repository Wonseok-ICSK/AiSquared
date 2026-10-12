"""Search orchestration (DESIGN §11): query expansion, provider fan-out and offer filtering.

* :func:`build_query_plan` expands a trip into concrete :class:`FlightQuery` objects
  (flexible dates x airports), exact dates first, capped per provider. Every combination
  that is dropped (past dates, return before departure, overlapping airports, the cap,
  nearby-airport limit) is summarized in the returned notes — nothing is capped silently.
* :func:`resolve_providers` instantiates the providers a trip uses and says why others
  are skipped.
* :func:`offer_matches_trip` applies the trip filters (stops, time windows, max price,
  currency) to one offer.
* :func:`search_trip` runs every provider concurrently on daemon threads (queries within
  one provider run sequentially) and never raises: failures end up in ``provider_stats``
  and ``log``. :func:`cancel_running_searches` stops searches in progress (app shutdown).
* :func:`trip_departed` says whether a trip has any searchable departure date left.
"""

from __future__ import annotations

import logging
import queue
import re
import threading
import time
import traceback
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from datetime import time as dtime
from typing import TYPE_CHECKING, Any

from sqlalchemy.orm import Session

from app import settings_store
from app.core.airports import airports_for, is_metro, metro_for_airport, nearby_airports
from app.providers.flights import get_provider_class, resolve_provider_keys
from app.providers.flights.base import (
    FlightLeg,
    FlightOffer,
    FlightProvider,
    FlightQuery,
    ProviderBlocked,
    ProviderContext,
    ProviderError,
    ProviderNotConfigured,
)
from app.settings_store import RuntimeSettings, load_runtime_settings
from app.timeutil import now_local, parse_hhmm

if TYPE_CHECKING:
    from app.models import Trip

logger = logging.getLogger(__name__)

MAX_PROVIDER_WORKERS = 3
"""Providers searched concurrently (queries within one provider are sequential)."""
MAX_NEARBY_EXTRA = 2
"""Nearby airports (outside the code's own metro) added per code when nearby search is on."""
QUERY_TIMEOUT_S = 180.0
"""Safety net: a provider gets at most this long per planned query before it is abandoned."""
SEARCH_TIMEOUT_BASE_S = 60.0
"""Fixed part of the search safety timeout (``+ QUERY_TIMEOUT_S`` per planned query)."""
CANCELLED_ERROR = "search cancelled (application shutting down)"
"""``SearchOutcome.error`` of a search stopped by :func:`cancel_running_searches`."""
NOT_CHECKED = "not checked"
"""Prefix of :func:`offer_matches_trip` reasons that are notes, not failures."""

_POLL_S = 0.25  # how often a waiting search checks whether it was cancelled
_TIER_OWN = 0  # the trip's own codes (or their metro airports)
_TIER_NEAR = 1  # same-metro siblings, alternative destinations
_TIER_EXTRA = 2  # other nearby airports
_MAX_LISTED = 6
_MAX_ERROR_CHARS = 300
_SECRET_PARAM_RE = re.compile(
    r"(?i)\b(api[_-]?key|apikey|key|token|access_token|refresh_token|secret|password|"
    r"client_secret|sig|signature|auth)=([^&\s'\"<>]+)"
)

ProviderSlot = tuple[str, FlightProvider | None, str | None]
"""``(provider key, instance or None, skip reason or None)``."""


# --------------------------------------------------------------------------- query plan


@dataclass(frozen=True)
class _Place:
    """A searchable origin/destination code with its priority tier (lower = searched first)."""

    code: str
    tier: int


@dataclass
class QueryPlan:
    """Queries for one provider (after the cap), the notes, and the uncapped total."""

    queries: list[FlightQuery]
    notes: list[str]
    total: int
    """Valid combinations before the cap (past / invalid ones excluded)."""


def build_query_plan(
    trip: Trip, provider_cls: type[FlightProvider], today: date, cap: int
) -> tuple[list[FlightQuery], list[str]]:
    """Expand ``trip`` into the queries ``provider_cls`` should run, plus notes.

    Dates: departure ``±flex_depart_days`` x return ``±flex_return_days`` (past departures
    and returns before departure dropped), ordered exact dates first, then by
    ``|Δdepart| + |Δreturn|``. Airports: the trip's codes, their metro siblings and nearby
    airports (``include_nearby_airports``) and ``alt_destinations``; providers with
    ``supports_metro_codes`` get metro codes as-is (one query) instead of one query per
    airport. At most ``min(cap, provider_cls.max_queries_per_run)`` queries are returned;
    everything dropped is summarized in the notes.
    """
    plan = plan_queries(trip, provider_cls, today, cap)
    return plan.queries, plan.notes


def plan_queries(
    trip: Trip, provider_cls: type[FlightProvider], today: date, cap: int
) -> QueryPlan:
    """Like :func:`build_query_plan` but also returns the uncapped combination count."""
    notes: list[str] = []
    metro_ok = bool(provider_cls.supports_metro_codes)
    origins = _origin_places(trip, metro_ok, notes)
    destinations = _destination_places(trip, metro_ok, notes)
    pairs = _airport_pairs(origins, destinations, notes)
    dates = _date_combinations(trip, today, notes)

    combos = [
        _make_query(trip, origin, destination, depart, ret)
        for depart, ret in dates
        for origin, destination in pairs
    ]
    limit = max(0, min(cap, provider_cls.max_queries_per_run))
    if len(combos) > limit:
        notes.append(_cap_note(combos, limit, cap, provider_cls))
    return QueryPlan(queries=combos[:limit], notes=notes, total=len(combos))


def _origin_places(trip: Trip, metro_ok: bool, notes: list[str]) -> list[_Place]:
    nearby = bool(trip.include_nearby_airports)
    return _dedupe_places(_places_for(trip.origin, nearby, metro_ok, notes))


def _destination_places(trip: Trip, metro_ok: bool, notes: list[str]) -> list[_Place]:
    places = _places_for(trip.destination, bool(trip.include_nearby_airports), metro_ok, notes)
    for alt in trip.alt_destinations or []:
        codes = [alt] if metro_ok else airports_for(alt)
        places.extend(_Place(code, _TIER_NEAR) for code in codes)
    return _dedupe_places(places)


def _places_for(code: str, include_nearby: bool, metro_ok: bool, notes: list[str]) -> list[_Place]:
    """Searchable codes for one trip code (see :func:`build_query_plan`)."""
    code = code.strip().upper()
    siblings, extras = _nearby_split(code, notes) if include_nearby else ([], [])
    if metro_ok:
        metro = metro_for_airport(code)
        primary = metro if siblings and metro else code
        if primary != code:
            notes.append(f"{code} searched as {primary} (all city airports) to include nearby ones")
        covered = set(airports_for(primary))
        return [_Place(primary, _TIER_OWN)] + [
            _Place(extra, _TIER_EXTRA) for extra in extras if extra not in covered
        ]
    own = airports_for(code)
    if is_metro(code):
        notes.append(f"{code} searched as {', '.join(own)} (provider needs airport codes)")
    return (
        [_Place(a, _TIER_OWN) for a in own]
        + [_Place(a, _TIER_NEAR) for a in siblings]
        + [_Place(a, _TIER_EXTRA) for a in extras]
    )


def _nearby_split(code: str, notes: list[str]) -> tuple[list[str], list[str]]:
    """``(same-metro siblings, nearest other airports)``; drops beyond the limit are noted."""
    own = set(airports_for(code))
    metro = None if is_metro(code) else metro_for_airport(code)
    siblings = (
        [a for a in airports_for(metro) if a not in own] if metro and is_metro(metro) else []
    )
    others = [a for a in nearby_airports(code) if a not in own and a not in siblings]
    kept, dropped = others[:MAX_NEARBY_EXTRA], others[MAX_NEARBY_EXTRA:]
    if dropped:
        notes.append(
            f"Nearby airports for {code} limited to the {MAX_NEARBY_EXTRA} nearest "
            f"({', '.join(kept)}); not searched: {_listing(dropped)}"
        )
    return siblings, kept


def _dedupe_places(places: Iterable[_Place]) -> list[_Place]:
    """Drop places whose airports are already covered by an earlier (higher-priority) place."""
    covered: set[str] = set()
    result: list[_Place] = []
    for place in places:
        airports = set(airports_for(place.code))
        if airports <= covered:
            continue
        covered |= airports
        result.append(place)
    return result


def _airport_pairs(
    origins: list[_Place], destinations: list[_Place], notes: list[str]
) -> list[tuple[_Place, _Place]]:
    pairs: list[tuple[_Place, _Place]] = []
    overlapping: list[str] = []
    for origin in origins:
        for destination in destinations:
            if set(airports_for(origin.code)) & set(airports_for(destination.code)):
                overlapping.append(f"{origin.code}→{destination.code}")
            else:
                pairs.append((origin, destination))
    if overlapping:
        notes.append(
            f"Skipped {len(overlapping)} route(s) whose origin and destination share an "
            f"airport: {_listing(overlapping)}"
        )
    # stable sort: the trip's own airports first, then siblings/alternatives, then nearby
    return sorted(pairs, key=lambda pair: pair[0].tier + pair[1].tier)


def _date_combinations(
    trip: Trip, today: date, notes: list[str]
) -> list[tuple[date, date | None]]:
    """``(depart, return)`` pairs, exact first then by ``|Δd| + |Δr|``; drops are noted."""
    flex_depart = abs(trip.flex_depart_days or 0)
    flex_return = abs(trip.flex_return_days or 0) if _is_round_trip(trip) else 0
    offsets = [
        (d, r)
        for d in range(-flex_depart, flex_depart + 1)
        for r in range(-flex_return, flex_return + 1)
    ]
    offsets.sort(key=lambda o: (abs(o[0]) + abs(o[1]), abs(o[0]), o[0], o[1]))

    combos: list[tuple[date, date | None]] = []
    past: list[date] = []
    reversed_count = 0
    for d, r in offsets:
        depart = trip.depart_date + timedelta(days=d)
        ret = trip.return_date + timedelta(days=r) if _is_round_trip(trip) else None
        if depart < today:
            past.append(depart)
        elif ret is not None and ret < depart:
            reversed_count += 1
        else:
            combos.append((depart, ret))
    if past:
        notes.append(
            f"Skipped {len(past)} date combination(s) departing in the past "
            f"({_date_span(sorted(set(past)))})"
        )
    if reversed_count:
        notes.append(f"Skipped {reversed_count} date combination(s) returning before departure")
    return combos


def _is_round_trip(trip: Trip) -> bool:
    return trip.return_date is not None and trip.trip_type != "one_way"


def last_departure_date(trip: Trip) -> date:
    """Latest departure date a search of ``trip`` covers (``depart_date + flex_depart_days``)."""
    return trip.depart_date + timedelta(days=abs(trip.flex_depart_days or 0))


def trip_departed(trip: Trip, today: date) -> bool:
    """True when ``trip`` has no searchable departure date left (all are before ``today``)."""
    return last_departure_date(trip) < today


def _make_query(
    trip: Trip, origin: _Place, destination: _Place, depart: date, ret: date | None
) -> FlightQuery:
    direct_only = bool(trip.direct_only)
    return FlightQuery(
        origin=origin.code,
        destination=destination.code,
        depart_date=depart,
        return_date=ret,
        adults=trip.adults or 1,
        cabin=trip.cabin or "economy",  # type: ignore[arg-type]
        direct_only=direct_only,
        max_stops=0 if direct_only else trip.max_stops,
        currency=trip.currency or "KRW",
    )


def _cap_note(
    combos: list[FlightQuery], limit: int, cap: int, provider_cls: type[FlightProvider]
) -> str:
    provider_limit = provider_cls.max_queries_per_run
    if provider_limit < cap:
        reason = f"provider limit max_queries_per_run={provider_limit}"
    elif provider_limit == cap:
        reason = f"limit {cap}: max_search_combinations setting / provider max_queries_per_run"
    else:
        reason = f"setting max_search_combinations={cap}"
    dropped = combos[limit:]
    return (
        f"Searching {limit} of {len(combos)} combinations ({reason}); "
        f"not searched: {len(dropped)} ({describe_queries(dropped)})"
    )


def describe_queries(queries: Sequence[FlightQuery]) -> str:
    """Compact summary of a set of queries: date spans and routes."""
    departs = sorted({q.depart_date for q in queries})
    returns = sorted({q.return_date for q in queries if q.return_date is not None})
    routes = list(dict.fromkeys(f"{q.origin}→{q.destination}" for q in queries))
    parts = [f"departures {_date_span(departs)}"]
    if returns:
        parts.append(f"returns {_date_span(returns)}")
    parts.append(f"routes {_listing(routes)}")
    return "; ".join(parts)


def describe_query(query: FlightQuery) -> str:
    """``"ICN→NRT 2026-11-20/2026-11-24"`` (one-way: no return part)."""
    dates = query.depart_date.isoformat()
    if query.return_date is not None:
        dates += f"/{query.return_date.isoformat()}"
    return f"{query.origin}→{query.destination} {dates}"


def _date_span(days: Sequence[date]) -> str:
    if not days:
        return "-"
    if len(days) == 1:
        return days[0].isoformat()
    return f"{days[0].isoformat()}..{days[-1].isoformat()} ({len(days)} dates)"


def _listing(items: Sequence[str]) -> str:
    shown = ", ".join(items[:_MAX_LISTED])
    return shown if len(items) <= _MAX_LISTED else f"{shown} (+{len(items) - _MAX_LISTED} more)"


# --------------------------------------------------------------------------- providers


def resolve_providers(
    db: Session, trip: Trip, runtime: RuntimeSettings | None = None
) -> list[ProviderSlot]:
    """Providers for ``trip`` as ``(key, provider | None, skip_reason | None)``.

    Keys come from the trip's ``providers`` list, else the runtime ``default_providers``,
    else every provider with ``enabled_by_default`` (``registry.resolve_provider_keys``).
    Unknown keys, providers that fail to initialise and providers whose
    ``status().configured`` is False are returned with ``None`` and a reason.
    """
    runtime = runtime or load_runtime_settings(db)
    trip_keys = list(trip.providers or [])
    keys = resolve_provider_keys(trip_keys, runtime.default_providers)
    requested = trip_keys or list(runtime.default_providers)
    unknown = [key for key in dict.fromkeys(requested) if key not in keys]
    slots = [_provider_slot(key, runtime) for key in keys]
    slots.extend((key, None, "unknown provider (not installed)") for key in unknown)
    return slots


def _provider_slot(key: str, runtime: RuntimeSettings) -> ProviderSlot:
    cls = get_provider_class(key)
    ctx = ProviderContext(
        get_secret=settings_store.get_secret,
        browser=_browser_manager() if cls.uses_browser else None,
        options=dict(runtime.provider_options.get(key) or {}),
    )
    try:
        provider = cls(ctx)
        status = provider.status()
    except Exception as exc:  # noqa: BLE001 - a broken provider must not break the search
        message = safe_error(exc)
        logger.warning("Flight provider %s could not be initialised: %s", key, message)
        return key, None, f"could not be initialised: {message}"
    if not status.configured:
        return key, None, status.message or "not configured"
    return key, provider, None


def _browser_manager() -> Any | None:
    """The shared BrowserManager, or ``None`` when browser automation is unavailable."""
    try:
        from app.browser.manager import get_browser_manager
    except ImportError as exc:
        logger.warning("Browser automation is not available (%s)", exc)
        return None
    try:
        return get_browser_manager()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Browser manager could not be started: %s", safe_error(exc))
        return None


def safe_error(exc: BaseException, limit: int = _MAX_ERROR_CHARS) -> str:
    """Exception text safe for logs and the UI: secret-looking URL params redacted, truncated."""
    text = str(exc).strip() or type(exc).__name__
    text = _SECRET_PARAM_RE.sub(lambda m: f"{m.group(1)}=***", text)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


# --------------------------------------------------------------------------- filters


def offer_matches_trip(offer: FlightOffer, trip: Trip) -> tuple[bool, list[str]]:
    """Whether ``offer`` satisfies the trip filters, with reasons.

    Checks currency (a mismatch never matches), ``direct_only`` / ``max_stops`` on the known
    legs, the outbound/return time windows (``HH:MM``; a window ``from > to`` wraps past
    midnight) and ``max_price`` in ``trip.price_basis``. An unknown inbound leg never fails
    a filter; it is reported as a ``"not checked: ..."`` note. Failure reasons are
    ``"<filter>: <detail>"``.
    """
    failures: list[str] = []
    notes: list[str] = []
    currency_ok = offer.currency == trip.currency
    if not currency_ok:
        failures.append(f"currency: offer in {offer.currency}, trip in {trip.currency}")
    inbound_unknown = offer.inbound is None and (bool(offer.return_date) or _is_round_trip(trip))
    _check_stops(offer, trip, inbound_unknown, failures, notes)
    _check_windows(offer, trip, failures, notes)
    if currency_ok:
        _check_price(offer, trip, failures)
    return not failures, failures + list(dict.fromkeys(notes))


def _check_stops(
    offer: FlightOffer, trip: Trip, inbound_unknown: bool, failures: list[str], notes: list[str]
) -> None:
    limit = 0 if trip.direct_only else trip.max_stops
    if limit is None:
        return
    rule = "direct flights only" if trip.direct_only else f"max {limit} stop(s)"
    for label, leg in (("outbound", offer.outbound), ("return", offer.inbound)):
        if leg is not None and leg.stops > limit:
            failures.append(f"stops: {label} has {leg.stops} stop(s), {rule}")
    if inbound_unknown:
        notes.append(f"{NOT_CHECKED}: return flight stops (return flight details unknown)")


@dataclass(frozen=True)
class _Window:
    label: str
    leg: str  # "outbound" | "inbound" (FlightOffer attribute)
    time: str  # "departure_time" | "arrival_time" (FlightLeg attribute)
    start: str  # Trip attribute names of the window bounds
    end: str


_WINDOWS: tuple[_Window, ...] = (
    _Window("outbound departure time", "outbound", "departure_time",
            "outbound_depart_from", "outbound_depart_to"),
    _Window("outbound arrival time", "outbound", "arrival_time",
            "outbound_arrive_from", "outbound_arrive_to"),
    _Window("return departure time", "inbound", "departure_time",
            "return_depart_from", "return_depart_to"),
    _Window("return arrival time", "inbound", "arrival_time",
            "return_arrive_from", "return_arrive_to"),
)


def _check_windows(offer: FlightOffer, trip: Trip, failures: list[str], notes: list[str]) -> None:
    for window in _WINDOWS:
        start, end = getattr(trip, window.start), getattr(trip, window.end)
        if not start and not end:
            continue
        leg: FlightLeg | None = getattr(offer, window.leg)
        if leg is None:
            if window.leg == "inbound" and (offer.return_date or _is_round_trip(trip)):
                notes.append(f"{NOT_CHECKED}: {window.label} (return flight details unknown)")
            continue
        clock = _clock(getattr(leg, window.time))
        if clock is None:
            notes.append(f"{NOT_CHECKED}: {window.label} (time unknown)")
            continue
        try:
            inside = _in_window(clock, start, end)
        except ValueError:
            notes.append(f"{NOT_CHECKED}: {window.label} (invalid window {start}–{end})")
            continue
        if not inside:
            failures.append(
                f"{window.label}: {clock.strftime('%H:%M')} outside "
                f"{start or '00:00'}–{end or '23:59'}"
            )


def _clock(value: str | None) -> dtime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.strip().replace(" ", "T")).time()
    except ValueError:
        return None


def _in_window(clock: dtime, start: str | None, end: str | None) -> bool:
    lo = parse_hhmm(start) if start else dtime.min
    hi = parse_hhmm(end) if end else dtime(23, 59, 59)
    if lo <= hi:
        return lo <= clock <= hi
    return clock >= lo or clock <= hi  # window wraps past midnight


def _check_price(offer: FlightOffer, trip: Trip, failures: list[str]) -> None:
    if trip.max_price is None:
        return
    basis = trip.price_basis or "per_person"
    price = offer.price_total if basis == "total" else offer.price_per_person
    if price > trip.max_price:
        failures.append(
            f"max price: {format_money(price, offer.currency)} {basis.replace('_', ' ')} "
            f"> {format_money(trip.max_price, offer.currency)}"
        )


def format_money(value: float, currency: str) -> str:
    """``"278,000 KRW"`` / ``"412.50 USD"`` (no decimals for whole amounts)."""
    text = f"{value:,.0f}" if float(value).is_integer() else f"{value:,.2f}"
    return f"{text} {currency}"


def summarize_mismatches(reason_lists: Iterable[list[str]]) -> str | None:
    """``"8 above max price, 3 stops"``-style tally of failing filters (notes ignored)."""
    counts: Counter[str] = Counter()
    for reasons in reason_lists:
        labels = {r.split(":", 1)[0] for r in reasons if not r.startswith(NOT_CHECKED)}
        counts.update(labels)
    if not counts:
        return None
    return ", ".join(f"{label} {n}" for label, n in counts.most_common())


# --------------------------------------------------------------------------- search


@dataclass
class SearchOutcome:
    """Everything one trip search produced. ``search_trip`` never raises."""

    results: list[tuple[FlightOffer, FlightQuery]] = field(default_factory=list)
    provider_stats: dict[str, dict[str, Any]] = field(default_factory=dict)
    log: list[str] = field(default_factory=list)
    combos_total: int = 0
    """Valid (provider, query) combinations before caps, summed over the providers that ran."""
    combos_searched: int = 0
    """Queries that completed successfully, summed over providers."""
    status: str = "error"
    """``"ok"`` (every query succeeded) | ``"partial"`` (some failed) | ``"error"`` (none
    ran or none succeeded, or the search was cancelled)."""
    error: str | None = None
    providers: dict[str, FlightProvider] = field(default_factory=dict)
    """Provider instances that ran (e.g. for ``get_flight_details`` follow-ups)."""
    blocked: dict[str, str] = field(default_factory=dict)
    """``provider key -> reason`` for providers that raised ``ProviderBlocked`` (anti-bot)."""
    cancelled: bool = False
    """True when :func:`cancel_running_searches` stopped this search (results incomplete)."""


@dataclass
class _ProviderJob:
    key: str
    provider: FlightProvider
    queries: list[FlightQuery]


@dataclass
class _ProviderResult:
    key: str
    results: list[tuple[FlightOffer, FlightQuery]] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    log: list[str] = field(default_factory=list)
    succeeded: int = 0
    failed: int = 0
    blocked: str | None = None


@dataclass(eq=False)
class _SearchControl:
    """Stop signal shared by one search and its provider threads."""

    stop: threading.Event = field(default_factory=threading.Event)
    """Set => providers issue no further queries (search cancelled or timed out)."""
    cancelled: bool = False

    def cancel(self) -> None:
        self.cancelled = True
        self.stop.set()


_active_lock = threading.Lock()
_active_searches: set[_SearchControl] = set()


def cancel_running_searches() -> int:
    """Stop every search in progress; returns how many were signalled.

    Their providers skip all remaining queries and :func:`search_trip` returns at once with
    ``cancelled=True`` (a query already in flight finishes on its daemon thread and is
    discarded). Searches started afterwards are not affected. Used at application shutdown.
    """
    with _active_lock:
        controls = list(_active_searches)
    for control in controls:
        control.cancel()
    return len(controls)


def search_trip(db: Session, trip: Trip, today: date | None = None) -> SearchOutcome:
    """Search every provider of ``trip`` and collect ``(offer, query)`` results.

    Providers run concurrently on daemon threads (at most :data:`MAX_PROVIDER_WORKERS`), so
    a slow or hung provider never delays process exit; queries within one provider run
    sequentially. Per-provider errors are captured (``ProviderBlocked`` and
    ``ProviderNotConfigured`` stop that provider's remaining queries; blocks are listed in
    ``outcome.blocked``). A provider still busy after the safety timeout is abandoned and
    issues no further queries. ``db`` is only used up front (settings, provider
    resolution); never raises.
    """
    outcome = SearchOutcome()
    control = _SearchControl()
    with _active_lock:
        _active_searches.add(control)
    try:
        _search(db, trip, today or now_local().date(), outcome, control)
    except Exception as exc:  # noqa: BLE001 - the contract is "never raise"
        message = safe_error(exc)
        logger.error("Search for trip %s failed: %s\n%s", trip.id, message, _frames(exc))
        outcome.status, outcome.error = "error", f"search failed: {message}"
        outcome.log.append(f"Search failed unexpectedly: {message}")
    finally:
        with _active_lock:
            _active_searches.discard(control)
    if control.cancelled:
        outcome.cancelled = True
        outcome.status, outcome.error = "error", CANCELLED_ERROR
        outcome.log.append(f"Search stopped before it finished: {CANCELLED_ERROR}")
    return outcome


def _search(
    db: Session, trip: Trip, today: date, outcome: SearchOutcome, control: _SearchControl
) -> None:
    runtime = load_runtime_settings(db)
    jobs: list[_ProviderJob] = []
    skipped: list[str] = []
    nothing_to_search = False
    for key, provider, reason in resolve_providers(db, trip, runtime):
        if provider is None:
            _skip(outcome, key, reason or "not available")
            skipped.append(f"{key}: {reason}")
            continue
        plan = plan_queries(trip, type(provider), today, runtime.max_search_combinations)
        outcome.log.extend(f"[{key}] {note}" for note in plan.notes)
        if not plan.queries:
            _skip(outcome, key, "no date/airport combination left to search")
            nothing_to_search = True
            continue
        outcome.combos_total += plan.total
        outcome.providers[key] = provider
        jobs.append(_ProviderJob(key, provider, plan.queries))

    if not jobs:
        outcome.status = "error"
        if nothing_to_search:
            outcome.error = "No date/airport combination left to search (see log)"
        elif skipped:
            outcome.error = "No usable flight provider: " + "; ".join(skipped)
        else:
            outcome.error = "No flight provider selected"
        return
    if control.cancelled:
        return
    results = _run_jobs(jobs, control)
    for result in results:
        outcome.results.extend(result.results)
        outcome.provider_stats[result.key] = result.stats
        outcome.log.extend(result.log)
        outcome.combos_searched += result.succeeded
        if result.blocked:
            outcome.blocked[result.key] = result.blocked
    _set_status(outcome, results)


def _skip(outcome: SearchOutcome, key: str, reason: str) -> None:
    outcome.provider_stats[key] = _stats("skipped", error=reason)
    outcome.log.append(f"[{key}] skipped: {reason}")


def _set_status(outcome: SearchOutcome, results: list[_ProviderResult]) -> None:
    failed = [r for r in results if r.failed]
    errors = "; ".join(f"{r.key}: {r.stats.get('error')}" for r in failed)
    if not any(r.succeeded for r in results):
        outcome.status, outcome.error = "error", f"All provider searches failed: {errors}"
    elif failed:
        outcome.status, outcome.error = "partial", f"Some searches failed: {errors}"
    else:
        outcome.status, outcome.error = "ok", None


def _run_jobs(jobs: list[_ProviderJob], control: _SearchControl) -> list[_ProviderResult]:
    """Run provider jobs on daemon worker threads and wait for them.

    Waiting ends when every provider is done, the safety timeout passes or the search is
    cancelled. ``control.stop`` is then set, so abandoned providers issue no further
    queries; their results are replaced by a timeout/cancel error.
    """
    pending: queue.SimpleQueue[tuple[int, _ProviderJob]] = queue.SimpleQueue()
    for item in enumerate(jobs):
        pending.put(item)
    done: queue.SimpleQueue[tuple[int, _ProviderResult]] = queue.SimpleQueue()
    for number in range(min(MAX_PROVIDER_WORKERS, len(jobs))):
        threading.Thread(
            target=_provider_worker,
            args=(pending, done, control.stop),
            name=f"flight-search-{number}",
            daemon=True,  # never delays interpreter exit (unlike concurrent.futures workers)
        ).start()
    timeout = SEARCH_TIMEOUT_BASE_S + QUERY_TIMEOUT_S * sum(len(job.queries) for job in jobs)
    finished = _await_results(done, len(jobs), control, timeout)
    control.stop.set()  # providers still running (timeout/cancel) stop before their next query
    return [
        finished[index] if index in finished else _unfinished(job, control, timeout)
        for index, job in enumerate(jobs)
    ]


def _provider_worker(
    pending: queue.SimpleQueue[tuple[int, _ProviderJob]],
    done: queue.SimpleQueue[tuple[int, _ProviderResult]],
    stop: threading.Event,
) -> None:
    """Daemon worker: run queued provider jobs until none is left."""
    while True:
        try:
            index, job = pending.get_nowait()
        except queue.Empty:
            return
        try:
            result = _run_provider(job, stop)
        except Exception as exc:  # noqa: BLE001 - _run_provider should not raise; belt and braces
            message = safe_error(exc)
            result = _ProviderResult(
                key=job.key,
                stats=_stats("error", error=message),
                log=[f"[{job.key}] {message}"],
                failed=1,
            )
        done.put((index, result))


def _await_results(
    done: queue.SimpleQueue[tuple[int, _ProviderResult]],
    count: int,
    control: _SearchControl,
    timeout: float,
) -> dict[int, _ProviderResult]:
    """Results by job index, collected until all arrived, the timeout or cancellation."""
    deadline = time.monotonic() + timeout
    finished: dict[int, _ProviderResult] = {}
    while len(finished) < count and not control.cancelled:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            index, result = done.get(timeout=min(remaining, _POLL_S))
        except queue.Empty:
            continue
        finished[index] = result
    return finished


def _unfinished(job: _ProviderJob, control: _SearchControl, timeout: float) -> _ProviderResult:
    """Result for a provider that was still running when the search stopped waiting."""
    if control.cancelled:
        message = "cancelled"
        line = f"[{job.key}] cancelled before it finished; results discarded"
    else:
        message = f"timed out after {timeout:.0f} s"
        line = f"[{job.key}] {message}; results discarded, remaining queries skipped"
        logger.error("Flight provider %s %s; abandoned", job.key, message)
    return _ProviderResult(
        key=job.key,
        stats=_stats("error", error=message, queries_planned=len(job.queries)),
        log=[line],
        failed=1,
    )


def _run_provider(job: _ProviderJob, stop: threading.Event) -> _ProviderResult:
    """Run one provider's queries sequentially, capturing every failure.

    Checks ``stop`` before each query: once it is set, the remaining queries are skipped.
    """
    result = _ProviderResult(key=job.key)
    errors: list[str] = []
    started = time.monotonic()
    executed = 0
    for index, query in enumerate(job.queries):
        if stop.is_set():
            errors.append("stopped")
            result.log.append(
                f"[{job.key}] stopped before {describe_query(query)}: "
                f"{_queries(len(job.queries) - index)} skipped"
            )
            break
        executed += 1
        try:
            offers = job.provider.search_flights(query)
        except (ProviderBlocked, ProviderNotConfigured) as exc:
            remaining = len(job.queries) - index - 1
            blocked = isinstance(exc, ProviderBlocked)
            message = f"{'blocked' if blocked else 'not configured'}: {safe_error(exc)}"
            if blocked:
                result.blocked = safe_error(exc)
            errors.append(message)
            result.log.append(
                f"[{job.key}] {describe_query(query)}: {message}; "
                f"{_queries(remaining, 'remaining ')} skipped"
            )
            logger.warning("Flight provider %s %s; remaining queries skipped", job.key, message)
            break
        except ProviderError as exc:
            message = safe_error(exc)
            errors.append(message)
            result.log.append(f"[{job.key}] {describe_query(query)}: error: {message}")
            logger.warning(
                "Flight provider %s failed for %s: %s", job.key, describe_query(query), message
            )
            continue
        except Exception as exc:  # noqa: BLE001 - provider bug; keep going with other queries
            message = f"unexpected {type(exc).__name__}: {safe_error(exc)}"
            errors.append(message)
            result.log.append(f"[{job.key}] {describe_query(query)}: {message}")
            logger.error("Flight provider %s crashed: %s\n%s", job.key, message, _frames(exc))
            continue
        result.succeeded += 1
        result.results.extend((offer, query) for offer in offers or [])

    result.results, duplicates = _dedupe_offers(result.results)
    result.failed = len(errors)
    duration = time.monotonic() - started
    result.stats = _stats(
        "error" if errors else "ok",
        offers=len(result.results),
        error=_join_errors(errors),
        duration_s=round(duration, 2),
        queries=executed,
        queries_planned=len(job.queries),
        queries_failed=len(errors),
    )
    summary = (
        f"[{job.key}] {result.succeeded}/{len(job.queries)} queries succeeded, "
        f"{len(result.results)} offers in {duration:.1f} s"
    )
    if duplicates:
        summary += f" ({duplicates} duplicate offers merged, cheapest kept)"
    result.log.append(summary)
    return result


def _queries(count: int, adjective: str = "") -> str:
    """``"1 remaining query"`` / ``"3 remaining queries"``."""
    return f"{count} {adjective}quer{'y' if count == 1 else 'ies'}"


def _dedupe_offers(
    results: list[tuple[FlightOffer, FlightQuery]],
) -> tuple[list[tuple[FlightOffer, FlightQuery]], int]:
    """One entry per ``offer_key`` (cheapest per person wins, first-seen order kept)."""
    index_by_key: dict[str, int] = {}
    unique: list[tuple[FlightOffer, FlightQuery]] = []
    for offer, query in results:
        key = offer.offer_key()
        position = index_by_key.get(key)
        if position is None:
            index_by_key[key] = len(unique)
            unique.append((offer, query))
        elif offer.price_per_person < unique[position][0].price_per_person:
            unique[position] = (offer, query)
    return unique, len(results) - len(unique)


def _stats(status: str, **values: Any) -> dict[str, Any]:
    stats: dict[str, Any] = {
        "status": status, "offers": 0, "error": None, "duration_s": 0.0, "queries": 0
    }
    stats.update(values)
    return stats


def _join_errors(errors: list[str]) -> str | None:
    if not errors:
        return None
    distinct = list(dict.fromkeys(errors))
    text = "; ".join(distinct[:3])
    if len(errors) > 1:
        text = f"{len(errors)} queries failed: {text}"
    return text if len(distinct) <= 3 else f"{text} (+{len(distinct) - 3} more)"


def _frames(exc: BaseException) -> str:
    """Traceback frames only (no exception message, which may carry URLs/secrets)."""
    return "".join(traceback.format_tb(exc.__traceback__)).rstrip()
