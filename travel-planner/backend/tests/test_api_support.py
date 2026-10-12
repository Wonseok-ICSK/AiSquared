"""Shared fixtures/helpers for the ``test_api_*`` modules (this module holds no tests).

* ``sched``: an *enabled* :class:`MonitorScheduler` installed as the process singleton whose
  ``start`` is a no-op, so the app lifespan never starts the APScheduler thread while
  ``sync_trip`` still creates (pending) jobs with computable next run times.
* ``quiet``: desktop toasts off (runtime setting + ``_show_toast`` stubbed); every other
  external channel is disabled by default.
* ``api``: the conftest ``client`` with both of the above in place before the lifespan runs.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from datetime import date, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from app.config import Settings
from app.db import session_scope
from app.services import monitoring
from app.services import scheduler as scheduler_module
from app.services.notifications import desktop
from app.services.scheduler import MonitorScheduler
from app.settings_store import save_runtime_settings
from app.timeutil import now_local


@pytest.fixture
def sched(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> MonitorScheduler:
    """Enabled scheduler singleton that is never started (no background thread)."""
    instance = MonitorScheduler(settings)
    monkeypatch.setattr(instance, "start", lambda: False)
    monkeypatch.setattr(scheduler_module, "_instance", instance)
    return instance


@pytest.fixture
def quiet(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> Iterator[list[Any]]:
    """No real Windows toasts; yields the list of toasts that would have been shown."""
    shown: list[Any] = []
    monkeypatch.setattr(desktop, "_show_toast", shown.append)
    with session_scope() as db:
        save_runtime_settings(db, {"notifications": {"desktop": {"enabled": False}}})
    yield shown


@pytest.fixture
def api(sched: MonitorScheduler, quiet: list[Any], client: TestClient) -> TestClient:
    """The app's TestClient with the scheduler stubbed and notifications quiet."""
    return client


def future_dates(offset_days: int = 40, length_days: int = 5) -> tuple[date, date]:
    """``(depart, return)`` safely in the future (searches skip past departure dates)."""
    depart = now_local().date() + timedelta(days=offset_days)
    return depart, depart + timedelta(days=length_days)


def trip_body(**overrides: Any) -> dict[str, Any]:
    """A valid ``TripCreate`` JSON body (ICN -> NRT round trip, mock provider)."""
    depart, ret = future_dates()
    body: dict[str, Any] = {
        "origin": "ICN",
        "destination": "NRT",
        "depart_date": depart.isoformat(),
        "return_date": ret.isoformat(),
        "adults": 1,
        "providers": ["mock"],
    }
    body.update(overrides)
    return body


def create_trip(api: TestClient, **overrides: Any) -> dict[str, Any]:
    """POST a trip and return its JSON (asserts 201)."""
    response = api.post("/api/trips", json=trip_body(**overrides))
    assert response.status_code == 201, response.text
    return response.json()


def wait_for_run(api: TestClient, run_id: int, timeout: float = 20.0) -> dict[str, Any]:
    """Poll ``GET /api/runs/{id}`` until the run is no longer running."""
    deadline = time.monotonic() + timeout
    while True:
        run = api.get(f"/api/runs/{run_id}").json()
        if run["status"] != "running":
            return run
        if time.monotonic() > deadline:
            raise AssertionError(f"run {run_id} still running after {timeout} s")
        time.sleep(0.05)


def wait_idle(trip_id: int, timeout: float = 20.0) -> None:
    """Wait until the trip's check thread is done (alerts and notifications included)."""
    deadline = time.monotonic() + timeout
    while monitoring.is_trip_running(trip_id):
        if time.monotonic() > deadline:
            raise AssertionError(f"check of trip {trip_id} still running after {timeout} s")
        time.sleep(0.05)


def run_check(api: TestClient, trip_id: int) -> dict[str, Any]:
    """Start a manual check and wait for it (and its alert phase) to finish."""
    response = api.post(f"/api/trips/{trip_id}/check")
    assert response.status_code == 202, response.text
    started = response.json()
    assert started["trip_id"] == trip_id and started["trigger"] == "manual"
    wait_idle(trip_id)
    return wait_for_run(api, started["id"])
