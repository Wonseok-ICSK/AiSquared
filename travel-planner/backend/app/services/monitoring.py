"""Monitoring runs (DESIGN §11): search a trip, persist the observed prices, fire alerts.

Flow of one check: per-trip guard (at most one check per trip in this process) ->
:func:`create_run` (``SearchRun`` with status ``running``) -> :func:`execute_run`:
``search.search_trip()`` -> persist ``FlightOffer`` rows (+ ``previous_price`` /
``lowest_price``) -> best matching offer -> finish the run (``ok`` / ``partial`` /
``error``) and update ``trip.last_*`` -> ``alerts.evaluate_alerts()`` -> (if alerts fired)
best-effort ``get_flight_details`` for the best offer -> the notification dispatcher.
A provider that blocked the search (captcha / anti-bot page) is reported to the user once
per provider per day as a ``system`` notification.

Entry points: :func:`run_trip_check` (synchronous; scheduler jobs) and
:func:`start_trip_check` (creates the run row, executes it in a daemon thread; API).
Scheduled checks of a trip without any searchable departure date left deactivate the trip
instead of running. :func:`recover_stale_runs` marks runs left ``running`` by a previous
process as errors; :func:`cancel_running_checks` stops checks in progress (shutdown).
Each phase uses its own DB session; nothing here raises into the scheduler.
"""

from __future__ import annotations

import importlib
import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from datetime import time as dtime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import session_scope
from app.models import FlightOffer as OfferRow
from app.models import Notification, SearchRun, Trip
from app.providers.flights.base import FlightOffer, FlightProvider, FlightQuery
from app.services.alerts import FINISHED_RUN_STATUSES, evaluate_alerts
from app.services.events import AlertEvent, NotificationMessage
from app.services.search import (
    SearchOutcome,
    cancel_running_searches,
    format_money,
    last_departure_date,
    offer_matches_trip,
    safe_error,
    search_trip,
    summarize_mismatches,
    trip_departed,
)
from app.settings_store import load_runtime_settings
from app.timeutil import now_local, utcnow

logger = logging.getLogger(__name__)

ENRICH_TIMEOUT_S = 90.0
"""Time budget for ``get_flight_details`` on the best offer before notifying."""
CLAIM_WAIT_S = 10.0
"""How long :func:`start_trip_check` waits for a concurrently starting run to get its id."""
RAW_MAX_CHARS = 20_000
"""Serialized size limit of ``FlightOffer.raw`` (larger payloads are trimmed)."""
_RAW_VALUE_MAX_CHARS = 2_000
_IN_CHUNK = 500
INTERRUPTED = "interrupted"
BLOCK_EVENT = "provider_blocked"
"""``Notification.data["event"]`` of the "provider blocked us" system notice."""

# String column sizes of ``FlightOffer`` (enforced by PostgreSQL, not SQLite).
_LIMITS = {
    "seller": 200,
    "offer_key": 512,
    "airline": 300,
    "flight_number": 200,
    "fare_type": 100,
    "baggage": 200,
}


class TripNotFoundError(LookupError):
    """The trip does not exist."""


class RunInProgressError(RuntimeError):
    """A check for the trip is starting right now and its run id is not known yet."""


# --------------------------------------------------------------------------- per-trip guard


class _RunGuard:
    """Per-trip locks: at most one check per trip runs in this process.

    A trip is *claimed* before its run row is created and released when the run is done;
    ``attach`` records the run id so concurrent callers can be pointed at it.
    """

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._claimed: set[int] = set()
        self._runs: dict[int, int] = {}

    def try_claim(self, trip_id: int) -> bool:
        """Claim ``trip_id`` without waiting; False if a check is already in progress."""
        with self._cond:
            if trip_id in self._claimed:
                return False
            self._claimed.add(trip_id)
            return True

    def claim_or_current(self, trip_id: int, timeout: float) -> tuple[bool, int | None]:
        """``(True, None)`` if claimed now, else ``(False, run id of the check in progress)``.

        Waits up to ``timeout`` seconds for an in-progress check to publish its run id.
        """
        deadline = time.monotonic() + timeout
        with self._cond:
            while trip_id in self._claimed and trip_id not in self._runs:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._cond.wait(remaining)
            if trip_id not in self._claimed:
                self._claimed.add(trip_id)
                return True, None
            return False, self._runs.get(trip_id)

    def attach(self, trip_id: int, run_id: int) -> None:
        """Record the run executing for a claimed trip."""
        with self._cond:
            self._runs[trip_id] = run_id
            self._cond.notify_all()

    def release(self, trip_id: int) -> None:
        """Release the claim (idempotent)."""
        with self._cond:
            self._claimed.discard(trip_id)
            self._runs.pop(trip_id, None)
            self._cond.notify_all()

    def claimed_trips(self) -> set[int]:
        """Trips with a check in progress."""
        with self._cond:
            return set(self._claimed)

    def current_run(self, trip_id: int) -> int | None:
        """Run id of the check in progress for ``trip_id``, if known."""
        with self._cond:
            return self._runs.get(trip_id)


