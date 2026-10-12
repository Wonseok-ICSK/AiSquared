"""Airport autocomplete and scheduler job listing APIs."""

from __future__ import annotations

from fastapi.testclient import TestClient

from test_api_support import api, create_trip, quiet, sched  # noqa: F401  (fixtures)


def test_airport_search(api: TestClient) -> None:  # noqa: F811
    results = api.get("/api/airports", params={"q": "tokyo"}).json()
    codes = [airport["code"] for airport in results]
    assert codes[0] == "TYO" and results[0]["is_metro"] is True
    assert {"NRT", "HND"} <= set(codes)
    narita = next(airport for airport in results if airport["code"] == "NRT")
    assert narita["city_code"] == "TYO" and narita["country"]
    assert results[0]["city_ko"] == "도쿄" and narita["city_ko"] == "도쿄"
    assert api.get("/api/airports", params={"q": "DAD"}).json()[0]["city_ko"] == "다낭"
    assert isinstance(narita["lat"], float) and isinstance(narita["lon"], float)

    assert api.get("/api/airports", params={"q": "icn"}).json()[0]["code"] == "ICN"
    assert api.get("/api/airports").json() == []  # blank query
    assert api.get("/api/airports", params={"q": "  "}).json() == []
    assert len(api.get("/api/airports", params={"q": "a"}).json()) <= 20
    assert len(api.get("/api/airports", params={"q": "a", "limit": 3}).json()) == 3
    assert api.get("/api/airports", params={"q": "a", "limit": 50}).status_code == 422


def test_scheduler_jobs(api: TestClient) -> None:  # noqa: F811
    assert api.get("/api/scheduler/jobs").json() == []
    trip = create_trip(api, name="Tokyo", schedule_times=["09:00"])
    jobs = api.get("/api/scheduler/jobs").json()
    assert len(jobs) == 1
    job = jobs[0]
    assert job["job_id"] == f"trip-{trip['id']}" and job["trip_id"] == trip["id"]
    assert job["trip_name"] == "Tokyo" and job["next_run_at"].endswith("Z")
    assert job["trigger"].startswith("daily at 09:00")
