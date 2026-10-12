"""Helpers shared by the trip-related routers (not a router: ``_``-prefixed, never mounted).

* :func:`get_trip_or_404` / :func:`get_or_404` load a row or raise HTTP 404.
* :func:`validation_error` turns a ``pydantic.ValidationError`` raised while merging a
  partial update (``TripUpdate.merged_with`` ...) into FastAPI's 422 response shape.
* :func:`trip_outs` / :func:`trip_out` build :class:`~app.schemas.TripOut` with its computed
  summary fields (latest/lowest prices, next scheduled check, unread notifications).
* :func:`sync_schedule` / :func:`unschedule` keep the monitoring scheduler in step with the
  trips table without ever failing the request.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime
from typing import Any, TypeVar

from fastapi import HTTPException, status
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import Base
from app.models import Notification, SearchRun, Trip
from app.schemas import TripOut
from app.services.alerts import FINISHED_RUN_STATUSES, run_best_price

logger = logging.getLogger(__name__)

ModelT = TypeVar("ModelT", bound=Base)


# --------------------------------------------------------------------------- errors


def not_found(what: str) -> HTTPException:
    """HTTP 404 with ``{"detail": "<what> not found"}``."""
    return HTTPException(status.HTTP_404_NOT_FOUND, detail=f"{what} not found")


def get_or_404(db: Session, model: type[ModelT], ident: Any, what: str) -> ModelT:
    """``db.get(model, ident)`` or HTTP 404."""
    row = db.get(model, ident)
    if row is None:
        raise not_found(what)
    return row


def get_trip_or_404(db: Session, trip_id: int) -> Trip:
    """The trip or HTTP 404 ``"Trip not found"``."""
    return get_or_404(db, Trip, trip_id, "Trip")


def validation_error(exc: ValidationError) -> RequestValidationError:
    """A 422 in FastAPI's own shape (``detail: [{loc: ["body", ...], msg, type}]``)."""
    errors = [
        {**error, "loc": ("body", *error.get("loc", ()))}
        for error in exc.errors(include_url=False, include_context=False, include_input=False)
    ]
    return RequestValidationError(errors)


# --------------------------------------------------------------------------- scheduler


def _scheduler() -> Any:
    from app.services.scheduler import get_scheduler  # lazy: APScheduler + monitoring

    return get_scheduler()


def sync_schedule(trip: Trip) -> None:
    """(Re)create the trip's scheduler jobs; failures are logged, never raised."""
    try:
        _scheduler().sync_trip(trip)
    except Exception:  # noqa: BLE001 - the trip is saved; the schedule is rebuilt on restart
        logger.exception("Could not schedule trip %s", trip.id)


def unschedule(trip_id: int) -> None:
    """Remove the trip's scheduler jobs; failures are logged, never raised."""
    try:
        from app.services.scheduler import unschedule_trip

        unschedule_trip(trip_id)
    except Exception:  # noqa: BLE001
        logger.exception("Could not remove the scheduler jobs of trip %s", trip_id)


def _next_runs(trip_ids: Sequence[int]) -> dict[int, datetime | None]:
    try:
        scheduler = _scheduler()
        return {trip_id: scheduler.next_run_for_trip(trip_id) for trip_id in trip_ids}
    except Exception:  # noqa: BLE001 - a summary field must not break the trips API
        logger.exception("Could not read next run times from the scheduler")
        return {}


# --------------------------------------------------------------------------- TripOut


def trip_outs(db: Session, trips: Sequence[Trip]) -> list[TripOut]:
    """``TripOut`` for each trip, computed fields filled with a few batched queries.

    * ``latest_min_price`` / ``latest_min_price_basis_value``: the best matching price of the
      latest finished (``ok``/``partial``) run, total / in the trip's price basis.
    * ``lowest_ever``: lowest best matching price over all finished runs, in the trip's basis.
    * Runs in another currency than the trip's current one are ignored (no implicit FX).
    * ``next_run_at``: from the scheduler; ``unread_alerts``: unread notifications of the trip.
    """
    ids = [trip.id for trip in trips]
    if not ids:
        return []
    latest = _latest_finished_runs(db, ids)
    lowest = _lowest_run_prices(db, ids)
    unread = _unread_counts(db, ids)
    next_runs = _next_runs(ids)
    result: list[TripOut] = []
    for trip in trips:
        out = TripOut.model_validate(trip)
        basis = trip.price_basis or "per_person"
        run = latest.get(trip.id)
        if run is not None and run.currency == trip.currency:
            out.latest_min_price = run.min_price_total
            out.latest_min_price_basis_value = run_best_price(run, basis)
        per_person, total = lowest.get((trip.id, trip.currency), (None, None))
        out.lowest_ever = total if basis == "total" else per_person
        out.next_run_at = next_runs.get(trip.id)
        out.unread_alerts = unread.get(trip.id, 0)
        result.append(out)
    return result


def trip_out(db: Session, trip: Trip) -> TripOut:
    """``TripOut`` for one trip (see :func:`trip_outs`)."""
    return trip_outs(db, [trip])[0]


def _latest_finished_runs(db: Session, trip_ids: list[int]) -> dict[int, SearchRun]:
    latest_ids = (
        select(func.max(SearchRun.id))
        .where(SearchRun.trip_id.in_(trip_ids), SearchRun.status.in_(FINISHED_RUN_STATUSES))
        .group_by(SearchRun.trip_id)
    )
    runs = db.scalars(select(SearchRun).where(SearchRun.id.in_(latest_ids)))
    return {run.trip_id: run for run in runs}


def _lowest_run_prices(
    db: Session, trip_ids: list[int]
) -> dict[tuple[int, str], tuple[float | None, float | None]]:
    """``(trip_id, currency) -> (lowest per-person, lowest total)`` over finished runs."""
    stmt = (
        select(
            SearchRun.trip_id,
            SearchRun.currency,
            func.min(SearchRun.min_price_per_person),
            func.min(SearchRun.min_price_total),
        )
        .where(SearchRun.trip_id.in_(trip_ids), SearchRun.status.in_(FINISHED_RUN_STATUSES))
        .group_by(SearchRun.trip_id, SearchRun.currency)
    )
    return {
        (trip_id, currency): (per_person, total)
        for trip_id, currency, per_person, total in db.execute(stmt)
    }


def _unread_counts(db: Session, trip_ids: list[int]) -> dict[int, int]:
    stmt = (
        select(Notification.trip_id, func.count(Notification.id))
        .where(Notification.trip_id.in_(trip_ids), Notification.read_at.is_(None))
        .group_by(Notification.trip_id)
    )
    return {trip_id: count for trip_id, count in db.execute(stmt)}