_guard = _RunGuard()


def is_trip_running(trip_id: int) -> bool:
    """True while a check for ``trip_id`` is in progress in this process."""
    return trip_id in _guard.claimed_trips()


# --------------------------------------------------------------------------- entry points


def create_run(db: Session, trip: Trip, trigger: str) -> SearchRun:
    """Insert a ``running`` :class:`SearchRun` for ``trip`` and commit it."""
    run = SearchRun(
        trip_id=trip.id,
        trigger=trigger,
        status="running",
        started_at=utcnow(),
        currency=trip.currency,
    )
    db.add(run)
    db.commit()
    logger.info("Run %s started for trip %s (%s)", run.id, trip.id, trigger)
    return run


def run_trip_check(trip_id: int, trigger: str = "schedule") -> int | None:
    """Check one trip synchronously; returns the run id, or ``None`` when skipped.

    Skipped when a check for the trip is already running, the trip does not exist, or
    (scheduled checks only) the trip is inactive. A scheduled check of a trip with no
    searchable departure date left (``search.trip_departed``) deactivates the trip and
    removes its scheduler jobs instead of creating a run. Never raises.
    """
    if not _guard.try_claim(trip_id):
        logger.info("Trip %s: a check is already running; %s check skipped", trip_id, trigger)
        return None
    try:
        with session_scope() as db:
            trip = db.get(Trip, trip_id)
            if trip is None:
                logger.warning("Trip %s not found; %s check skipped", trip_id, trigger)
                return None
            if trigger == "schedule" and not trip.active:
                logger.info("Trip %s is inactive; scheduled check skipped", trip_id)
                return None
            if trigger == "schedule" and trip_departed(trip, now_local().date()):
                _retire_departed_trip(db, trip)
                return None
            run_id = create_run(db, trip, trigger).id
        _guard.attach(trip_id, run_id)
        execute_run(run_id)
        return run_id
    except Exception as exc:  # noqa: BLE001 - scheduler jobs must not die
        logger.error("Check of trip %s failed to start: %s", trip_id, safe_error(exc))
        return None
    finally:
        _guard.release(trip_id)


def start_trip_check(trip_id: int, trigger: str = "manual") -> int:
    """Create a run for ``trip_id`` and execute it in a daemon thread; returns the run id.

    If a check for the trip is already in progress, returns that run's id instead.
    Raises :class:`TripNotFoundError` for an unknown trip and :class:`RunInProgressError`
    if another check is starting but has no run id after :data:`CLAIM_WAIT_S` seconds.
    """
    claimed, current = _guard.claim_or_current(trip_id, CLAIM_WAIT_S)
    if not claimed:
        if current is None:
            raise RunInProgressError(f"a check for trip {trip_id} is starting; try again")
        logger.info("Trip %s: check already running as run %s", trip_id, current)
        return current
    try:
        with session_scope() as db:
            trip = db.get(Trip, trip_id)
            if trip is None:
                raise TripNotFoundError(f"trip {trip_id} not found")
            run_id = create_run(db, trip, trigger).id
        _guard.attach(trip_id, run_id)
        worker = threading.Thread(
            target=_execute_and_release,
            args=(trip_id, run_id),
            name=f"trip-check-{trip_id}",
            daemon=True,
        )
        worker.start()
    except BaseException:
        _guard.release(trip_id)
        raise
    return run_id


def _execute_and_release(trip_id: int, run_id: int) -> None:
    try:
        execute_run(run_id)
    finally:
        _guard.release(trip_id)


