"""``GET /api/meta``: enums, defaults from settings, providers summary."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import __version__
from app.api import providers as providers_api
from app.config import get_settings
from app.schemas import CABINS, NOTIFICATION_CHANNELS, STYLE_KEYS


@pytest.fixture(autouse=True)
def no_browser_manager(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(providers_api, "_browser_manager", lambda: object())


def test_meta(client: TestClient) -> None:
    response = client.get("/api/meta")
    assert response.status_code == 200
    meta = response.json()
    assert meta["version"] == __version__
    assert meta["cabins"] == CABINS and meta["style_keys"] == STYLE_KEYS
    assert meta["notification_channels"] == NOTIFICATION_CHANNELS
    assert meta["flex_days"] == [0, 1, 3, 7] and "xhigh" in meta["llm_efforts"]
    defaults = meta["defaults"]
    assert (defaults["currency"], defaults["timezone"]) == ("KRW", "Asia/Seoul")
    assert defaults["style_weights"] == {key: 10.0 for key in STYLE_KEYS}
    assert defaults["score_style_weights"] == {key: 3.0 for key in STYLE_KEYS}
    assert defaults["schedule_times"] == ["08:30", "17:30"] and defaults["max_schedule_times"] == 6
    providers = {p["key"]: p for p in meta["providers"]}
    assert providers["google_flights"]["enabled"] and not providers["naver"]["enabled"]
    assert providers == {p["key"]: p for p in client.get("/api/providers").json()}


def test_meta_defaults_follow_settings(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEFAULT_CURRENCY", "jpy")
    monkeypatch.setenv("TIMEZONE", "Asia/Tokyo")
    get_settings.cache_clear()
    defaults = client.get("/api/meta").json()["defaults"]
    assert (defaults["currency"], defaults["timezone"]) == ("JPY", "Asia/Tokyo")
