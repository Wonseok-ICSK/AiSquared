"""``/api/providers``: readiness list, background test searches and login sessions.

No browser is ever launched: the BrowserManager lookup is replaced by a sentinel, fake
providers are registered temporarily, and ``sessions.start_login`` is mocked.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any, ClassVar

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api import providers as providers_api
from app.browser import sessions
from app.models import ProviderSession
from app.providers.flights import registry
from app.providers.flights.base import (
    FlightLeg,
    FlightOffer,
    FlightProvider,
    FlightQuery,
    ProviderBlocked,
)
from app.schemas import ProviderTestOut
from app.timeutil import now_local, utcnow

BROWSER = object()


def make_offer(price: float, currency: str = "KRW") -> FlightOffer:
    leg = FlightLeg(
        departure_airport="ICN",
        arrival_airport="NRT",
        departure_time="2026-11-07T09:00",
        arrival_time="2026-11-07T11:30",
        duration_minutes=150,
        stops=0,
        airlines=["Korean Air"],
        flight_numbers=[f"KE{int(price) % 1000}"],
    )
    return FlightOffer(
        provider="fake",
        outbound=leg,
        inbound=None,
        price_total=price,
        price_per_person=price,
        currency=currency,
        cabin="economy",
        return_date="2026-11-11",
    )


class FakeProvider(FlightProvider):
    key = "fake"
    display_name = "Fake Flights"
    description = "Deterministic fake for API tests."
    uses_browser = True
    enabled_by_default = False
    queries: ClassVar[list[FlightQuery]] = []
    contexts: ClassVar[list[Any]] = []
    gate: ClassVar[threading.Event | None] = None
    error: ClassVar[Exception | None] = None

    def __init__(self, ctx: Any) -> None:
        super().__init__(ctx)
        FakeProvider.contexts.append(ctx)

    def search_flights(self, query: FlightQuery) -> list[FlightOffer]:
        FakeProvider.queries.append(query)
        if FakeProvider.gate is not None:
            FakeProvider.gate.wait(5)
        if FakeProvider.error is not None:
            raise FakeProvider.error
        return [make_offer(312_000), make_offer(298_500)]


class KeyedProvider(FlightProvider):
    key = "keyed"
    display_name = "Keyed API"
    requires_api_key = True
    api_key_name = "serpapi"

    def search_flights(self, query: FlightQuery) -> list[FlightOffer]:
        return []


class LoginProvider(FlightProvider):
    key = "airline"
    display_name = "Airline Direct"
    supports_login = True
    enabled_by_default = False
    login_url = "https://www.example-air.co.kr/login"
    login_cookies = ("SESSION",)

    def search_flights(self, query: FlightQuery) -> list[FlightOffer]:
        return []


class NoUrlLoginProvider(LoginProvider):
    key = "nourl"
    display_name = "No URL"
    login_url = None


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Fresh in-memory API state, a sentinel browser manager, fake providers registered,
    browser profiles under tmp (never the real %LOCALAPPDATA%)."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
    monkeypatch.setattr(providers_api, "_browser_manager", lambda: BROWSER)
    for name in ("_tests", "_test_threads", "_login_threads", "_login_results"):
        monkeypatch.setattr(providers_api, name, {})
    for cls in (FakeProvider, KeyedProvider, LoginProvider, NoUrlLoginProvider):
        monkeypatch.setitem(registry._registry._classes, cls.key, cls)
    FakeProvider.queries, FakeProvider.contexts = [], []
    FakeProvider.gate, FakeProvider.error = None, None
    yield
    if FakeProvider.gate is not None:
        FakeProvider.gate.set()
    for thread in [*providers_api._test_threads.values(), *providers_api._login_threads.values()]:
        thread.join(5)
        assert not thread.is_alive()


def by_key(client: TestClient) -> dict[str, dict[str, Any]]:
    response = client.get("/api/providers")
    assert response.status_code == 200
    return {p["key"]: p for p in response.json()}


def finish_test(key: str) -> None:
    providers_api._test_threads[key].join(5)
    assert not providers_api._test_threads[key].is_alive()


# --------------------------------------------------------------------------- list


def test_list_real_providers(client: TestClient) -> None:
    providers = by_key(client)
    assert {"google_flights", "naver", "serpapi", "mock"} <= set(providers)
    google, naver, serp, mock = (providers[k] for k in ("google_flights", "naver", "serpapi", "mock"))
    assert google["configured"] and google["enabled"] and google["enabled_by_default"]
    assert google["uses_browser"] and not google["requires_api_key"]
    assert "fetch mode" in google["message"]
    assert not naver["enabled"] and not naver["enabled_by_default"] and naver["uses_browser"]
    assert "terms of service" in naver["description"] and naver["configured"]
    assert serp["requires_api_key"] and not serp["configured"] and serp["enabled"]
    assert "SerpApi API key is not set" in serp["message"]
    assert not mock["enabled"] and mock["configured"]
    for info in providers.values():
        assert set(info) == {
            "key", "display_name", "description", "requires_api_key", "configured",
            "enabled_by_default", "enabled", "uses_browser", "supports_login",
            "session_status", "message",
        }
        assert info["session_status"] is None or info["supports_login"]


def test_list_reflects_settings(client: TestClient) -> None:
    client.put("/api/settings", json={"api_keys": {"serpapi": "key"}, "default_providers": ["naver"]})
    providers = by_key(client)
    assert providers["serpapi"]["configured"] and not providers["serpapi"]["enabled"]
    assert providers["naver"]["enabled"] and not providers["google_flights"]["enabled"]
    assert providers["keyed"]["configured"]  # same secret name


def test_list_passes_provider_options_and_the_browser(client: TestClient) -> None:
    client.put("/api/settings", json={"provider_options": {"fake": {"x": 1}}})
    client.get("/api/providers")
    ctx = FakeProvider.contexts[-1]
    assert ctx.options == {"x": 1} and ctx.browser is BROWSER
    assert by_key(client)["keyed"]["configured"] is False


def test_list_survives_a_broken_provider(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(self: FlightProvider) -> Any:
        raise RuntimeError("status exploded token=abc123")

    monkeypatch.setattr(FakeProvider, "status", boom)
    info = by_key(client)["fake"]
    assert info["configured"] is False and "could not be initialised" in info["message"]
    assert "abc123" not in info["message"]


def test_session_status_for_login_providers(client: TestClient, db: Session) -> None:
    assert by_key(client)["airline"]["session_status"] == "none"
    sessions.save_storage_state("airline", {"cookies": [], "origins": []})
    db.add(ProviderSession(provider="airline", status="active"))
    db.commit()
    assert by_key(client)["airline"]["session_status"] == "active"
    sessions.state_path("airline").unlink()
    assert by_key(client)["airline"]["session_status"] == "expired"


# --------------------------------------------------------------------------- test search


def test_provider_test_runs_in_background(client: TestClient) -> None:
    FakeProvider.gate = threading.Event()
    response = client.post("/api/providers/fake/test")
    assert response.status_code == 202
    started = response.json()
    assert started["status"] == "running" and started["key"] == "fake"
    assert started["started_at"].endswith("Z") and started["finished_at"] is None
    assert client.get("/api/providers/fake/test").json()["status"] == "running"
    again = client.post("/api/providers/fake/test").json()  # no second search
    assert again["started_at"] == started["started_at"]

    FakeProvider.gate.set()
    finish_test("fake")
    result = client.get("/api/providers/fake/test").json()
    assert result["status"] == "ok" and result["offers_found"] == 2
    assert result["min_price"] == 298_500 and result["currency"] == "KRW"
    assert result["finished_at"] is not None
    assert len(FakeProvider.queries) == 1
    query = FakeProvider.queries[0]
    depart = now_local().date() + timedelta(days=30)
    assert (query.origin, query.destination, query.adults) == ("ICN", "NRT", 1)
    assert (query.depart_date, query.return_date) == (depart, depart + timedelta(days=4))


def test_provider_test_can_be_rerun(client: TestClient) -> None:
    client.post("/api/providers/fake/test")
    finish_test("fake")
    second = client.post("/api/providers/fake/test").json()
    assert second["status"] == "running"
    finish_test("fake")
    assert len(FakeProvider.queries) == 2


def test_provider_test_reports_errors(client: TestClient) -> None:
    FakeProvider.error = ProviderBlocked("captcha page (consent.google.com)")
    client.post("/api/providers/fake/test")
    finish_test("fake")
    result = client.get("/api/providers/fake/test").json()
    assert result["status"] == "error" and result["message"].startswith("Blocked by the site")
    assert result["offers_found"] is None


def test_provider_test_crash_is_reported(client: TestClient) -> None:
    FakeProvider.error = RuntimeError("boom")
    client.post("/api/providers/fake/test")
    finish_test("fake")
    assert client.get("/api/providers/fake/test").json()["message"] == "RuntimeError: boom"


def test_provider_test_no_offers_is_ok_with_message(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(FakeProvider, "search_flights", lambda self, query: [])
    client.post("/api/providers/fake/test")
    finish_test("fake")
    result = client.get("/api/providers/fake/test").json()
    assert result["status"] == "ok" and result["offers_found"] == 0 and result["min_price"] is None
    assert "no offers" in result["message"]


def test_unconfigured_provider_test_fails_immediately(client: TestClient) -> None:
    response = client.post("/api/providers/keyed/test")
    assert response.status_code == 202
    result = response.json()
    assert result["status"] == "error" and result["message"].startswith("Not configured")
    assert "keyed" not in providers_api._test_threads
    assert client.get("/api/providers/keyed/test").json() == result


def test_provider_test_404s(client: TestClient) -> None:
    assert client.get("/api/providers/fake/test").status_code == 404
    assert client.post("/api/providers/nope/test").status_code == 404
    assert client.get("/api/providers/nope/test").status_code == 404


def test_stale_running_test_is_reported_as_timed_out(client: TestClient) -> None:
    providers_api._tests["fake"] = ProviderTestOut(
        key="fake", status="running", started_at=utcnow() - timedelta(hours=1)
    )
    result = client.get("/api/providers/fake/test").json()
    assert result["status"] == "error" and "did not finish" in result["message"]
    assert client.post("/api/providers/fake/test").json()["status"] == "running"  # restartable
    finish_test("fake")


# --------------------------------------------------------------------------- login sessions


@pytest.fixture
def fake_login(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    state: dict[str, Any] = {
        "gate": threading.Event(),
        "calls": [],
        "result": {"status": "active", "cookies": 3, "message": "saved"},
    }

    def start_login(key: str, url: str, **kwargs: Any) -> dict[str, Any]:
        state["calls"].append((key, url, kwargs))
        state["gate"].wait(5)
        return state["result"]

    monkeypatch.setattr(providers_api.sessions, "start_login", start_login)
    yield state
    state["gate"].set()


def test_login_starts_in_background(client: TestClient, fake_login: dict[str, Any]) -> None:
    response = client.post("/api/providers/airline/login")
    assert response.status_code == 202
    assert response.json()["ok"] is True and "Log in there" in response.json()["message"]
    assert "Login window open" in by_key(client)["airline"]["message"]
    assert "already open" in client.post("/api/providers/airline/login").json()["message"]
    conflict = client.delete("/api/providers/airline/session")
    assert conflict.status_code == 409

    fake_login["gate"].set()
    providers_api._login_threads["airline"].join(5)
    assert len(fake_login["calls"]) == 1
    key, url, kwargs = fake_login["calls"][0]
    assert (key, url) == ("airline", "https://www.example-air.co.kr/login")
    assert kwargs == {"cookie_domains": None, "login_cookies": ("SESSION",)}
    assert "Login window" not in by_key(client)["airline"]["message"]


def test_failed_login_is_shown_in_the_provider_message(
    client: TestClient, fake_login: dict[str, Any]
) -> None:
    fake_login["result"] = {"status": "none", "cookies": 0, "message": "No login cookies captured."}
    fake_login["gate"].set()
    client.post("/api/providers/airline/login")
    providers_api._login_threads["airline"].join(5)
    assert "Last login attempt: No login cookies captured." in by_key(client)["airline"]["message"]


def test_login_rejections(client: TestClient, fake_login: dict[str, Any]) -> None:
    assert client.post("/api/providers/nope/login").status_code == 404
    not_supported = client.post("/api/providers/fake/login")
    assert not_supported.status_code == 400 and "login" in not_supported.json()["detail"]
    assert client.post("/api/providers/nourl/login").status_code == 400
    assert fake_login["calls"] == []


def test_delete_session(client: TestClient, db: Session) -> None:
    sessions.save_storage_state("airline", {"cookies": [{"name": "SESSION"}], "origins": []})
    db.add(ProviderSession(provider="airline", status="active"))
    db.commit()
    assert by_key(client)["airline"]["session_status"] == "active"
    response = client.delete("/api/providers/airline/session")
    assert response.status_code == 200 and response.json()["ok"] is True
    assert not sessions.state_path("airline").exists()
    assert by_key(client)["airline"]["session_status"] == "none"
    assert client.delete("/api/providers/nope/session").status_code == 404


def test_delete_session_reports_partial_removal(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(providers_api.sessions, "delete_session", lambda db, key: False)
    response = client.delete("/api/providers/airline/session")
    assert response.status_code == 200
    assert response.json()["ok"] is False and "could not be deleted" in response.json()["message"]
