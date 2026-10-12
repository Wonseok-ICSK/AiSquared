"""Process configuration from environment variables / ``<repo>/.env`` (DESIGN §8).

UI-editable runtime settings live in :mod:`app.settings_store` and override these.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = BACKEND_DIR.parent
FRONTEND_DIST = REPO_ROOT / "frontend" / "dist"
ENV_FILE = REPO_ROOT / ".env"


class Settings(BaseSettings):
    """Environment-driven settings; field names map to upper-case env vars (no prefix)."""

    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
        case_sensitive=False,
    )

    host: str = "127.0.0.1"
    port: int = Field(8765, validation_alias="BACKEND_PORT")
    """Env var ``BACKEND_PORT`` (not ``PORT``: the repo's Node app reads ``PORT`` from the
    shared ``.env``). Uvicorn's ``--port`` must match; used to build local self-links."""
    data_dir: Path = REPO_ROOT / "data"
    database_url: str = ""  # resolved to sqlite:///<data_dir>/app.db when empty
    timezone: str = "Asia/Seoul"
    default_currency: str = "KRW"
    language: Literal["en", "ko"] = Field("ko", validation_alias="APP_LANGUAGE")
    """Env var ``APP_LANGUAGE`` (not ``LANGUAGE``, which gettext uses): default language for
    itineraries and notification text; changeable in Settings."""

    browser_channel: str = "msedge"
    browser_headless: bool = True
    scheduler_enabled: bool = True
    max_search_combinations: int = 12

    anthropic_api_key: SecretStr | None = None
    llm_model: str = "claude-opus-5-5"
    llm_effort: str = "high"

    serpapi_api_key: SecretStr | None = None
    google_maps_api_key: SecretStr | None = None

    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    smtp_from: str | None = None
    smtp_to: str | None = None  # comma-separated
    smtp_starttls: bool = True

    kakao_rest_api_key: SecretStr | None = None
    kakao_client_secret: SecretStr | None = None
    kakao_redirect_uri: str | None = None  # defaults to the local callback URL

    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: str | None = None

    @field_validator("default_currency")
    @classmethod
    def _currency_code(cls, value: str) -> str:
        code = value.strip().upper()
        if not re.fullmatch(r"[A-Z]{3}", code):
            raise ValueError(f"DEFAULT_CURRENCY must be an ISO 4217 code, got {value!r}")
        return code

    @model_validator(mode="after")
    def _resolve_paths(self) -> Settings:
        if not self.data_dir.is_absolute():
            self.data_dir = (REPO_ROOT / self.data_dir).resolve()
        if not self.database_url:
            self.database_url = f"sqlite:///{(self.data_dir / 'app.db').as_posix()}"
        if not self.kakao_redirect_uri:
            self.kakao_redirect_uri = f"http://127.0.0.1:{self.port}/api/integrations/kakao/callback"
        return self

    @property
    def browser_profiles_dir(self) -> Path:
        """Directory holding persistent Playwright profiles."""
        return self.data_dir / "browser_profiles"

    @property
    def sessions_dir(self) -> Path:
        """Directory holding encrypted provider ``storage_state`` files."""
        return self.data_dir / "sessions"

    @property
    def logs_dir(self) -> Path:
        """Directory for application log files."""
        return self.data_dir / "logs"

    @property
    def smtp_to_list(self) -> list[str]:
        """``SMTP_TO`` split on commas/semicolons, blanks removed."""
        raw = (self.smtp_to or "").replace(";", ",")
        return [addr.strip() for addr in raw.split(",") if addr.strip()]


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings (cached; call ``get_settings.cache_clear()`` in tests)."""
    return Settings()


def ensure_dirs(settings: Settings | None = None) -> None:
    """Create ``data/`` and its runtime sub-directories if missing."""
    s = settings or get_settings()
    for path in (s.data_dir, s.browser_profiles_dir, s.sessions_dir, s.logs_dir):
        path.mkdir(parents=True, exist_ok=True)
