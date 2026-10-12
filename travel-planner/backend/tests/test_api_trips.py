"""Trips API: CRUD, default alert rules, merge validation, schedule sync, computed fields."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    AlertRule,
    FlightOffer,
    Itinerary,
    Notification,
    SearchRun,
    TravelPreference,
    Trip,
)
from app.services import monitoring
from app.services.scheduler import JOB_JITTER_S, get_scheduler
from app.timeutil import utcnow
from test_api_support import (  # noqa: F401  (fixtures)
    api,
    create_trip,
    future_dates,
    quiet,
    run_check,
    sched,
    trip_body,
)


def same_slot(a: str | None, b: str | None) -> bool:
    """Equal next-run times up to the scheduler's jitter (a never-started scheduler computes
    pending jobs' next run on every call, each with fresh jitter)."""
    assert a is not None and b is not None
    parse = lambda value: datetime.fromisoformat(value.replace("Z", "+00:00"))  # noqa: E731
    return abs((parse(a) - parse(b)).total_seconds()) <= JOB_JITTER_S


def rules_of(api: TestClient, trip_id: int) -> dict[str, dict]:
    return {rule["kind"]: rule for rule in api.get(f"/api/trips/{trip_id}/alerts").json()}


# --------------------------------------------------------------------------- create / read


def test_create_trip_defaults_and_rules(api: TestClient) -> None:  # noqa: F811
    trip = create_trip(api, target_price=250_000)
    assert trip["name"] == "ICN → NRT"
    assert trip["currency"] == "KRW" and trip["price_basis"] == "per_person"
    assert trip["schedule_mode"] == "times" and trip["schedule_times"] == ["08:30", "17:30"]
    assert trip["providers"] == ["mock"] and trip["active"] is True
    assert trip["created_at"].endswith("Z")
    rules = rules_of(api, trip["id"])
    assert set(rules) == {"below_target", "new_low"}
    assert rules["below_target"]["threshold"] is None  # follows trip.target_price
    assert rules["below_target"]["price_basis"] == "per_person"


def test_create_trip_without_target_gets_only_new_low(api: TestClient) -> None:  # noqa: F811
    trip = create_trip(api, price_basis="total")
    rules = rules_of(api, trip["id"])
    assert set(rules) == {"new_low"} and rules["new_low"]["price_basis"] == "total"


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"return_date": "2020-01-01"}, ""),  # return before departure (model-level rule)
        ({"origin": "SEL", "destination": "ICN"}, ""),  # same metro
        ({"adults": 3}, "adults"),
        ({"schedule_times": ["25:00"]}, "schedule_times.0"),
    ],
)
def test_create_trip_validation(api: TestClient, overrides: dict, field: str) -> None:  # noqa: F811
    response = api.post("/api/trips", json=trip_body(**overrides))
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert isinstance(detail, list) and detail[0]["loc"][0] == "body"
    if field:
        assert ".".join(map(str, detail[0]["loc"][1:])) == field


def test_get_and_list_trips(api: TestClient) -> None:  # noqa: F811
    later, _ = future_dates(80)
    late = create_trip(api, name="Late", depart_date=later.isoformat(),
                       return_date=(later + timedelta(days=3)).isoformat())
    soon = create_trip(api, name="Soon")
    paused = create_trip(api, name="Paused", active=False)
    assert api.get(f"/api/trips/{soon['id']}").json()["name"] == "Soon"
    names = [trip["name"] for trip in api.get("/api/trips").json()]
    assert names == ["Soon", "Late", "Paused"]  # active first, then by departure
    assert paused["next_run_at"] is None and late["next_run_at"] is not None
    assert api.get("/api/trips/999999").status_code == 404
    assert api.get("/api/trips/999999").json() == {"detail": "Trip not found"}


# --------------------------------------------------------------------------- update


def test_update_trip_partial_and_auto_name(api: TestClient) -> None:  # noqa: F811
    trip = create_trip(api)
    response = api.put(f"/api/trips/{trip['id']}", json={"destination": "KIX", "max_price": 300000})
    assert response.status_code == 200, response.text
    updated = response.json()
    assert updated["destination"] == "KIX" and updated["name"] == "ICN → KIX"  # regenerated
    assert updated["max_price"] == 300000 and updated["return_date"] == trip["return_date"]

    custom = api.put(f"/api/trips/{trip['id']}", json={"name": "Osaka"}).json()
    renamed = api.put(f"/api/trips/{trip['id']}", json={"destination": "FUK"}).json()
    assert custom["name"] == "Osaka" and renamed["name"] == "Osaka"  # custom names stay

    one_way = api.put(f"/api/trips/{trip['id']}", json={"trip_type": "one_way"}).json()
    assert one_way["return_date"] is None and one_way["flex_return_days"] == 0

    cleared = api.put(f"/api/trips/{trip['id']}", json={"max_price": None}).json()
    assert cleared["max_price"] is None  # explicit null clears


