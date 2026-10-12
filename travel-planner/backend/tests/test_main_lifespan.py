"""App lifespan wiring: recovery hooks, browser configuration, scheduler start/stop.

No real scheduler thread or browser is ever started here: the scheduler singleton is a fake
or a disabled ``MonitorScheduler``, and ``BrowserManager`` only launches a browser for a job.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app import main as main_module
from app.browser import manager as browser_manager
from app.config import get_settings
from app.models import Itinerary, SearchRun, Trip
from app.services import monitoring
from app.services import scheduler as scheduler_module
from app.services.itinerary import service as itinerary_service
from app.services.scheduler import MonitorScheduler
from app.settings_store import save_runtime_settings
from monitoring_fakes import add_trip


class FakeScheduler:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def start(self) -> bool:
        self.calls.append("scheduler.start")
        return True

    def shutdown(self) -> None:
        self.calls.append("scheduler.shutdown")


class FakeBrowserManager:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def configure(self, channel: str | None = None, headless: bool | None = None) -> None:
        self.calls.append(f"browser.configure({channel}, {headless})")


@pytest.fixture
def make_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(main_module, "configure_logging", lambda _settings: None)

    def make() -> TestClient:
        return TestClient(main_module.create_app(frontend_dist=tmp_path / "no-frontend"))

    return make


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every background hook instead of running it."""
    recorded: list[str] = []
    monkeypatch.setattr(scheduler_module, "_instance", FakeScheduler(recorded))
    monkeypatch.setattr(
        monitoring, "recover_stale_runs", lambda: recorded.append("recover_runs") or 0
    )
    monkeypatch.setattr(
        itinerary_service,
        "recover_stale_itineraries",
        lambda: recorded.append("recover_itineraries") or 0,
    )
    fake_manager = FakeBrowserManager(recorded)
    monkeypatch.setattr(browser_manager, "get_browser_manager", lambda: fake_manager)
    monkeypatch.setattr(
        browser_manager,
        "shutdown_browser_manager",
        lambda wait=True, timeout=0: recorded.append(f"browser.shutdown(wait={wait})"),
    )
    return recorded


def test_lifespan_runs_hooks_in_order(make_client: Any, calls: list[str], db: Session) -> None:
    save_runtime_settings(db, {"browser_channel": "chrome", "browser_headless": False})
    with make_client() as client:
        assert client.get("/api/health").status_code == 200
        assert calls == [
            "recover_runs",
            "recover_itineraries",
            "browser.configure(chrome, False)",
            "scheduler.start",
        ]
    assert calls[4:] == ["scheduler.shutdown", "browser.shutdown(wait=True)"]


def test_failing_hook_does_not_stop_startup(
    make_client: Any, calls: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom() -> int:
        raise RuntimeError("db locked")

    monkeypatch.setattr(monitoring, "recover_stale_runs", boom)

    def broken_shutdown() -> None:
        raise RuntimeError("scheduler wedged")

    monkeypatch.setattr(scheduler_module._instance, "shutdown", broken_shutdown)
    with make_client() as client:
        assert client.get("/api/health").json()["status"] == "ok"
    assert "scheduler.start" in calls and "recover_itineraries" in calls
    assert calls[-1] == "browser.shutdown(wait=True)"  # still runs after a failed step


def test_real_hooks_recover_stale_work_without_threads(
    make_client: Any, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    get_settings.cache_clear()
    disabled = MonitorScheduler(get_settings())
    monkeypatch.setattr(scheduler_module, "_instance", disabled)
    monkeypatch.setattr(browser_manager, "_manager", None)

    trip: Trip = add_trip(db)
    run = SearchRun(trip_id=trip.id, trigger="schedule", status="running", currency="KRW")
    itinerary = Itinerary(trip_id=trip.id, status="running", request={"language": "en"})
    db.add_all([run, itinerary])
    db.commit()
    save_runtime_settings(db, {"browser_channel": "chrome", "browser_headless": False})

    with make_client():
        db.expire_all()
        assert db.get(SearchRun, run.id).status == "error"
        assert db.get(SearchRun, run.id).error == monitoring.INTERRUPTED
        assert db.get(Itinerary, itinerary.id).status == "error"
        assert disabled.running is False  # SCHEDULER_ENABLED=false: no thread
        manager = browser_manager._manager
        assert manager is not None and manager.channel == "chrome"
        assert manager.headless is False
    assert browser_manager._manager is None  # shut down and forgotten
    assert manager.is_shut_down
