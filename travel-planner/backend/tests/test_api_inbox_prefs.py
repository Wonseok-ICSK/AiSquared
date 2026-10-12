"""Notifications inbox and travel preferences APIs."""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Notification
from app.schemas import DEFAULT_STYLE_SCORES, DEFAULT_STYLE_WEIGHTS
from app.timeutil import utcnow
from test_api_support import api, create_trip, quiet, sched  # noqa: F401  (fixtures)


def _notify(db: Session, title: str, trip_id: int | None = None, read: bool = False) -> int:
    row = Notification(
        trip_id=trip_id,
        kind="price_alert" if trip_id else "system",
        title=title,
        body=f"{title} body",
        url="https://example.test",
        data={"k": 1},
        channel_results={"inapp": "sent", "desktop": "skipped"},
        read_at=utcnow() if read else None,
    )
    db.add(row)
    db.commit()
    return row.id


def test_inbox(api: TestClient, db: Session) -> None:  # noqa: F811
    trip = create_trip(api)
    first = _notify(db, "first", trip["id"])
    second = _notify(db, "second")
    _notify(db, "old", read=True)

    inbox = api.get("/api/notifications").json()
    assert [n["title"] for n in inbox] == ["old", "second", "first"]  # newest first
    assert inbox[2]["trip_id"] == trip["id"] and inbox[2]["kind"] == "price_alert"
    assert inbox[2]["channel_results"] == {"inapp": "sent", "desktop": "skipped"}
    assert inbox[2]["data"] == {"k": 1}
    unread = api.get("/api/notifications", params={"unread_only": True}).json()
    assert [n["title"] for n in unread] == ["second", "first"]
    assert len(api.get("/api/notifications", params={"limit": 1}).json()) == 1
    assert api.get("/api/notifications/unread-count").json() == {"count": 2}

    read = api.post(f"/api/notifications/{first}/read")
    assert read.status_code == 200
    stamp = read.json()["read_at"]
    assert stamp is not None and stamp.endswith("Z")
    assert api.post(f"/api/notifications/{first}/read").json()["read_at"] == stamp  # idempotent
    assert api.get("/api/notifications/unread-count").json() == {"count": 1}
    assert api.post("/api/notifications/999999/read").status_code == 404

    done = api.post("/api/notifications/read-all")
    assert done.status_code == 200 and done.json()["ok"] is True
    assert done.json()["message"] == "1 notification marked as read"
    assert api.get("/api/notifications/unread-count").json() == {"count": 0}
    assert all(n["read_at"] for n in api.get("/api/notifications").json())
    assert db.get(Notification, second) is not None


PREFS = {
    "style_mode": "percent",
    "style_weights": {"food": 40, "culture": 30, "nature": 30},
    "companion": "couple",
    "pace": "relaxed",
    "food_wishlist": ["ramen", "sushi"],
    "restaurant_budget_min": 1000,
    "restaurant_budget_max": 5000,
    "restaurant_budget_currency": "jpy",
    "hotel_area": "Shinjuku",
    "must_visit": ["Senso-ji"],
    "budget_total": 2_000_000,
    "budget_flight": 600_000,
    "budget_food": -1,
}


def test_preferences_roundtrip(api: TestClient) -> None:  # noqa: F811
    trip = create_trip(api)
    url = f"/api/trips/{trip['id']}/preferences"
    missing = api.get(url)
    assert missing.status_code == 404  # the UI shows its defaults (api.getPreferences -> null)

    saved = api.put(url, json=PREFS)
    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert body["trip_id"] == trip["id"] and body["companion"] == "couple"
    assert body["style_weights"]["food"] == 40 and body["style_weights"]["shopping"] == 0
    assert body["restaurant_budget_currency"] == "JPY" and body["budget_currency"] == "KRW"
    assert body["budget_food"] == -1 and body["budget_hotel"] is None
    assert body["food_wishlist"] == ["ramen", "sushi"]
    assert api.get(url).json() == body

    # PUT replaces the whole set (omitted fields fall back to their defaults)
    replaced = api.put(
        url, json={"style_mode": "score", "budget_total": 0, "budget_flight": 0}
    ).json()
    assert replaced["style_mode"] == "score"
    assert replaced["style_weights"] == DEFAULT_STYLE_SCORES
    assert replaced["food_wishlist"] == [] and replaced["hotel_area"] is None
    assert replaced["created_at"] == body["created_at"]

    default_weights = api.put(url, json={"budget_total": 1, "budget_flight": 1}).json()
    assert default_weights["style_weights"] == DEFAULT_STYLE_WEIGHTS


def test_preferences_validation_and_404(api: TestClient) -> None:  # noqa: F811
    trip = create_trip(api)
    url = f"/api/trips/{trip['id']}/preferences"
    bad = api.put(url, json={**PREFS, "style_weights": {"food": 90, "culture": 20}})
    assert bad.status_code == 422
    assert api.put(url, json={**PREFS, "budget_total": -5}).status_code == 422
    assert api.put(url, json={"budget_flight": 1}).status_code == 422  # budget_total required
    assert api.get("/api/trips/999999/preferences").status_code == 404
    assert api.put("/api/trips/999999/preferences", json=PREFS).status_code == 404