def test_update_trip_merged_validation_is_422(api: TestClient, db: Session) -> None:  # noqa: F811
    trip = create_trip(api)
    response = api.put(f"/api/trips/{trip['id']}", json={"return_date": "2020-01-01"})
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail[0]["loc"] == ["body"] and "return_date" in detail[0]["msg"]
    # nothing was changed
    assert api.get(f"/api/trips/{trip['id']}").json()["return_date"] == trip["return_date"]
    bad = api.put(f"/api/trips/{trip['id']}", json={"origin": "TYO"})  # NRT is in TYO
    assert bad.status_code == 422
    assert api.put("/api/trips/999999", json={"name": "x"}).status_code == 404


def test_setting_a_target_adds_the_below_target_rule_once(api: TestClient) -> None:  # noqa: F811
    trip = create_trip(api)
    assert set(rules_of(api, trip["id"])) == {"new_low"}
    api.put(f"/api/trips/{trip['id']}", json={"target_price": 200000})
    rules = rules_of(api, trip["id"])
    assert set(rules) == {"below_target", "new_low"} and rules["below_target"]["threshold"] is None
    # deleting it and changing the target again does not resurrect it
    api.delete(f"/api/alerts/{rules['below_target']['id']}")
    api.put(f"/api/trips/{trip['id']}", json={"target_price": 180000})
    assert set(rules_of(api, trip["id"])) == {"new_low"}


def test_update_resyncs_schedule(api: TestClient) -> None:  # noqa: F811
    trip = create_trip(api)
    jobs = api.get("/api/scheduler/jobs").json()
    entry = next(job for job in jobs if job["trip_id"] == trip["id"])
    assert entry["trigger"].startswith("daily at 08:30, 17:30")
    assert same_slot(entry["next_run_at"], trip["next_run_at"])
    assert entry["trip_name"] == trip["name"]

    updated = api.put(
        f"/api/trips/{trip['id']}",
        json={"schedule_mode": "interval", "schedule_interval_hours": 6},
    ).json()
    jobs = api.get("/api/scheduler/jobs").json()
    entry = next(job for job in jobs if job["trip_id"] == trip["id"])
    assert entry["trigger"] == "every 6 hours"
    assert same_slot(updated["next_run_at"], entry["next_run_at"])

    paused = api.put(f"/api/trips/{trip['id']}", json={"active": False}).json()
    assert paused["next_run_at"] is None
    assert all(job["trip_id"] != trip["id"] for job in api.get("/api/scheduler/jobs").json())
    assert get_scheduler().next_run_for_trip(trip["id"]) is None


# --------------------------------------------------------------------------- delete


def test_delete_trip_removes_children_and_jobs(
    api: TestClient, db: Session  # noqa: F811
) -> None:
    trip = create_trip(api, target_price=1)
    trip_id = trip["id"]
    db.add(Notification(trip_id=trip_id, kind="system", title="t", body="b"))
    db.commit()
    assert get_scheduler().next_run_for_trip(trip_id) is not None
    response = api.delete(f"/api/trips/{trip_id}")
    assert response.status_code == 204 and response.content == b""
    assert api.get(f"/api/trips/{trip_id}").status_code == 404
    assert get_scheduler().next_run_for_trip(trip_id) is None
    db.expire_all()
    assert db.scalar(select(func.count(AlertRule.id)).where(AlertRule.trip_id == trip_id)) == 0
    assert db.scalar(select(func.count(Notification.id))) == 0
    assert api.delete(f"/api/trips/{trip_id}").status_code == 404


