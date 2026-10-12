"""End-to-end through the REST API with the mock provider: create a trip, check it, read
offers / price history / offer history, see the alert and the inbox."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from test_api_support import api, create_trip, quiet, run_check, sched  # noqa: F401  (fixtures)

TARGET = 10_000_000.0  # far above any mock fare: the first check fires below_target


def test_monitoring_flow_end_to_end(api: TestClient) -> None:  # noqa: F811
    trip = create_trip(api, target_price=TARGET, name="Tokyo")
    trip_id = trip["id"]
    assert trip["latest_min_price"] is None and trip["lowest_ever"] is None
    assert trip["unread_alerts"] == 0
    assert trip["next_run_at"] is not None  # scheduled (times mode 08:30 / 17:30)

    rules = api.get(f"/api/trips/{trip_id}/alerts").json()
    assert sorted(rule["kind"] for rule in rules) == ["below_target", "new_low"]
    assert all(rule["armed"] and rule["enabled"] for rule in rules)

    first = run_check(api, trip_id)
    assert first["status"] == "ok", first
    assert first["offers_found"] > 0 and first["offers_matching"] > 0
    assert first["provider_stats"]["mock"]["status"] == "ok"
    assert first["best_offer_id"] is not None

    # offers of the latest finished run, cheapest first
    offers = api.get(f"/api/trips/{trip_id}/offers").json()
    assert len(offers) == first["offers_matching"]
    prices = [offer["price_total"] for offer in offers]
    assert prices == sorted(prices)
    assert offers[0]["id"] == first["best_offer_id"]
    assert offers[0]["price_total"] == first["min_price_total"]
    assert all(offer["run_id"] == first["id"] and offer["matches_filters"] for offer in offers)
    assert offers[0]["outbound"]["departure_airport"] == "ICN"
    assert offers[0]["inbound"] is not None  # the mock knows the return flight

    by_departure = api.get(
        f"/api/trips/{trip_id}/offers", params={"sort": "departure", "only_matching": "false"}
    ).json()
    times = [offer["departure_time"] for offer in by_departure]
    assert times == sorted(times) and len(by_departure) == first["offers_found"]
    by_duration = api.get(f"/api/trips/{trip_id}/offers", params={"sort": "duration"}).json()
    durations = [offer["duration_minutes"] for offer in by_duration]
    assert durations == sorted(durations)
    assert len(api.get(f"/api/trips/{trip_id}/offers", params={"limit": 2}).json()) == 2

    # the alert fired once (below target) -> one unread price alert
    rules = {rule["kind"]: rule for rule in api.get(f"/api/trips/{trip_id}/alerts").json()}
    assert rules["below_target"]["armed"] is False
    assert rules["below_target"]["last_triggered_price"] == first["min_price_per_person"]
    assert rules["new_low"]["last_triggered_at"] is None  # first run never a "new low"
    assert api.get("/api/notifications/unread-count").json() == {"count": 1}
    inbox = api.get("/api/notifications").json()
    assert len(inbox) == 1
    alert = inbox[0]
    assert alert["kind"] == "price_alert" and alert["trip_id"] == trip_id
    assert alert["alert_rule_id"] == rules["below_target"]["id"]
    assert alert["channel_results"]["inapp"] == "sent"
    assert alert["read_at"] is None

    trip = api.get(f"/api/trips/{trip_id}").json()
    assert trip["latest_min_price"] == first["min_price_total"]
    assert trip["latest_min_price_basis_value"] == first["min_price_per_person"]
    assert trip["lowest_ever"] == first["min_price_per_person"]
    assert trip["unread_alerts"] == 1
    assert trip["last_status"] == "ok" and trip["last_checked_at"] is not None

    # a second check: same fares (same day), nothing fires again
    second = run_check(api, trip_id)
    assert second["status"] == "ok" and second["id"] > first["id"]
    assert api.get("/api/notifications/unread-count").json() == {"count": 1}

    runs = api.get(f"/api/trips/{trip_id}/runs").json()
    assert [run["id"] for run in runs] == [second["id"], first["id"]]  # newest first
    assert len(api.get(f"/api/trips/{trip_id}/runs", params={"limit": 1}).json()) == 1

    history = api.get(f"/api/trips/{trip_id}/price-history").json()
    assert history["trip_id"] == trip_id and history["currency"] == "KRW"
    assert history["price_basis"] == "per_person"
    assert [point["run_id"] for point in history["points"]] == [first["id"], second["id"]]
    point = history["points"][0]
    assert point["min_total"] == first["min_price_total"]
    assert point["min_per_person"] == first["min_price_per_person"]
    assert point["by_provider"] == {"mock": first["min_price_per_person"]}
    assert point["checked_at"].endswith("Z")

    # the best offer was seen in both runs -> two observations of the same offer_key
    best_id = second["best_offer_id"]
    offer_history = api.get(f"/api/trips/{trip_id}/offers/{best_id}/history").json()
    assert len(offer_history["points"]) == 2
    assert {p["run_id"] for p in offer_history["points"]} == {first["id"], second["id"]}
    assert offer_history["lowest_per_person"] == min(
        p["price_per_person"] for p in offer_history["points"]
    )
    assert offer_history["currency"] == "KRW" and "|" in offer_history["offer_key"]

    # older runs are still addressable
    old_offers = api.get(f"/api/trips/{trip_id}/offers", params={"run_id": first["id"]}).json()
    assert {offer["run_id"] for offer in old_offers} == {first["id"]}

    # inbox actions
    marked = api.post(f"/api/notifications/{alert['id']}/read").json()
    assert marked["read_at"] is not None
    assert api.get("/api/notifications/unread-count").json() == {"count": 0}
    assert api.get("/api/notifications", params={"unread_only": "true"}).json() == []
    assert api.get(f"/api/trips/{trip_id}").json()["unread_alerts"] == 0


def test_check_with_no_usable_provider_finishes_as_error(api: TestClient) -> None:  # noqa: F811
    trip = create_trip(api, providers=["no-such-provider"])
    run = run_check(api, trip["id"])
    assert run["status"] == "error"
    assert api.get(f"/api/trips/{trip['id']}/offers").json() == []  # no finished run
    history: dict[str, Any] = api.get(f"/api/trips/{trip['id']}/price-history").json()
    assert history["points"] == []
    assert api.get(f"/api/trips/{trip['id']}").json()["last_status"] == "error"


def test_run_and_offer_lookups_404(api: TestClient) -> None:  # noqa: F811
    trip = create_trip(api)
    other = create_trip(api, destination="KIX")
    run = run_check(api, other["id"])
    offer_id = run["best_offer_id"]
    assert api.get("/api/runs/999999").status_code == 404
    assert api.get("/api/trips/999999/runs").status_code == 404
    assert api.post("/api/trips/999999/check").status_code == 404
    assert api.get("/api/trips/999999/offers").status_code == 404
    assert api.get("/api/trips/999999/price-history").status_code == 404
    # a run / offer of another trip is not found under this trip
    response = api.get(f"/api/trips/{trip['id']}/offers", params={"run_id": run["id"]})
    assert response.status_code == 404 and response.json() == {"detail": "Run not found"}
    response = api.get(f"/api/trips/{trip['id']}/offers/{offer_id}/history")
    assert response.status_code == 404 and response.json() == {"detail": "Offer not found"}
    assert api.get(f"/api/trips/{trip['id']}/offers", params={"sort": "nope"}).status_code == 422
