"""Itineraries (DESIGN §7, §10): create + generate in the background, list, get, delete, refine.

Creating (or refining) commits a ``pending`` row and starts generation in a daemon thread
(:func:`app.services.itinerary.service.start_generation`); the UI polls ``GET
/itineraries/{id}`` while ``status`` is ``pending``/``running``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Itinerary, Trip
from app.schemas import ItineraryCreate, ItineraryOut, ItineraryRefine
from app.services.itinerary import service

router = APIRouter(tags=["itineraries"])


def _http_error(exc: service.ItineraryServiceError) -> HTTPException:
    if isinstance(exc, service.TripNotFoundError | service.ItineraryNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


def _itinerary_or_404(db: Session, itinerary_id: int) -> Itinerary:
    row = db.get(Itinerary, itinerary_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"itinerary {itinerary_id} not found")
    return row


@router.post("/trips/{trip_id}/itineraries", response_model=ItineraryOut, status_code=201)
def create_itinerary(
    trip_id: int, body: ItineraryCreate, db: Session = Depends(get_db)
) -> ItineraryOut:
    """Validate the request, store a ``pending`` itinerary and generate it in the background.

    404 unknown trip; 400 when it cannot be planned (no preferences, offer of another trip,
    no return time, inconsistent times, unknown destination).
    """
    try:
        row = service.create_itinerary(db, trip_id, body)
    except service.ItineraryServiceError as exc:
        db.rollback()
        raise _http_error(exc) from None
    out = ItineraryOut.model_validate(row)  # snapshot before the worker touches the row
    service.start_generation(row.id)
    return out


@router.get("/trips/{trip_id}/itineraries", response_model=list[ItineraryOut])
def list_itineraries(trip_id: int, db: Session = Depends(get_db)) -> list[Itinerary]:
    """Itineraries of a trip, newest first."""
    if db.get(Trip, trip_id) is None:
        raise HTTPException(status_code=404, detail=f"trip {trip_id} not found")
    query = select(Itinerary).where(Itinerary.trip_id == trip_id).order_by(Itinerary.id.desc())
    return list(db.scalars(query))


@router.get("/itineraries/{itinerary_id}", response_model=ItineraryOut)
def get_itinerary(itinerary_id: int, db: Session = Depends(get_db)) -> Itinerary:
    """One itinerary (poll while ``status`` is ``pending``/``running``)."""
    return _itinerary_or_404(db, itinerary_id)


@router.delete("/itineraries/{itinerary_id}", status_code=204, response_class=Response)
def delete_itinerary(itinerary_id: int, db: Session = Depends(get_db)) -> Response:
    """Delete an itinerary (refinements keep existing with ``parent_id`` cleared).

    A generation still running for it finds the row gone and stops saving.
    """
    db.delete(_itinerary_or_404(db, itinerary_id))
    db.commit()
    return Response(status_code=204)


@router.post("/itineraries/{itinerary_id}/refine", response_model=ItineraryOut, status_code=201)
def refine_itinerary(
    itinerary_id: int, body: ItineraryRefine, db: Session = Depends(get_db)
) -> ItineraryOut:
    """Create a revised version from ``feedback`` (new ``pending`` row with ``parent_id``).

    404 unknown itinerary; 400 when the parent is not finished.
    """
    try:
        row = service.create_refinement(db, itinerary_id, body.feedback, start=False)
    except service.ItineraryServiceError as exc:
        db.rollback()
        raise _http_error(exc) from None
    out = ItineraryOut.model_validate(row)
    service.start_generation(row.id)
    return out
