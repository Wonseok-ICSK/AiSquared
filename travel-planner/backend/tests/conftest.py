"""Shared fixtures: isolated settings/DB per test, a throwaway SecretBox, a TestClient.

Every test gets (autouse): env/.env isolation with ``DATA_DIR`` in ``tmp_path``, a fresh
SQLite database with all tables, a SecretBox with a generated key installed as the
singleton (the real Credential Manager is never touched), and an empty FX cache.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from app import main as main_module
from app.config import Settings, get_settings
from app.core import fx
from app.db import SessionLocal, configure_engine, get_db, init_db
from app.security import secrets as secrets_module
from app.security.secrets import SecretBox, set_secretbox


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip ``@pytest.mark.live`` tests unless RUN_LIVE=1."""
    if os.environ.get("RUN_LIVE") == "1":
        return
    skip_live = pytest.mark.skip(reason="live test; set RUN_LIVE=1 to run")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)


def _keyring_forbidden(*_args: object, **_kwargs: object) -> None:
    raise RuntimeError("tests must not use the real Windows Credential Manager")


@pytest.fixture(autouse=True)
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Settings]:
    """Settings that ignore the developer's .env and real environment."""
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    for field_name, field in Settings.model_fields.items():
        monkeypatch.delenv(field_name.upper(), raising=False)
        if isinstance(field.validation_alias, str):
            monkeypatch.delenv(field.validation_alias.upper(), raising=False)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    # The app defaults to Korean; most tests assert the English texts.
    monkeypatch.setenv("APP_LANGUAGE", "en")
    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def engine(settings: Settings) -> Iterator[Engine]:
    """Fresh temp-file SQLite database with all tables created."""
    test_engine = configure_engine(settings.database_url)
    init_db()
    yield test_engine
    test_engine.dispose()


@pytest.fixture(autouse=True)
def secretbox(monkeypatch: pytest.MonkeyPatch) -> Iterator[SecretBox]:
    """SecretBox with a random key installed as the singleton; keyring access is blocked."""
    monkeypatch.setattr(secrets_module.keyring, "get_password", _keyring_forbidden)
    monkeypatch.setattr(secrets_module.keyring, "set_password", _keyring_forbidden)
    box = SecretBox(key=Fernet.generate_key())
    set_secretbox(box)
    yield box
    set_secretbox(None)


@pytest.fixture(autouse=True)
def _clear_fx_cache() -> Iterator[None]:
    fx.clear_cache()
    yield
    fx.clear_cache()


@pytest.fixture
def db(engine: Engine) -> Iterator[Session]:
    """A session on the test database."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def client(engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """TestClient for ``create_app()`` on the test DB (no SPA build, no log files)."""
    monkeypatch.setattr(main_module, "configure_logging", lambda _settings: None)
    # The lifespan starts the monitoring scheduler; give tests an instance that never starts
    # its thread (tests that need scheduling call get_scheduler() / sync_trip directly).
    from app.config import get_settings
    from app.services import scheduler as scheduler_module

    sched = scheduler_module.MonitorScheduler(get_settings())
    monkeypatch.setattr(sched, "start", lambda: False)
    monkeypatch.setattr(scheduler_module, "_instance", sched)
    app = main_module.create_app(frontend_dist=tmp_path / "no-frontend")

    def _test_db() -> Iterator[Session]:
        session = SessionLocal()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = _test_db
    with TestClient(app) as test_client:
        yield test_client