def _retire_departed_trip(db: Session, trip: Trip) -> None:
    """Deactivate a trip whose departure dates have all passed and drop its jobs."""
    trip.active = False
    db.commit()
    logger.info(
        "Trip %s: last departure date %s has passed; monitoring deactivated",
        trip.id, last_departure_date(trip),
    )
    try:
        from app.services.scheduler import unschedule_trip  # lazy: scheduler imports us
    except ImportError as exc:  # pragma: no cover - scheduler is part of this module
        logger.warning("Scheduler unavailable (%s); jobs of trip %s kept", exc, trip.id)
        return
    try:
        unschedule_trip(trip.id)
    except Exception as exc:  # noqa: BLE001 - the jobs are dropped by housekeeping anyway
        logger.warning("Could not remove the jobs of trip %s: %s", trip.id, safe_error(exc))


def cancel_running_checks() -> int:
    """Stop the checks in progress (application shutdown); returns how many were signalled.

    Their searches skip all remaining queries and the runs end as ``error: interrupted``
    without touching ``trip.last_checked_at`` (so the scheduler catches up on next start).
    """
    count = cancel_running_searches()
    if count:
        logger.info("Asked %d running check(s) to stop", count)
    return count


def recover_stale_runs() -> int:
    """Mark ``running`` runs that no check in this process owns as ``error: interrupted``.

    Called at startup (runs left over by a previous process) and by daily housekeeping.
    Returns the number of runs fixed.
    """
    busy = _guard.claimed_trips()
    with session_scope() as db:
        stale = [
            run
            for run in db.scalars(select(SearchRun).where(SearchRun.status == "running"))
            if run.trip_id not in busy
        ]
        for run in stale:
            _mark_interrupted(run, "the application stopped before it finished")
    if stale:
        logger.warning("Marked %d interrupted run(s) as error", len(stale))
    return len(stale)


# --------------------------------------------------------------------------- execution


@dataclass
class _Persisted:
    """Offer rows of the run paired with the provider offers they came from."""

    pairs: list[tuple[OfferRow, FlightOffer]]
    best: tuple[OfferRow, FlightOffer] | None


def execute_run(run_id: int) -> None:
    """Execute a ``running`` run end to end (own DB sessions). Never raises.

    A search stopped by :func:`cancel_running_checks` ends the run as
    ``error: interrupted`` (its partial results are discarded).
    """
    try:
        outcome = _search_phase(run_id)
        if outcome is None:
            return
        if outcome.cancelled:
            _interrupt_run(run_id, "the application is shutting down")
            return
        _finish_phase(run_id, outcome)
    except Exception as exc:  # noqa: BLE001 - mark the run failed instead of crashing
        message = safe_error(exc)
        logger.error("Run %s failed: %s", run_id, message)
        _fail_run(run_id, f"internal error: {message}")


def _search_phase(run_id: int) -> SearchOutcome | None:
    with session_scope() as db:
        run = db.get(SearchRun, run_id)
        if run is None or run.status != "running":
            logger.warning("Run %s not found or not running; nothing to execute", run_id)
            return None
        trip = db.get(Trip, run.trip_id)
        if trip is None:
            return None
        return search_trip(db, trip)


def _finish_phase(run_id: int, outcome: SearchOutcome) -> None:
    with session_scope() as db:
        run = db.get(SearchRun, run_id)
        trip = db.get(Trip, run.trip_id) if run is not None else None
        if run is None or trip is None:
            logger.info("Run %s or its trip was deleted during the search", run_id)
            return
        persisted = _persist_offers(db, trip, run, outcome.results)
        _finish_run(run, trip, outcome, persisted)
        db.commit()
        logger.info(
            "Run %s finished: %s, %d offers (%d matching)",
            run.id, run.status, run.offers_found, run.offers_matching,
        )
        if run.status in FINISHED_RUN_STATUSES:
            _alert_phase(db, trip, run, persisted, outcome.providers)
        _report_blocks(db, trip, run, outcome)


def _fail_run(run_id: int, message: str) -> None:
    try:
        with session_scope() as db:
            run = db.get(SearchRun, run_id)
            if run is None or run.status != "running":
                return
            _mark_finished(run, "error", message)
            trip = db.get(Trip, run.trip_id)
            if trip is not None:
                _update_trip(trip, run)
    except Exception as exc:  # noqa: BLE001
        logger.error("Could not mark run %s as failed: %s", run_id, safe_error(exc))


