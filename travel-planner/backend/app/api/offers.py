"""Offers, price history and the "Book" redirect (DESIGN §7).

* ``GET /trips/{id}/offers?run_id=&only_matching=true&sort=price|departure|duration&limit=``
  — offers of one run (default: the latest finished run, i.e. status ``ok``/``partial``).
* ``GET /trips/{id}/price-history`` — best matching prices per finished run (+ per provider).
* ``GET /trips/{id}/offers/{offer_id}/history`` — every observation of that itinerary
  (same ``offer_key`` and provider).
* ``GET /offers/{offer_id}/book`` — the target of the UI's "Book" button: a 302 to the most
  specific booking page known for the offer. Round-trip offers whose return flight is unknown
  (Google Flights lists outbound options priced at the round-trip total) are resolved on the
  first click: the offer's provider looks up the return flight (``get_flight_details``, one
  provider request, at most :data:`BOOK_RESOLVE_TIMEOUT_S` seconds) and the result — inbound
  leg plus Google's "Booking options" page with both flights, the dates and the passengers —
  is stored on the offer (``raw["booking_resolved"]``; the previous link is kept in
  ``raw["search_link"]``), so later clicks redirect at once. Any failure falls back to the
  stored link. A lookup that outlives the timeout still stores its result when it finishes.
"""

from __future__ import annotations

import copy
import logging
import threading
from collections import defaultdict
from concurrent.futures import Future
from typing import Any, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api._common import get_or_404, get_trip_or_404, not_found
from app.db import get_db, session_scope
from app.models import FlightOffer as OfferRow
from app.models import SearchRun
from app.providers.flights import get_provider_class
from app.providers.flights.base import FlightOffer, FlightProvider
from app.schemas import (
    FlightOfferOut,
    OfferHistoryOut,
    OfferHistoryPoint,
    PriceHistoryOut,
    PricePoint,
)
from app.services import search as search_service
from app.services.alerts import FINISHED_RUN_STATUSES
from app.services.monitoring import trim_raw
from app.services.search import safe_error
from app.settings_store import load_runtime_settings

logger = logging.getLogger(__name__)

router = APIRouter(tags=["offers"])

OfferSort = Literal["price", "departure", "duration"]

BOOK_RESOLVE_TIMEOUT_S = 45.0
"""How long ``GET /offers/{id}/book`` waits for the provider to find the return flight."""
GOOGLE_LINK_PROVIDERS = frozenset({"google_flights", "serpapi"})
"""Providers whose flights come from Google Flights, so a "Booking options" link can be
built from the offer's flight numbers (``google_flights_tfs.booking_link``)."""
GOOGLE_BOOKING_PATH = "/travel/flights/booking"
RESOLVED_FLAG = "booking_resolved"
SEARCH_LINK_KEY = "search_link"
_BAGGAGE_MAX = 200  # FlightOffer column sizes
_FARE_TYPE_MAX = 100


# --------------------------------------------------------------------------- offers


@router.get("/trips/{trip_id}/offers", response_model=list[FlightOfferOut])
def list_offers(
    trip_id: int,
    run_id: int | None = None,
    only_matching: bool = True,
    sort: OfferSort = "price",
    limit: int = Query(200, ge=1, le=2000),
    db: Session = Depends(get_db),
) -> list[OfferRow]:
    """Offers of ``run_id`` (default: the latest finished run; none yet -> ``[]``)."""
    get_trip_or_404(db, trip_id)
    if run_id is not None:
        run = db.get(SearchRun, run_id)
        if run is None or run.trip_id != trip_id:
            raise not_found("Run")
    else:
        run_id = db.scalar(
            select(func.max(SearchRun.id)).where(
                SearchRun.trip_id == trip_id, SearchRun.status.in_(FINISHED_RUN_STATUSES)
            )
        )
        if run_id is None:
            return []
    stmt = select(OfferRow).where(OfferRow.run_id == run_id)
    if only_matching:
        stmt = stmt.where(OfferRow.matches_filters.is_(True))
    return list(db.scalars(stmt.order_by(*_order(sort)).limit(limit)))


