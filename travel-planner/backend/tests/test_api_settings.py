"""``/api/settings``: redacted GET, partial PUT (secret semantics, 422s, browser reconfigure),
notification channel tests and Telegram chat detection (no network: dispatcher mocked, respx)."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api import settings as settings_api
from app.config import get_settings
from app.models import AppSetting, Notification
from app.services.notifications import dispatcher
from app.settings_store import get_secret, load_runtime_settings

TELEGRAM_UPDATES = r"https://api\.telegram\.org/bot.+/getUpdates"


class FakeBrowserManager:
    def __init__(self) -> None:
        self.calls: list[tuple[str | None, bool | None]] = []

    def configure(self, channel: str | None = None, headless: bool | None = None) -> None:
        self.calls.append((channel, headless))


@pytest.fixture
def browser(monkeypatch: pytest.MonkeyPatch) -> FakeBrowserManager:
    fake = FakeBrowserManager()
    monkeypatch.setattr(settings_api, "get_browser_manager", lambda: fake)
    return fake


# --------------------------------------------------------------------------- GET / PUT


def test_language_defaults_to_korean(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("APP_LANGUAGE")  # conftest pins English for the text assertions
    get_settings.cache_clear()
    assert load_runtime_settings(db).language == "ko"


def test_get_settings_defaults_redacts_secrets(client: TestClient) -> None:
    data = client.get("/api/settings").json()
    assert data["browser_channel"] == "msedge" and data["browser_headless"] is True
    assert data["llm_model"] == "claude-opus-5-5" and data["language"] == "en"
    assert data["default_providers"] == [] and data["provider_options"] == {}
    assert data["api_keys"]["serpapi"] == {"is_set": False, "source": None}
    assert data["notifications"]["telegram"]["bot_token"] == {"is_set": False, "source": None}
    assert data["notifications"]["ntfy"]["server"] == "https://ntfy.sh"


def test_put_secret_semantics(client: TestClient, db: Session, browser: FakeBrowserManager) -> None:
    secret = "serp-SECRET-123"
    response = client.put("/api/settings", json={"api_keys": {"serpapi": secret}})
    assert response.status_code == 200
    assert response.json()["api_keys"]["serpapi"] == {"is_set": True, "source": "ui"}
    assert secret not in response.text and secret not in client.get("/api/settings").text
    stored = db.get(AppSetting, "runtime")
    assert stored is not None and secret not in str(stored.value)  # encrypted at rest
    assert get_secret("serpapi", db) == secret

    # omitted and null both leave the secret unchanged
    client.put("/api/settings", json={"language": "ko"})
    client.put("/api/settings", json={"api_keys": {"serpapi": None}})
    data = client.get("/api/settings").json()
    assert data["api_keys"]["serpapi"]["is_set"] is True and data["language"] == "ko"

    # "" clears the UI value
    cleared = client.put("/api/settings", json={"api_keys": {"serpapi": ""}}).json()
    assert cleared["api_keys"]["serpapi"] == {"is_set": False, "source": None}
    db.expire_all()
    assert get_secret("serpapi", db) is None
    assert browser.calls == []  # browser settings untouched


def test_cleared_secret_falls_back_to_env(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SERPAPI_API_KEY", "from-env")
    get_settings.cache_clear()
    assert client.get("/api/settings").json()["api_keys"]["serpapi"] == {
        "is_set": True,
        "source": "env",
    }
    client.put("/api/settings", json={"api_keys": {"serpapi": "from-ui"}})
    assert client.get("/api/settings").json()["api_keys"]["serpapi"]["source"] == "ui"
    client.put("/api/settings", json={"api_keys": {"serpapi": ""}})
    assert client.get("/api/settings").json()["api_keys"]["serpapi"]["source"] == "env"


def test_put_partial_and_null_resets_to_env(client: TestClient, browser: FakeBrowserManager) -> None:
    body = {
        "llm_model": "claude-sonnet-x",
        "max_search_combinations": 20,
        "notifications": {"telegram": {"chat_id": "42"}, "email": {"smtp_port": 465}},
        "provider_options": {"google_flights": {"fetch_mode": "http"}},
    }
    data = client.put("/api/settings", json=body).json()
    assert data["llm_model"] == "claude-sonnet-x" and data["max_search_combinations"] == 20
    assert data["notifications"]["telegram"]["chat_id"] == "42"
    assert data["notifications"]["email"]["smtp_port"] == 465
    assert data["provider_options"] == {"google_flights": {"fetch_mode": "http"}}

    reset = client.put("/api/settings", json={"llm_model": "", "max_search_combinations": None}).json()
    assert reset["llm_model"] == "claude-opus-5-5" and reset["max_search_combinations"] == 12
    assert reset["notifications"]["telegram"]["chat_id"] == "42"  # untouched


@pytest.mark.parametrize(
    "body",
    [
        {"llm_effort": "extreme"},
        {"max_search_combinations": 0},
        {"browser_channel": "firefox"},
        {"notifications": {"email": {"smtp_port": 70000}}},
        {"language": "fr"},
    ],
)
def test_put_validation_errors_are_422(client: TestClient, body: dict[str, Any]) -> None:
    response = client.put("/api/settings", json=body)
    assert response.status_code == 422
    assert isinstance(response.json()["detail"], list)


def test_put_rejects_unknown_default_providers(client: TestClient) -> None:
    response = client.put("/api/settings", json={"default_providers": ["google_flights", "nope"]})
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail[0]["loc"] == ["body", "default_providers"] and "nope" in detail[0]["msg"]
    assert client.get("/api/settings").json()["default_providers"] == []  # nothing saved
    ok = client.put("/api/settings", json={"default_providers": ["naver", "google_flights"]})
    assert ok.status_code == 200 and ok.json()["default_providers"] == ["naver", "google_flights"]
    assert client.put("/api/settings", json={"default_providers": []}).json()["default_providers"] == []


def test_put_browser_settings_reconfigure_the_manager(
    client: TestClient, db: Session, browser: FakeBrowserManager
) -> None:
    client.put("/api/settings", json={"browser_channel": "chrome"})
    assert browser.calls == [("chrome", True)]
    client.put("/api/settings", json={"browser_headless": False})
    assert browser.calls[-1] == ("chrome", False)
    client.put("/api/settings", json={"language": "ko", "llm_effort": "max"})
    assert len(browser.calls) == 2  # unrelated change: no reconfigure
    client.put("/api/settings", json={"browser_channel": None, "browser_headless": None})
    assert browser.calls[-1] == ("msedge", True)  # reset to env defaults
    assert load_runtime_settings(db).browser_channel == "msedge"


def test_apply_browser_settings_never_raises(monkeypatch: pytest.MonkeyPatch, db: Session) -> None:
    class Broken:
        def configure(self, **_kwargs: Any) -> None:
            raise ValueError("invalid browser channel")

    monkeypatch.setattr(settings_api, "get_browser_manager", Broken)
    settings_api.apply_browser_settings(load_runtime_settings(db))


# --------------------------------------------------------------------------- notification tests


def test_notification_test_uses_dispatcher(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake_send_test(_db: Session, channel: str) -> str:
        calls.append(channel)
        return "skipped: bot token is not set"

    monkeypatch.setattr(dispatcher, "send_test", fake_send_test)
    response = client.post("/api/settings/notifications/test", json={"channel": "telegram"})
    assert response.status_code == 200
    assert response.json() == {"channel": "telegram", "result": "skipped: bot token is not set"}
    assert calls == ["telegram"]


def test_notification_test_rejects_unknown_channel(client: TestClient) -> None:
    assert client.post("/api/settings/notifications/test", json={"channel": "sms"}).status_code == 422
    assert client.post("/api/settings/notifications/test", json={}).status_code == 422


def test_inapp_notification_test_stores_a_system_notification(client: TestClient, db: Session) -> None:
    response = client.post("/api/settings/notifications/test", json={"channel": "inapp"})
    assert response.json() == {"channel": "inapp", "result": "sent"}
    rows = db.scalars(select(Notification)).all()
    assert len(rows) == 1 and rows[0].kind == "system"


# --------------------------------------------------------------------------- telegram detect


def test_detect_chat_needs_a_bot_token(client: TestClient) -> None:
    response = client.post("/api/settings/telegram/detect-chat")
    assert response.status_code == 400
    assert "@BotFather" in response.json()["detail"]


def test_detect_chat_saves_the_chat_id(client: TestClient, db: Session) -> None:
    token = "123456:ABC-secret"
    client.put("/api/settings", json={"notifications": {"telegram": {"bot_token": token}}})
    updates = {
        "ok": True,
        "result": [
            {"update_id": 1, "message": {"chat": {"id": -100, "type": "group", "title": "Group"}}},
            {
                "update_id": 2,
                "message": {"chat": {"id": 987654, "type": "private", "username": "traveler"}},
            },
        ],
    }
    with respx.mock(assert_all_called=True) as router:
        route = router.post(url__regex=TELEGRAM_UPDATES).mock(
            return_value=httpx.Response(200, json=updates)
        )
        response = client.post("/api/settings/telegram/detect-chat")
    assert response.status_code == 200
    assert response.json() == {"chat_id": "987654", "username": "traveler"}
    assert token in str(route.calls.last.request.url)
    assert client.get("/api/settings").json()["notifications"]["telegram"]["chat_id"] == "987654"


def test_detect_chat_without_updates_is_400_with_guidance(client: TestClient) -> None:
    client.put("/api/settings", json={"notifications": {"telegram": {"bot_token": "1:tok"}}})
    with respx.mock() as router:
        router.post(url__regex=TELEGRAM_UPDATES).mock(
            return_value=httpx.Response(200, json={"ok": True, "result": []})
        )
        response = client.post("/api/settings/telegram/detect-chat")
    assert response.status_code == 400
    assert "/start" in response.json()["detail"]
    assert client.get("/api/settings").json()["notifications"]["telegram"]["chat_id"] is None


def test_detect_chat_invalid_token_is_400_and_redacted(client: TestClient) -> None:
    token = "999:bad-token-value"
    client.put("/api/settings", json={"notifications": {"telegram": {"bot_token": token}}})
    with respx.mock() as router:
        router.post(url__regex=TELEGRAM_UPDATES).mock(
            return_value=httpx.Response(
                401, json={"ok": False, "error_code": 401, "description": "Unauthorized"}
            )
        )
        response = client.post("/api/settings/telegram/detect-chat")
    assert response.status_code == 400
    assert "invalid bot token" in response.json()["detail"] and token not in response.text
