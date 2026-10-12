"""Trips: CRUD and manual checks (DESIGN §7).

* ``GET/POST /trips``; ``GET/PUT/DELETE /trips/{id}`` — creating a trip attaches the default
  alert rules (``below_target`` when it has a target price, ``new_low`` always) and schedules
  it; updating re-validates the merged trip (422) and re-syncs its schedule; deleting removes
  its jobs.
* ``POST /trips/{id}/check`` — starts a manual check in the background and returns the run
  (``status == "running"``; poll ``GET /runs/{id}``). A check already in progress for the
  trip is returned instead of starting a second one.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api._common import (
    get_or_404,
    get_trip_or_404,
    not_found,
    sync_schedule,
    trip_out,
    trip_outs,
    unschedule,
    validation_error,
)
from app.db import get_db
from app.models import SearchRun, Trip
from app.schemas import SearchRunOut, TripCreate, TripOut, TripUpdate
from app.services import monitoring
from app.services.alerts import default_alert_rules

logger = logging.getLogger(__name__)

router = APIRouter(tags=["trips"])


@router.get("/trips", response_model=list[TripOut])
def list_trips(db: Session = Depends(get_db)) -> list[TripOut]:
    """All trips: active first, then by departure date."""
    trips = db.scalars(
        select(Trip).order_by(Trip.active.desc(), Trip.depart_date, Trip.id)
    ).all()
    return trip_outs(db, trips)


@router.post("/trips", response_model=TripOut, status_code=status.HTTP_201_CREATED)
def create_trip(body: TripCreate, db: Session = Depends(get_db)) -> TripOut:
    """Create a trip with its default alert rules and schedule it."""
    trip = Trip(**body.model_dump())
    trip.alert_rules.extend(default_alert_rules(trip))
    db.add(trip)
    db.commit()
    logger.info("Trip %s created: %s", trip.id, trip.name)
    sync_schedule(trip)
    return trip_out(db, trip)


@router.get("/trips/{trip_id}", response_model=TripOut)
def get_trip(trip_id: int, db: Session = Depends(get_db)) -> TripOut:
    """One trip with its computed summary fields."""
    return trip_out(db, get_trip_or_404(db, trip_id))


@router.put("/trips/{trip_id}", response_model=TripOut)
def update_trip(trip_id: int, body: TripUpdate, db: Session = Depends(get_db)) -> TripOut:
    """Partial update; the merged trip is validated as a whole (422 when invalid).

    Setting a target price on a trip that has no ``below_target`` rule yet adds one that
    follows the trip's target (as a new trip would get).
    """
    trip = get_trip_or_404(db, trip_id)
    current = {name: getattr(trip, name) for name in TripCreate.model_fields}
    try:
        merged = body.merged_with(current)
    except ValidationError as exc:
        raise validation_error(exc) from exc
    had_target = trip.target_price is not None
    for name, value in merged.model_dump().items():
        setattr(trip, name, value)
    if not had_target and trip.target_price is not None:
        _ensure_target_rule(trip)
    db.commit()
    sync_schedule(trip)
    return trip_out(db, trip)


def _ensure_target_rule(trip: Trip) -> None:
    if any(rule.kind == "below_target" for rule in trip.alert_rules):
        return
    trip.alert_rules.extend(
        rule for rule in default_alert_rules(trip) if rule.kind == "below_target"
    )
    logger.info("Trip %s: target price set; below_target alert rule added", trip.id)


@router.delete("/trips/{trip_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_trip(trip_id: int, db: Session = Depends(get_db)) -> Response:
    """Delete a trip with its runs, offers, rules, notifications, preferences, itineraries."""
    trip = get_trip_or_404(db, trip_id)
    db.delete(trip)
    db.commit()
    unschedule(trip_id)
    logger.info("Trip %s deleted", trip_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/trips/{trip_id}/check", response_model=SearchRunOut, status_code=status.HTTP_202_ACCEPTED
)
def check_trip(trip_id: int, db: Session = Depends(get_db)) -> SearchRunOut:
    """Start a manual check in the background; returns its run (poll ``GET /runs/{id}``)."""
    get_trip_or_404(db, trip_id)
    db.rollback()  # end the read transaction; the check runs in its own sessions
    try:
        run_id = monitoring.start_trip_check(trip_id, "manual")
    except monitoring.TripNotFoundError as exc:
        raise not_found("Trip") from exc
    except monitoring.RunInProgressError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return SearchRunOut.model_validate(get_or_404(db, SearchRun, run_id, "Run"))