def _interrupt_run(run_id: int, why: str) -> None:
    """Mark a still-running run ``error: interrupted`` (trip.last_* left untouched)."""
    with session_scope() as db:
        run = db.get(SearchRun, run_id)
        if run is not None and run.status == "running":
            _mark_interrupted(run, why)
            logger.warning("Run %s interrupted: %s", run_id, why)


def _mark_interrupted(run: SearchRun, why: str) -> None:
    run.status = "error"
    run.error = INTERRUPTED
    run.finished_at = run.finished_at or utcnow()
    run.log.append(f"Run interrupted: {why}")


def _mark_finished(run: SearchRun, status: str, error: str | None) -> None:
    run.status = status
    run.error = error
    run.finished_at = utcnow()


def _update_trip(trip: Trip, run: SearchRun) -> None:
    trip.last_checked_at = run.finished_at
    trip.last_status = run.status
    trip.last_error = run.error


# --------------------------------------------------------------------------- persistence


def _persist_offers(
    db: Session, trip: Trip, run: SearchRun, results: list[tuple[FlightOffer, FlightQuery]]
) -> _Persisted:
    """Insert one ``FlightOffer`` row per result (flushed, ids assigned)."""
    if not results:
        return _Persisted(pairs=[], best=None)
    keys = {stored_offer_key(offer) for offer, _ in results}
    history = _price_history(db, trip.id, run.id, keys)
    searched_at = utcnow()
    pairs: list[tuple[OfferRow, FlightOffer]] = []
    mismatch_reasons: list[list[str]] = []
    for offer, query in results:
        matches, reasons = offer_matches_trip(offer, trip)
        if not matches:
            mismatch_reasons.append(reasons)
        history_key = (stored_offer_key(offer), offer.provider, offer.currency)
        previous, lowest = history.get(history_key, (None, None))
        row = _offer_row(offer, query, trip, run)
        row.searched_at = searched_at
        row.matches_filters = matches
        row.previous_price = previous
        row.lowest_price = (
            offer.price_per_person if lowest is None else min(lowest, offer.price_per_person)
        )
        db.add(row)
        pairs.append((row, offer))
    db.flush()
    summary = summarize_mismatches(mismatch_reasons)
    if summary:
        run.log.append(f"{len(mismatch_reasons)} offer(s) excluded by trip filters ({summary})")
    return _Persisted(pairs=pairs, best=_pick_best(pairs, trip))


def _price_history(
    db: Session, trip_id: int, run_id: int, keys: set[str]
) -> dict[tuple[str, str, str], tuple[float | None, float]]:
    """``(offer_key, provider, currency) -> (latest per-person price, lowest per-person)``
    over the trip's earlier runs."""
    groups: list[tuple[str, str, str, float, int]] = []
    ordered = sorted(keys)
    for start in range(0, len(ordered), _IN_CHUNK):
        chunk = ordered[start : start + _IN_CHUNK]
        stmt = (
            select(
                OfferRow.offer_key,
                OfferRow.provider,
                OfferRow.currency,
                func.min(OfferRow.price_per_person),
                func.max(OfferRow.id),
            )
            .where(
                OfferRow.trip_id == trip_id,
                OfferRow.run_id < run_id,
                OfferRow.offer_key.in_(chunk),
            )
            .group_by(OfferRow.offer_key, OfferRow.provider, OfferRow.currency)
        )
        groups.extend(tuple(row) for row in db.execute(stmt))  # type: ignore[misc]
    latest = _prices_by_id(db, [group[4] for group in groups])
    return {
        (key, provider, currency): (latest.get(last_id), lowest)
        for key, provider, currency, lowest, last_id in groups
    }


def _prices_by_id(db: Session, ids: list[int]) -> dict[int, float]:
    prices: dict[int, float] = {}
    for start in range(0, len(ids), _IN_CHUNK):
        chunk = ids[start : start + _IN_CHUNK]
        stmt = select(OfferRow.id, OfferRow.price_per_person).where(OfferRow.id.in_(chunk))
        prices.update({row_id: price for row_id, price in db.execute(stmt)})
    return prices