def _order(sort: OfferSort) -> list[Any]:
    price = [OfferRow.price_total, OfferRow.price_per_person]
    if sort == "departure":
        return [OfferRow.departure_time, *price, OfferRow.id]
    if sort == "duration":
        return [OfferRow.duration_minutes.is_(None), OfferRow.duration_minutes, *price, OfferRow.id]
    return [*price, OfferRow.stops, OfferRow.departure_time, OfferRow.id]


@router.get("/trips/{trip_id}/price-history", response_model=PriceHistoryOut)
def price_history(trip_id: int, db: Session = Depends(get_db)) -> PriceHistoryOut:
    """Best matching prices of every finished run in the trip's currency, oldest first.

    ``by_provider`` holds each provider's lowest matching per-person price of the run.
    """
    trip = get_trip_or_404(db, trip_id)
    runs = db.scalars(
        select(SearchRun)
        .where(
            SearchRun.trip_id == trip_id,
            SearchRun.status.in_(FINISHED_RUN_STATUSES),
            SearchRun.currency == trip.currency,
        )
        .order_by(SearchRun.id)
    ).all()
    by_run: dict[int, dict[str, float]] = defaultdict(dict)
    stmt = (
        select(OfferRow.run_id, OfferRow.provider, func.min(OfferRow.price_per_person))
        .where(
            OfferRow.trip_id == trip_id,
            OfferRow.matches_filters.is_(True),
            OfferRow.currency == trip.currency,
        )
        .group_by(OfferRow.run_id, OfferRow.provider)
    )
    for run_id, provider, price in db.execute(stmt):
        by_run[run_id][provider] = price
    points = [
        PricePoint(
            run_id=run.id,
            checked_at=run.finished_at or run.started_at,
            min_total=run.min_price_total,
            min_per_person=run.min_price_per_person,
            by_provider=by_run.get(run.id, {}),
        )
        for run in runs
    ]
    return PriceHistoryOut(
        trip_id=trip.id, currency=trip.currency, price_basis=trip.price_basis, points=points
    )


@router.get("/trips/{trip_id}/offers/{offer_id}/history", response_model=OfferHistoryOut)
def offer_history(trip_id: int, offer_id: int, db: Session = Depends(get_db)) -> OfferHistoryOut:
    """Every observation of the offer's itinerary (same ``offer_key``, provider, currency)."""
    get_trip_or_404(db, trip_id)
    offer = db.get(OfferRow, offer_id)
    if offer is None or offer.trip_id != trip_id:
        raise not_found("Offer")
    rows = db.scalars(
        select(OfferRow)
        .where(
            OfferRow.trip_id == trip_id,
            OfferRow.offer_key == offer.offer_key,
            OfferRow.provider == offer.provider,
            OfferRow.currency == offer.currency,
        )
        .order_by(OfferRow.searched_at, OfferRow.id)
    ).all()
    return OfferHistoryOut(
        trip_id=trip_id,
        offer_key=offer.offer_key,
        currency=offer.currency,
        lowest_per_person=min((row.price_per_person for row in rows), default=None),
        points=[
            OfferHistoryPoint(
                offer_id=row.id,
                run_id=row.run_id,
                searched_at=row.searched_at,
                provider=row.provider,
                price_total=row.price_total,
                price_per_person=row.price_per_person,
            )
            for row in rows
        ],
    )


# --------------------------------------------------------------------------- booking redirect


