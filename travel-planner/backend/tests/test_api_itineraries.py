"""``/api/trips/{id}/itineraries`` and ``/api/itineraries/{id}``: create (background start),
list, get, delete, refine. Generation is replaced: no network and no threads left running."""

from __future__ import annotations

import threading
from datetime import date
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.db import session_scope
from app.models import Itinerary, TravelPreference, Trip
from app.services.itinerary import service

PLAN = {"title": "Tokyo in 4 days", "destination": "Tokyo", "currency_local": "JPY", "days": []}
MANUAL = {"arrival_at": "2026-11-20T14:00", "departure_at": "2026-11-23T19:00"}


@pytest.fixture
def started(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Records background starts instead of running the pipeline."""
    ids: list[int] = []
    monkeypatch.setattr(
        service, "start_generation", lambda itinerary_id, **_: ids.append(itinerary_id) or True
    )
    return ids


def make_trip(db: Session, *, prefs: bool = True) -> Trip:
    trip = Trip(
        name="Tokyo", origin="ICN", destination="TYO", trip_type="round_trip",
        depart_date=date(2026, 11, 20), return_date=date(2026, 11, 23), adults=2, currency="KRW",
    )
    if prefs:
        trip.preferences = TravelPreference(
            budget_total=3_000_000, budget_flight=1_000_000, hotel_area="Asakusa"
        )
    db.add(trip)
    db.commit()
    return trip


def make_done(db: Session, trip: Trip, **fields: Any) -> Itinerary:
    row = Itinerary(trip_id=trip.id, status="done", plan=dict(PLAN), request={"language": "en"}, **fields)
    db.add(row)
    db.commit()
    return row


# --------------------------------------------------------------------------- create


def test_create_returns_pending_and_starts_generation(
    client: TestClient, db: Session, started: list[int]
) -> None:
    trip = make_trip(db)
    response = client.post(
        f"/api/trips/{trip.id}/itineraries", json={**MANUAL, "notes": "anniversary", "use_ai": False}
    )
    assert response.status_code == 201
    data = response.json()
    assert data["status"] == "pending" and data["trip_id"] == trip.id and data["plan"] is None
    assert (data["arrival_at"], data["departure_at"]) == (MANUAL["arrival_at"], MANUAL["departure_at"])
    assert data["hotel_area"] == "Asakusa" and data["parent_id"] is None
    assert data["request"]["use_ai"] is False and data["request"]["source"] == "manual"
    assert data["created_at"].endswith("Z")
    assert started == [data["id"]]
    assert db.get(Itinerary, data["id"]) is not None


def test_create_errors(client: TestClient, db: Session, started: list[int]) -> None:
    bare = make_trip(db, prefs=False)
    no_prefs = client.post(f"/api/trips/{bare.id}/itineraries", json=MANUAL)
    assert no_prefs.status_code == 400 and "preferences" in no_prefs.json()["detail"]
    missing = client.post("/api/trips/9999/itineraries", json=MANUAL)
    assert missing.status_code == 404 and "9999" in missing.json()["detail"]
    trip = make_trip(db)
    bad_offer = client.post(f"/api/trips/{trip.id}/itineraries", json={"offer_id": 12345})
    assert bad_offer.status_code == 400
    assert client.post(f"/api/trips/{trip.id}/itineraries", json={}).status_code == 422
    reversed_times = {"arrival_at": "2026-11-23T19:00", "departure_at": "2026-11-20T14:00"}
    assert client.post(f"/api/trips/{trip.id}/itineraries", json=reversed_times).status_code == 422
    assert started == []


# --------------------------------------------------------------------------- read / delete


def test_list_newest_first_and_get(client: TestClient, db: Session, started: list[int]) -> None:
    trip = make_trip(db)
    other = make_trip(db)
    first = make_done(db, trip)
    second = client.post(f"/api/trips/{trip.id}/itineraries", json=MANUAL).json()
    make_done(db, other)
    listed = client.get(f"/api/trips/{trip.id}/itineraries").json()
    assert [it["id"] for it in listed] == [second["id"], first.id]
    assert listed[1]["plan"]["title"] == "Tokyo in 4 days"
    assert client.get(f"/api/trips/{trip.id + 100}/itineraries").status_code == 404

    one = client.get(f"/api/itineraries/{first.id}")
    assert one.status_code == 200 and one.json()["status"] == "done"
    assert one.json()["plan"]["route_metrics"]["backtrack_hops"] == 0  # schema defaults applied
    assert client.get("/api/itineraries/9999").status_code == 404


def test_delete(client: TestClient, db: Session) -> None:
    trip = make_trip(db)
    parent = make_done(db, trip)
    child = make_done(db, trip, parent_id=parent.id, feedback="more food")
    response = client.delete(f"/api/itineraries/{parent.id}")
    assert response.status_code == 204 and response.content == b""
    assert client.get(f"/api/itineraries/{parent.id}").status_code == 404
    assert client.delete(f"/api/itineraries/{parent.id}").status_code == 404
    kept = client.get(f"/api/itineraries/{child.id}").json()
    assert kept["parent_id"] is None and kept["feedback"] == "more food"


# --------------------------------------------------------------------------- refine


def test_refine_creates_a_child_and_starts_it(client: TestClient, db: Session, started: list[int]) -> None:
    trip = make_trip(db)
    parent = make_done(db, trip, arrival_at=MANUAL["arrival_at"], departure_at=MANUAL["departure_at"])
    response = client.post(f"/api/itineraries/{parent.id}/refine", json={"feedback": "  less walking  "})
    assert response.status_code == 201
    child = response.json()
    assert child["status"] == "pending" and child["parent_id"] == parent.id
    assert child["feedback"] == "less walking" and child["arrival_at"] == MANUAL["arrival_at"]
    assert child["request"]["refine_of"] == parent.id
    assert started == [child["id"]]


def test_refine_errors(client: TestClient, db: Session, started: list[int]) -> None:
    trip = make_trip(db)
    pending = Itinerary(trip_id=trip.id, status="pending", request={})
    db.add(pending)
    db.commit()
    unfinished = client.post(f"/api/itineraries/{pending.id}/refine", json={"feedback": "more food"})
    assert unfinished.status_code == 400 and "finished" in unfinished.json()["detail"]
    assert client.post("/api/itineraries/9999/refine", json={"feedback": "x"}).status_code == 404
    done = make_done(db, trip)
    blank = client.post(f"/api/itineraries/{done.id}/refine", json={"feedback": "   "})
    assert blank.status_code == 422 and blank.json()["detail"][0]["loc"] == ["body", "feedback"]
    assert started == []


# --------------------------------------------------------------------------- background run


def test_generation_runs_in_a_background_thread(
    client: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real ``start_generation`` with the pipeline replaced; the UI-style poll sees ``done``."""
    release = threading.Event()

    def fake_generate(itinerary_id: int, *, client: Any = None) -> None:
        release.wait(5)
        with session_scope() as session:
            row = session.get(Itinerary, itinerary_id)
            row.status, row.plan, row.progress = "done", dict(PLAN), None

    monkeypatch.setattr(service, "generate_itinerary", fake_generate)
    trip = make_trip(db)
    created = client.post(f"/api/trips/{trip.id}/itineraries", json=MANUAL).json()
    assert created["status"] == "pending" and service.is_generating(created["id"])
    release.set()
    for thread in threading.enumerate():
        if thread.name == f"itinerary-{created['id']}":
            thread.join(5)
            assert not thread.is_alive()
    final = client.get(f"/api/itineraries/{created['id']}").json()
    assert final["status"] == "done" and final["plan"]["title"] == "Tokyo in 4 days"
    assert not service.is_generating(created["id"])