def test_delete_checked_trip_with_itineraries(api: TestClient, db: Session) -> None:  # noqa: F811
    trip = create_trip(api, target_price=10_000_000)
    run = run_check(api, trip["id"])
    assert run["status"] == "ok" and run["best_offer_id"]
    api.put(f"/api/trips/{trip['id']}/preferences", json={"budget_total": 1, "budget_flight": 1})
    parent = Itinerary(trip_id=trip["id"], status="done", offer_id=run["best_offer_id"])
    db.add(parent)
    db.flush()
    db.add(Itinerary(trip_id=trip["id"], status="done", parent_id=parent.id,
                     offer_id=run["best_offer_id"]))
    db.commit()
    assert api.delete(f"/api/trips/{trip['id']}").status_code == 204
    db.expire_all()
    for model in (SearchRun, FlightOffer, Itinerary, Notification, TravelPreference, AlertRule):
        assert db.scalar(select(func.count()).select_from(model)) == 0, model.__name__


# --------------------------------------------------------------------------- computed fields


def _run(db: Session, trip_id: int, status: str, total: float | None, currency: str = "KRW",
         adults: int = 2) -> SearchRun:
    run = SearchRun(
        trip_id=trip_id,
        trigger="schedule",
        status=status,
        currency=currency,
        finished_at=utcnow(),
        min_price_total=total,
        min_price_per_person=total / adults if total is not None else None,
    )
    db.add(run)
    db.commit()
    return run


def test_trip_out_computed_fields(api: TestClient, db: Session) -> None:  # noqa: F811
    trip = create_trip(api, adults=2, price_basis="total")
    trip_id = trip["id"]
    other = create_trip(api, destination="KIX")
    _run(db, trip_id, "ok", 500_000)
    _run(db, trip_id, "ok", 300_000, currency="JPY")  # other currency: never mixed in
    _run(db, trip_id, "partial", 420_000)  # latest finished run
    _run(db, trip_id, "error", None)  # failed runs are not "finished"
    db.add_all(
        [
            Notification(trip_id=trip_id, kind="price_alert", title="a", body=""),
            Notification(trip_id=trip_id, kind="system", title="b", body=""),
            Notification(trip_id=trip_id, kind="price_alert", title="c", body="",
                         read_at=utcnow()),
            Notification(trip_id=other["id"], kind="price_alert", title="d", body=""),
        ]
    )
    db.commit()

    out = api.get(f"/api/trips/{trip_id}").json()
    assert out["latest_min_price"] == 420_000
    assert out["latest_min_price_basis_value"] == 420_000  # basis total
    assert out["lowest_ever"] == 420_000
    assert out["unread_alerts"] == 2

    per_person = api.put(f"/api/trips/{trip_id}", json={"price_basis": "per_person"}).json()
    assert per_person["latest_min_price"] == 420_000
    assert per_person["latest_min_price_basis_value"] == 210_000
    assert per_person["lowest_ever"] == 210_000

    listed = {t["id"]: t for t in api.get("/api/trips").json()}
    assert listed[trip_id]["unread_alerts"] == 2 and listed[other["id"]]["unread_alerts"] == 1
    assert listed[other["id"]]["latest_min_price"] is None

    # the latest finished run in another currency hides the "latest" price
    _run(db, trip_id, "ok", 1_000, currency="JPY")
    out = api.get(f"/api/trips/{trip_id}").json()
    assert out["latest_min_price"] is None and out["lowest_ever"] == 210_000


# --------------------------------------------------------------------------- check


def test_check_endpoint_errors(
    api: TestClient, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    trip = create_trip(api)

    def busy(trip_id: int, trigger: str = "manual") -> int:
        raise monitoring.RunInProgressError("a check for this trip is starting; try again")

    monkeypatch.setattr(monitoring, "start_trip_check", busy)
    response = api.post(f"/api/trips/{trip['id']}/check")
    assert response.status_code == 409 and "try again" in response.json()["detail"]

    def gone(trip_id: int, trigger: str = "manual") -> int:
        raise monitoring.TripNotFoundError("gone")

    monkeypatch.setattr(monitoring, "start_trip_check", gone)
    assert api.post(f"/api/trips/{trip['id']}/check").status_code == 404


def test_check_returns_running_run(
    api: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    trip = create_trip(api)
    calls: list[tuple[int, str]] = []

    def start(trip_id: int, trigger: str = "manual") -> int:
        calls.append((trip_id, trigger))
        run = SearchRun(trip_id=trip_id, trigger=trigger, status="running", currency="KRW")
        db.add(run)
        db.commit()
        return run.id

    monkeypatch.setattr(monitoring, "start_trip_check", start)
    response = api.post(f"/api/trips/{trip['id']}/check")
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "running" and body["trigger"] == "manual"
    assert body["finished_at"] is None and body["provider_stats"] == {}
    assert calls == [(trip["id"], "manual")]
    assert db.get(Trip, trip["id"]) is not None