@router.get(
    "/offers/{offer_id}/book",
    response_class=RedirectResponse,
    status_code=status.HTTP_302_FOUND,
    responses={404: {"description": "Unknown offer"}, 409: {"description": "No booking link"}},
)
def book_offer(offer_id: int, db: Session = Depends(get_db)) -> RedirectResponse:
    """302 to the offer's booking page, resolving an unknown return flight first (see module)."""
    row = get_or_404(db, OfferRow, offer_id, "Offer")
    stored = row.booking_url or row.source_url
    if not stored:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="This offer has no booking link")
    one_way = row.return_date is None
    if one_way or row.raw.get(RESOLVED_FLAG) or is_google_booking_page(row.booking_url):
        return _redirect(stored)

    offer = offer_from_row(row)
    if row.inbound is not None:
        # Return flight already known (e.g. filled in when an alert fired): no request needed.
        link = google_booking_link(offer) if row.provider in GOOGLE_LINK_PROVIDERS else None
        if link is None:
            return _redirect(stored)
        db.rollback()
        return _redirect(_store_resolution(offer_id, offer, link) or stored)

    provider = _details_provider(db, row)
    db.rollback()  # end the read transaction before the (slow) provider request
    if provider is None:
        return _redirect(stored)
    return _redirect(_await_resolution(offer_id, offer, provider) or stored)


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=status.HTTP_302_FOUND)


def is_google_booking_page(url: str | None) -> bool:
    """True for Google Flights' "Booking options" page (``/travel/flights/booking``)."""
    if not url:
        return False
    parts = urlsplit(url)
    is_google = ".google." in f".{parts.netloc.lower()}"  # google.com, www.google.co.kr, ...
    return is_google and parts.path.rstrip("/") == GOOGLE_BOOKING_PATH


def offer_adults(row: OfferRow) -> int:
    """Passengers the offer was priced for (``price_total / price_per_person``), else the
    trip's current ``adults``."""
    if row.price_per_person and row.price_per_person > 0:
        ratio = round(row.price_total / row.price_per_person)
        if 1 <= ratio <= 9:
            return ratio
    trip = row.trip
    return max(trip.adults or 1, 1) if trip is not None else 1


def offer_from_row(row: OfferRow) -> FlightOffer:
    """The provider dataclass of a stored offer (copies; the row is never mutated)."""
    data: dict[str, Any] = {
        "provider": row.provider,
        "outbound": copy.deepcopy(dict(row.outbound)),
        "inbound": copy.deepcopy(dict(row.inbound)) if row.inbound else None,
        "price_total": row.price_total,
        "price_per_person": row.price_per_person,
        "currency": row.currency,
        "cabin": row.cabin,
        "adults": offer_adults(row),
        "fare_type": row.fare_type,
        "baggage": row.baggage,
        "seller": row.seller,
        "booking_url": row.booking_url,
        "source_url": row.source_url,
        "return_date": row.return_date.isoformat() if row.return_date else None,
        "raw": copy.deepcopy(dict(row.raw or {})),
    }
    return FlightOffer.from_dict(data)


def google_booking_link(offer: FlightOffer) -> str | None:
    """Google's "Booking options" link for an offer whose flights are all known (every leg's
    segments carry flight numbers), else ``None``."""
    if offer.inbound is None and offer.return_date:
        return None
    try:
        from app.providers.flights.google_flights import search_query_for_offer
        from app.providers.flights.google_flights_tfs import booking_link, selected_from_leg

        outbound = selected_from_leg(offer.outbound)
        inbound = selected_from_leg(offer.inbound) if offer.inbound is not None else ()
        if not outbound or inbound is None:
            return None
        return booking_link(search_query_for_offer(offer), outbound, inbound)
    except Exception as exc:  # noqa: BLE001 - a link is optional
        logger.warning("Could not build a Google booking link: %s", safe_error(exc))
        return None


def _details_provider(db: Session, row: OfferRow) -> FlightProvider | None:
    """The offer's provider, created like a search creates it, if it can look up return
    flights (overrides ``get_flight_details``) and is usable; else ``None``."""
    try:
        cls = get_provider_class(row.provider)
    except KeyError:
        logger.warning("Offer %s: provider %r is not installed; using the stored link",
                       row.id, row.provider)
        return None
    if cls.get_flight_details is FlightProvider.get_flight_details:
        return None
    try:
        _key, provider, reason = search_service._provider_slot(
            row.provider, load_runtime_settings(db)
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Offer %s: provider %s unavailable: %s", row.id, row.provider,
                       safe_error(exc))
        return None
    if provider is None:
        logger.warning("Offer %s: provider %s unavailable (%s); using the stored link",
                       row.id, row.provider, reason)
    return provider