def _offer_row(offer: FlightOffer, query: FlightQuery, trip: Trip, run: SearchRun) -> OfferRow:
    """Flatten a provider offer into a ``FlightOffer`` row (outbound-leg spec fields)."""
    out, inbound = offer.outbound, offer.inbound
    return OfferRow(
        trip_id=trip.id,
        run_id=run.id,
        provider=offer.provider,
        seller=_clip(offer.seller, "seller"),
        offer_key=stored_offer_key(offer),
        origin=out.departure_airport,
        destination=out.arrival_airport,
        depart_date=query.depart_date,
        return_date=query.return_date,
        airline=_clip(_joined(out.airlines, ", "), "airline"),
        flight_number=_clip(_joined(out.flight_numbers, " / "), "flight_number"),
        departure_airport=out.departure_airport,
        arrival_airport=out.arrival_airport,
        departure_time=out.departure_time,
        arrival_time=out.arrival_time,
        duration_minutes=out.duration_minutes,
        stops=max(leg.stops for leg in (out, inbound) if leg is not None),
        outbound=out.to_dict(),
        inbound=inbound.to_dict() if inbound is not None else None,
        cabin=offer.cabin,
        fare_type=_clip(offer.fare_type, "fare_type"),
        baggage=_clip(offer.baggage, "baggage"),
        price_total=offer.price_total,
        price_per_person=offer.price_per_person,
        currency=offer.currency,
        booking_url=offer.booking_url,
        source_url=offer.source_url,
        raw=trim_raw(offer.raw),
    )


def stored_offer_key(offer: FlightOffer) -> str:
    """``offer.offer_key()`` as stored in ``FlightOffer.offer_key`` (clipped to the column)."""
    return _clip(offer.offer_key(), "offer_key") or ""


def _joined(values: list[str], separator: str) -> str | None:
    text = separator.join(v for v in values if v)
    return text or None


def _clip(value: str | None, column: str) -> str | None:
    limit = _LIMITS[column]
    if value is None or len(value) <= limit:
        return value
    return value[: limit - 1] + "…"


