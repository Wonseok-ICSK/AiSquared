"""Alert rules API: list/create/update/delete, merge validation and re-arming."""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import AlertRule
from test_api_support import api, create_trip, quiet, sched  # noqa: F401  (fixtures)


def test_alert_crud(api: TestClient) -> None:  # noqa: F811
    trip = create_trip(api)
    response = api.post(
        f"/api/trips/{trip['id']}/alerts",
        json={"kind": "price_drop", "drop_percent": 10, "price_basis": "total"},
    )
    assert response.status_code == 201, response.text
    rule = response.json()
    assert rule["trip_id"] == trip["id"] and rule["armed"] is True and rule["enabled"] is True
    assert rule["drop_percent"] == 10 and rule["price_basis"] == "total"
    assert rule["last_triggered_at"] is None and rule["created_at"].endswith("Z")

    kinds = [r["kind"] for r in api.get(f"/api/trips/{trip['id']}/alerts").json()]
    assert kinds == ["new_low", "price_drop"]  # oldest first

    updated = api.put(f"/api/alerts/{rule['id']}", json={"drop_percent": 15}).json()
    assert updated["drop_percent"] == 15 and updated["kind"] == "price_drop"

    assert api.delete(f"/api/alerts/{rule['id']}").status_code == 204
    kinds = [r["kind"] for r in api.get(f"/api/trips/{trip['id']}/alerts").json()]
    assert kinds == ["new_low"]


def test_alert_validation_and_404(api: TestClient) -> None:  # noqa: F811
    trip = create_trip(api)
    bad = api.post(f"/api/trips/{trip['id']}/alerts", json={"kind": "price_drop"})
    assert bad.status_code == 422
    assert api.post("/api/trips/999999/alerts", json={"kind": "new_low"}).status_code == 404
    assert api.get("/api/trips/999999/alerts").status_code == 404
    assert api.put("/api/alerts/999999", json={"enabled": False}).status_code == 404
    assert api.delete("/api/alerts/999999").status_code == 404

    rule = api.post(f"/api/trips/{trip['id']}/alerts", json={"kind": "below_target"}).json()
    # switching to price_drop without a drop percent: the merged rule is invalid
    response = api.put(f"/api/alerts/{rule['id']}", json={"kind": "price_drop"})
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail[0]["loc"] == ["body"] and "drop_percent" in detail[0]["msg"]
    unchanged = api.get(f"/api/trips/{trip['id']}/alerts").json()
    assert any(r["id"] == rule["id"] and r["kind"] == "below_target" for r in unchanged)


def _disarm(db: Session, rule_id: int, enabled: bool = True) -> None:
    rule = db.get(AlertRule, rule_id)
    assert rule is not None
    rule.armed, rule.enabled, rule.last_triggered_price = False, enabled, 100.0
    db.commit()


def test_rearming_rules(api: TestClient, db: Session) -> None:  # noqa: F811
    trip = create_trip(api, target_price=200_000)
    rule = next(
        r for r in api.get(f"/api/trips/{trip['id']}/alerts").json() if r["kind"] == "below_target"
    )
    rule_id = rule["id"]

    # re-saving an enabled rule unchanged keeps it disarmed (no duplicate alert)
    _disarm(db, rule_id)
    same = api.put(
        f"/api/alerts/{rule_id}",
        json={"kind": "below_target", "enabled": True, "threshold": None,
              "drop_percent": None, "price_basis": "per_person"},
    ).json()
    assert same["armed"] is False

    # a changed condition re-arms
    assert api.put(f"/api/alerts/{rule_id}", json={"threshold": 150_000}).json()["armed"] is True

    # switching a disabled rule back on re-arms
    _disarm(db, rule_id, enabled=False)
    assert api.put(f"/api/alerts/{rule_id}", json={"enabled": True}).json()["armed"] is True

    # disabling keeps the state; an explicit armed always wins
    _disarm(db, rule_id)
    off = api.put(f"/api/alerts/{rule_id}", json={"enabled": False}).json()
    assert off["enabled"] is False and off["armed"] is False
    on = api.put(f"/api/alerts/{rule_id}", json={"enabled": True, "armed": False}).json()
    assert on["enabled"] is True and on["armed"] is False
    assert api.put(f"/api/alerts/{rule_id}", json={"armed": True}).json()["armed"] is True
