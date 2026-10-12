"""Live keyless pipeline (RUN_LIVE=1): real Overpass, Nominatim, Open-Meteo and Frankfurter.

Small and polite: ≤ 2 Overpass box queries, a handful of cached Nominatim lookups (1.1 s
apart), one Open-Meteo request per climatology year and one FX rate."""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from sqlalchemy.orm import Session

from app.models import TravelPreference, Trip
from app.schemas import ItineraryCreate, ItineraryPlan
from app.services.itinerary.service import create_itinerary, generate_itinerary
from app.services.notifications import desktop


@pytest.mark.live
def test_live_rule_based_itinerary(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    shown: list[Any] = []
    monkeypatch.setattr(desktop, "toast_support", lambda: (True, ""))
    monkeypatch.setattr(desktop, "register_aumid", lambda icon_path=None: True)
    monkeypatch.setattr(desktop, "_show_toast", shown.append)
    trip = Trip(name="Tokyo", origin="ICN", destination="TYO", trip_type="round_trip",
                depart_date=date(2026, 11, 20), return_date=date(2026, 11, 22), adults=2, currency="KRW",
                preferences=TravelPreference(budget_total=2_000_000, budget_flight=700_000, hotel_area="Asakusa",
                                             food_wishlist=["sushi"]))
    db.add(trip)
    db.commit()
    row = create_itinerary(db, trip.id, ItineraryCreate(arrival_at="2026-11-20T13:00",
                                                        departure_at="2026-11-22T18:00"))
    generate_itinerary(row.id)
    db.expire_all()
    row = db.get(type(row), row.id)
    assert row is not None and row.status == "done", row.error
    plan = ItineraryPlan.model_validate(row.plan)
    activities = [i for d in plan.days for i in d.items if i.kind == "activity"]
    assert row.generator == "rule_based" and len(plan.days) == 3 and activities
    assert plan.days[1].weather and plan.budget is not None
    print(plan.title, len(activities), "activities;", plan.warnings[:3])
