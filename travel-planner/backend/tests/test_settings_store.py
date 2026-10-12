from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import AppSetting
from app.schemas import RuntimeSettingsIn
from app.settings_store import (
    SECRETS,
    get_secret,
    load_runtime_settings,
    save_runtime_settings,
    to_public,
)


def stored(db: Session) -> dict:
    db.expire_all()
    row = db.get(AppSetting, "runtime")
    return dict(row.value) if row else {}


def test_defaults_from_env(db: Session) -> None:
    rs = load_runtime_settings(db)
    assert rs.browser_channel == "msedge" and rs.browser_headless is True
    assert rs.llm_model == "claude-opus-5-5" and rs.llm_effort == "xhigh"
    assert rs.max_search_combinations == 12 and rs.default_providers == []
    assert rs.notifications.desktop.enabled is True
    assert rs.notifications.email.enabled is False
    assert rs.api_keys.anthropic is None
    assert rs.notifications.kakao.redirect_uri.endswith("/api/integrations/kakao/callback")


def test_secret_encrypted_at_rest_and_decrypted_in_memory(db: Session) -> None:
    rs = save_runtime_settings(db, {"api_keys": {"serpapi": "serp-123"}})
    assert rs.api_keys.serpapi.get_secret_value() == "serp-123"
    raw = stored(db)
    assert raw["api_keys"]["serpapi"].startswith("enc:v1:")
    assert "serp-123" not in json.dumps(raw)
    assert "serp-123" not in repr(rs)
    assert load_runtime_settings(db).api_keys.serpapi.get_secret_value() == "serp-123"
    assert get_secret("serpapi", db) == "serp-123"


def test_secret_none_is_unchanged_and_empty_clears(db: Session) -> None:
    save_runtime_settings(db, RuntimeSettingsIn.model_validate({"api_keys": {"anthropic": "sk-1"}}))
    save_runtime_settings(db, {"api_keys": {"anthropic": None}})
    save_runtime_settings(db, {"browser_headless": False})
    assert get_secret("anthropic", db) == "sk-1"
    save_runtime_settings(db, {"api_keys": {"anthropic": ""}})
    assert get_secret("anthropic", db) is None
    assert "anthropic" not in stored(db).get("api_keys", {})


def test_group_none_does_not_wipe_secrets(db: Session) -> None:
    save_runtime_settings(db, {"notifications": {"telegram": {"bot_token": "123:abc", "chat_id": "42"}}})
    save_runtime_settings(db, {"notifications": None})
    assert get_secret("telegram_bot_token", db) == "123:abc"


def test_to_public_redacts(db: Session) -> None:
    rs = save_runtime_settings(
        db,
        {
            "api_keys": {"google_maps": "maps-key"},
            "notifications": {"email": {"password": "app-pass", "smtp_host": "smtp.gmail.com"}},
        },
    )
    public = to_public(rs)
    body = public.model_dump_json()
    assert "maps-key" not in body and "app-pass" not in body
    assert public.api_keys.google_maps.is_set is True
    assert public.api_keys.google_maps.source == "ui"
    assert public.api_keys.anthropic.is_set is False and public.api_keys.anthropic.source is None
    assert public.notifications.email.password.is_set is True
    assert public.notifications.email.smtp_host == "smtp.gmail.com"


def test_env_fallback(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SERPAPI_API_KEY", "env-serp")
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_TO", "me@example.com; you@example.com")
    monkeypatch.setenv("LLM_EFFORT", "max")
    get_settings.cache_clear()

    assert get_secret("serpapi", db) == "env-serp"
    rs = load_runtime_settings(db)
    assert to_public(rs).api_keys.serpapi.source == "env"
    assert rs.notifications.email.enabled is True
    assert rs.notifications.email.to_addrs == ["me@example.com", "you@example.com"]
    assert rs.llm_effort == "max"

    save_runtime_settings(db, {"api_keys": {"serpapi": "ui-serp"}})
    assert get_secret("serpapi", db) == "ui-serp"
    save_runtime_settings(db, {"api_keys": {"serpapi": ""}})
    assert get_secret("serpapi", db) == "env-serp"


def test_invalid_env_effort_falls_back(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_EFFORT", "turbo")
    get_settings.cache_clear()
    assert load_runtime_settings(db).llm_effort == "xhigh"


def test_non_secret_override_and_reset(db: Session) -> None:
    save_runtime_settings(db, {"browser_headless": False, "default_providers": ["mock"]})
    rs = load_runtime_settings(db)
    assert rs.browser_headless is False and rs.default_providers == ["mock"]
    save_runtime_settings(db, {"browser_headless": None})
    rs = load_runtime_settings(db)
    assert rs.browser_headless is True and rs.default_providers == ["mock"]


def test_invalid_update_is_rejected_and_not_persisted(db: Session) -> None:
    with pytest.raises(ValidationError):
        save_runtime_settings(db, {"max_search_combinations": 0})
    with pytest.raises(ValidationError):
        save_runtime_settings(db, {"notifications": {"email": {"smtp_port": "abc"}}})
    assert stored(db) == {}


def test_kakao_tokens_and_expiry(db: Session) -> None:
    expires = datetime(2026, 10, 6, 18, 0, tzinfo=timezone(timedelta(hours=9)))
    save_runtime_settings(
        db,
        {"notifications": {"kakao": {"access_token": "tok", "access_expires_at": expires}}},
    )
    rs = load_runtime_settings(db)
    assert rs.notifications.kakao.access_expires_at == datetime(2026, 10, 6, 9, 0)
    assert get_secret("kakao_access_token", db) == "tok"
    public = json.loads(to_public(rs).model_dump_json())
    assert public["notifications"]["kakao"]["access_expires_at"] == "2026-10-06T09:00:00Z"
    assert public["notifications"]["kakao"]["access_token"] == {"is_set": True, "source": "ui"}


def test_get_secret_without_session_and_unknown_name(db: Session) -> None:
    save_runtime_settings(db, {"api_keys": {"serpapi": "tp"}})
    assert get_secret("serpapi") == "tp"
    with pytest.raises(KeyError):
        get_secret("does_not_exist")
    assert {"anthropic", "serpapi", "google_maps", "smtp_password"} <= set(SECRETS)


def test_undecryptable_secret_is_ignored(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SERPAPI_API_KEY", "env-serp")
    get_settings.cache_clear()
    db.add(AppSetting(key="runtime", value={"api_keys": {"serpapi": "enc:v1:garbage"}}))
    db.commit()
    assert get_secret("serpapi", db) == "env-serp"


def test_blank_llm_model_resets_to_env_default(db: Session) -> None:
    save_runtime_settings(db, {"llm_model": "claude-custom"})
    assert load_runtime_settings(db).llm_model == "claude-custom"
    rs = save_runtime_settings(db, {"llm_model": ""})
    assert rs.llm_model == "claude-opus-5-5"
    assert "llm_model" not in stored(db)