def trim_raw(raw: dict[str, Any] | None, max_chars: int = RAW_MAX_CHARS) -> dict[str, Any]:
    """JSON-safe copy of a provider's ``raw`` payload, trimmed to about ``max_chars``.

    Oversized payloads keep their small top-level entries and list the dropped keys
    under ``"_trimmed"``.
    """
    if not raw:
        return {}
    try:
        text = json.dumps(raw, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        return {"_trimmed": ["<unserializable>"]}
    if len(text) <= max_chars:
        return json.loads(text)
    kept: dict[str, Any] = {}
    dropped: list[str] = []
    budget = max_chars
    for key, value in raw.items():
        piece = json.dumps(value, default=str, ensure_ascii=False)
        if len(piece) <= min(_RAW_VALUE_MAX_CHARS, budget):
            kept[str(key)] = json.loads(piece)
            budget -= len(piece) + len(str(key)) + 4
        else:
            dropped.append(str(key))
    kept["_trimmed"] = dropped
    return kept


def _pick_best(
    pairs: list[tuple[OfferRow, FlightOffer]], trip: Trip
) -> tuple[OfferRow, FlightOffer] | None:
    """Cheapest matching offer in the trip's price basis (ties: fewer stops, shorter, earlier)."""
    matching = [pair for pair in pairs if pair[0].matches_filters]
    if not matching:
        return None
    total = trip.price_basis == "total"

    def rank(pair: tuple[OfferRow, FlightOffer]) -> tuple[float, int, int, str, int]:
        row = pair[0]
        price = row.price_total if total else row.price_per_person
        return (price, row.stops, row.duration_minutes or 10**6, row.departure_time, row.id)

    return min(matching, key=rank)


def _finish_run(run: SearchRun, trip: Trip, outcome: SearchOutcome, persisted: _Persisted) -> None:
    matching = [row for row, _ in persisted.pairs if row.matches_filters]
    run.offers_found = len(persisted.pairs)
    run.offers_matching = len(matching)
    run.min_price_total = min((row.price_total for row in matching), default=None)
    run.min_price_per_person = min((row.price_per_person for row in matching), default=None)
    run.currency = trip.currency
    run.best_offer_id = persisted.best[0].id if persisted.best else None
    run.provider_stats = dict(outcome.provider_stats)
    run.combinations_total = outcome.combos_total
    run.combinations_searched = outcome.combos_searched
    run.log = [*outcome.log, *run.log, _summary_line(run, trip, persisted)]
    _mark_finished(run, outcome.status, outcome.error)
    _update_trip(trip, run)


def _summary_line(run: SearchRun, trip: Trip, persisted: _Persisted) -> str:
    head = f"{run.offers_found} offer(s) found, {run.offers_matching} match the trip filters"
    if persisted.best is None:
        return head
    row = persisted.best[0]
    total = trip.price_basis == "total"
    price = row.price_total if total else row.price_per_person
    basis = "total" if total else "per person"
    return f"{head}; best {format_money(price, row.currency)} {basis} ({row.provider})"


# --------------------------------------------------------------------------- alerts


def _alert_phase(
    db: Session,
    trip: Trip,
    run: SearchRun,
    persisted: _Persisted,
    providers: dict[str, FlightProvider],
) -> None:
    """Evaluate alerts; if any fired, enrich the best offer and dispatch the notification."""
    try:
        events = evaluate_alerts(db, trip, run)
        db.commit()
    except Exception as exc:  # noqa: BLE001 - alerts must not break monitoring
        db.rollback()
        logger.error("Alert evaluation for run %s failed: %s", run.id, safe_error(exc))
        return
    if not events:
        return
    run.log.append("Alerts fired: " + ", ".join(event.reason or event.kind for event in events))
    db.commit()
    best_row = persisted.best[0] if persisted.best else None
    if persisted.best is not None:
        row, offer = persisted.best
        _enrich_best_offer(db, run, trip, row, offer, providers.get(row.provider))
    _dispatch(db, trip, run, best_row, events)


def _enrich_best_offer(
    db: Session,
    run: SearchRun,
    trip: Trip,
    row: OfferRow,
    offer: FlightOffer,
    provider: FlightProvider | None,
) -> None:
    """Best effort: fill in an unknown return flight via ``provider.get_flight_details``."""
    if provider is None or row.inbound is not None or row.return_date is None:
        return
    detailed = _call_with_timeout(provider.get_flight_details, offer, ENRICH_TIMEOUT_S)
    if detailed is None or detailed.inbound is None:
        return
    try:
        row.inbound = detailed.inbound.to_dict()
        row.stops = max(row.stops, detailed.inbound.stops)
        row.baggage = row.baggage or _clip(detailed.baggage, "baggage")
        row.fare_type = row.fare_type or _clip(detailed.fare_type, "fare_type")
        if detailed.booking_url and detailed.booking_url != row.booking_url:
            # The detailed link opens the exact booking page (both flights, dates, passengers);
            # keep the search-time link for reference.
            raw = dict(row.raw or {})
            if row.booking_url:
                raw["search_link"] = row.booking_url
            raw["booking_resolved"] = True
            row.raw = raw
            row.booking_url = detailed.booking_url
        matches, reasons = offer_matches_trip(detailed, trip)
        if not matches:
            run.log.append(
                "Best offer's return flight (fetched after the search) misses trip filters: "
                + "; ".join(reasons)
            )
        db.commit()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        logger.warning("Could not store flight details for offer %s: %s", row.id, safe_error(exc))


def _call_with_timeout(
    fn: Callable[[FlightOffer], FlightOffer], offer: FlightOffer, timeout: float
) -> FlightOffer | None:
    """``fn(offer)`` on a daemon thread (a hung provider never delays process exit);
    ``None`` on error or after ``timeout`` seconds."""
    box: dict[str, Any] = {}

    def target() -> None:
        try:
            box["value"] = fn(offer)
        except Exception as exc:  # noqa: BLE001 - details are optional
            box["error"] = exc

    worker = threading.Thread(target=target, name="offer-details", daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        logger.warning("Fetching flight details timed out after %.0f s", timeout)
        return None
    if "error" in box:
        logger.warning("Fetching flight details failed: %s", safe_error(box["error"]))
        return None
    return box.get("value")


def _dispatch(
    db: Session, trip: Trip, run: SearchRun, best_row: OfferRow | None, events: list[AlertEvent]
) -> None:
    notify = _load_notifier()
    if notify is None:
        return
    try:
        notify(db, trip, run, best_row, events)
        db.commit()
    except Exception as exc:  # noqa: BLE001 - notification problems never fail a run
        db.rollback()
        logger.error("Sending the price alert for trip %s failed: %s", trip.id, safe_error(exc))


def _load_notifier() -> Callable[..., Any] | None:
    """``notifications.dispatcher.notify_price_alert`` (imported lazily), or ``None``."""
    return _dispatcher_function("notify_price_alert", "price alert")


def _load_system_notifier() -> Callable[..., Any] | None:
    """``notifications.dispatcher.notify`` (imported lazily), or ``None``."""
    return _dispatcher_function("notify", "provider notice")


def _dispatcher_function(name: str, what: str) -> Callable[..., Any] | None:
    try:
        dispatcher = importlib.import_module("app.services.notifications.dispatcher")
    except ImportError as exc:
        logger.warning("Notification dispatcher unavailable (%s); %s not sent", exc, what)
        return None
    function = getattr(dispatcher, name, None)
    if function is None:
        logger.warning("Notification dispatcher has no %s(); %s not sent", name, what)
    return function


# --------------------------------------------------------------------------- provider blocks

_block_lock = threading.Lock()

_BLOCK_TEXT: dict[str, dict[str, str]] = {
    "en": {
        "title": "{provider} blocked automated searches",
        "body": (
            "While checking {trip} ({route}), {provider} answered with a block or captcha "
            "page: {reason}\n"
            "Its remaining searches in that check were skipped; it is tried again only at "
            "the next scheduled check (no retries in between).\n"
            "If this keeps happening, check less often or enable another flight provider "
            "(e.g. SerpApi) in Settings. You get at most one such notice per provider per day."
        ),
    },
    "ko": {
        "title": "{provider} 자동 검색 차단",
        "body": (
            "{trip} ({route}) 확인 중 {provider}이(가) 차단/캡차 페이지를 반환했습니다: {reason}\n"
            "이번 확인의 남은 검색은 건너뛰었으며, 다음 예약 확인 때까지 다시 시도하지 않습니다.\n"
            "계속 발생하면 확인 횟수를 줄이거나 설정에서 다른 항공권 제공자(예: SerpApi)를 "
            "사용하세요. 이 안내는 제공자별로 하루에 한 번만 보냅니다."
        ),
    },
}


def _report_blocks(db: Session, trip: Trip, run: SearchRun, outcome: SearchOutcome) -> None:
    """Send a ``system`` notification for each provider that blocked the search, at most
    once per provider per local day (research guardrail: stop and tell the user rather
    than retry). Never raises."""
    if not outcome.blocked:
        return
    notify = _load_system_notifier()
    if notify is None:
        return
    for key, reason in outcome.blocked.items():
        provider = outcome.providers.get(key)
        name = type(provider).display_name if provider is not None else key
        try:
            with _block_lock:
                if _block_reported_today(db, key):
                    continue
                message = _block_message(db, trip, run, key, name, reason)
                notify(db, kind="system", message=message, trip_id=trip.id)
                db.commit()
            logger.info("Told the user that %s blocked the search (trip %s)", key, trip.id)
        except Exception as exc:  # noqa: BLE001 - a notice must never fail the run
            db.rollback()
            logger.error("Could not send the %s block notice: %s", key, safe_error(exc))


def _block_reported_today(db: Session, key: str) -> bool:
    """Whether a block notice for provider ``key`` was stored since local midnight."""
    now = now_local()
    midnight = datetime.combine(now.date(), dtime.min, tzinfo=now.tzinfo)
    since = midnight.astimezone(UTC).replace(tzinfo=None)
    rows = db.scalars(
        select(Notification.data).where(
            Notification.kind == "system", Notification.created_at >= since
        )
    )
    return any(
        isinstance(data, dict) and data.get("event") == BLOCK_EVENT and data.get("provider") == key
        for data in rows
    )


def _block_message(
    db: Session, trip: Trip, run: SearchRun, key: str, name: str, reason: str
) -> NotificationMessage:
    try:
        language = load_runtime_settings(db).language
    except Exception:  # noqa: BLE001 - message language is cosmetic
        language = "en"
    text = _BLOCK_TEXT.get(language, _BLOCK_TEXT["en"])
    values = {
        "provider": name,
        "trip": trip.name or f"#{trip.id}",
        "route": f"{trip.origin} → {trip.destination}",
        "reason": reason,
    }
    return NotificationMessage(
        title=text["title"].format(**values),
        body=text["body"].format(**values),
        data={
            "event": BLOCK_EVENT,
            "provider": key,
            "trip_id": trip.id,
            "run_id": run.id,
            "reason": reason,
        },
    )