_inflight: dict[int, Future[str | None]] = {}
_inflight_lock = threading.Lock()


def _await_resolution(offer_id: int, offer: FlightOffer, provider: FlightProvider) -> str | None:
    """Resolve on a daemon thread (shared by concurrent clicks on the same offer) and wait
    up to :data:`BOOK_RESOLVE_TIMEOUT_S`; ``None`` on failure or timeout."""
    future = _start_resolution(offer_id, offer, provider)
    try:
        return future.result(timeout=BOOK_RESOLVE_TIMEOUT_S)
    except TimeoutError:
        logger.warning(
            "Offer %s: finding the return flight took over %.0f s; redirected to the stored "
            "link (the result is saved when it arrives)", offer_id, BOOK_RESOLVE_TIMEOUT_S,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Offer %s: finding the return flight failed: %s", offer_id, safe_error(exc))
    return None


def _start_resolution(
    offer_id: int, offer: FlightOffer, provider: FlightProvider
) -> Future[str | None]:
    with _inflight_lock:
        future = _inflight.get(offer_id)
        if future is not None:
            return future
        future = Future()
        _inflight[offer_id] = future
    worker = threading.Thread(
        target=_resolution_worker,
        args=(offer_id, offer, provider, future),
        name=f"book-offer-{offer_id}",
        daemon=True,  # a hung provider never delays process exit
    )
    worker.start()
    return future


def _resolution_worker(
    offer_id: int, offer: FlightOffer, provider: FlightProvider, future: Future[str | None]
) -> None:
    try:
        url = _resolve(offer_id, offer, provider)
    except BaseException as exc:  # noqa: BLE001 - reported through the future
        future.set_exception(exc)
    else:
        future.set_result(url)
    finally:
        with _inflight_lock:
            if _inflight.get(offer_id) is future:
                del _inflight[offer_id]


def _resolve(offer_id: int, offer: FlightOffer, provider: FlightProvider) -> str | None:
    """Look up the return flight, store it with the best link; returns the link to use."""
    detailed = provider.get_flight_details(offer)
    if detailed is None or detailed.inbound is None:
        logger.warning(
            "Offer %s: %s found no return flight; redirecting to the stored link",
            offer_id, offer.provider,
        )
        return None
    link = detailed.booking_url if detailed.booking_url != offer.booking_url else None
    if not is_google_booking_page(link) and offer.provider in GOOGLE_LINK_PROVIDERS:
        link = google_booking_link(detailed) or link
    url = _store_resolution(offer_id, detailed, link)
    logger.info("Offer %s: booking page resolved (%s)", offer_id, offer.provider)
    return url


def _store_resolution(offer_id: int, detailed: FlightOffer, link: str | None) -> str | None:
    """Save the known return flight and ``link`` on the offer row (marked resolved, the
    previous link kept in ``raw["search_link"]``); returns the link to redirect to."""
    with session_scope() as db:
        row = db.get(OfferRow, offer_id)
        if row is None:  # deleted meanwhile
            return link
        if row.raw.get(RESOLVED_FLAG):  # resolved by a concurrent request
            return row.booking_url or row.source_url
        previous = row.booking_url or row.source_url
        if detailed.inbound is not None:
            row.inbound = detailed.inbound.to_dict()
            row.stops = max(row.stops, detailed.inbound.stops)
        row.baggage = row.baggage or _clip(detailed.baggage, _BAGGAGE_MAX)
        row.fare_type = row.fare_type or _clip(detailed.fare_type, _FARE_TYPE_MAX)
        extra = {
            key: value
            for key, value in (detailed.raw or {}).items()
            if key not in (RESOLVED_FLAG, SEARCH_LINK_KEY)
        }
        row.raw = trim_raw({RESOLVED_FLAG: True, SEARCH_LINK_KEY: previous, **extra})
        if link:
            row.booking_url = link
        return row.booking_url or row.source_url


def _clip(value: str | None, limit: int) -> str | None:
    if value is None or len(value) <= limit:
        return value
    return value[: limit - 1] + "…"
