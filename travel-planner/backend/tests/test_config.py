from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import REPO_ROOT, Settings


def test_backend_ignores_node_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PORT", "3001")  # the Node app's variable in the shared .env
    settings = Settings()
    assert settings.port == 8765
    assert settings.kakao_redirect_uri == "http://127.0.0.1:8765/api/integrations/kakao/callback"
    monkeypatch.setenv("BACKEND_PORT", "9000")
    assert Settings().port == 9000


def test_env_example_resolves_to_backend_defaults() -> None:
    example = REPO_ROOT / ".env.example"
    if not example.is_file():
        pytest.skip(".env.example not present")
    settings = Settings(_env_file=Path(example))
    assert settings.port == 8765 and settings.default_currency == "KRW"


def test_default_currency_normalised_and_validated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEFAULT_CURRENCY", " jpy ")
    assert Settings().default_currency == "JPY"
    monkeypatch.setenv("DEFAULT_CURRENCY", "won!")
    with pytest.raises(ValidationError):
        Settings()
