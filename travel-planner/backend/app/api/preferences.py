"""Travel style + budget of a trip (DESIGN §7): ``GET/PUT /trips/{id}/preferences``.

``GET`` answers 404 while nothing was saved for the trip (the UI then shows its defaults);
``PUT`` creates or replaces the whole preference set.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api._common import get_trip_or_404, not_found
from app.db import get_db
from app.models import TravelPreference
from app.schemas import PreferencesIn, PreferencesOut

router = APIRouter(tags=["preferences"])


@router.get("/trips/{trip_id}/preferences", response_model=PreferencesOut)
def get_preferences(trip_id: int, db: Session = Depends(get_db)) -> TravelPreference:
    """The trip's saved preferences (404 ``"Preferences not found"`` when none yet)."""
    get_trip_or_404(db, trip_id)
    preferences = db.get(TravelPreference, trip_id)
    if preferences is None:
        raise not_found("Preferences")
    return preferences


@router.put("/trips/{trip_id}/preferences", response_model=PreferencesOut)
def put_preferences(
    trip_id: int, body: PreferencesIn, db: Session = Depends(get_db)
) -> TravelPreference:
    """Create or replace the trip's preferences."""
    get_trip_or_404(db, trip_id)
    values = body.model_dump()
    preferences = db.get(TravelPreference, trip_id)
    if preferences is None:
        preferences = TravelPreference(trip_id=trip_id, **values)
        db.add(preferences)
    else:
        for name, value in values.items():
            setattr(preferences, name, value)
    db.commit()
    return preferences
