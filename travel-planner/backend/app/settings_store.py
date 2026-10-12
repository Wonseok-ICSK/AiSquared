"""UI-editable runtime settings persisted in ``AppSetting["runtime"]`` (DESIGN §8).

Storage model: the row holds only *overrides* (a sparse nested dict); everything not
overridden comes from env/.env (:class:`app.config.Settings`). Secret leaves are stored
individually encrypted (``"enc:v1:..."``). In memory, secrets are ``SecretStr`` so that a
logged/printed ``RuntimeSettings`` never reveals them.

Secret names accepted by :func:`get_secret` are the keys of :data:`SECRETS`.
"""

from __future__ import annotations

import copy
import logging
from datetime import UTC, datetime
from typing import Annotated, Any, NamedTuple

from pydantic import BaseModel, BeforeValidator, Field, PrivateAttr, SecretStr, field_validator
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import session_scope
from app.models import AppSetting
from app.schemas import LLM_EFFORTS, Language, LlmEffort, RuntimeSettingsIn, RuntimeSettingsOut
from app.security.secrets import SecretBoxError, get_secretbox

logger = logging.getLogger(__name__)

RUNTIME_KEY = "runtime"
DEFAULT_LLM_EFFORT: LlmEffort = "xhigh"


class SecretSpec(NamedTuple):
    """Where a secret lives in RuntimeSettings and which ``Settings`` field is its env fallback."""

    path: tuple[str, ...]
    env_field: str | None


SECRETS: dict[str, SecretSpec] = {
    "anthropic": SecretSpec(("api_keys", "anthropic"), "anthropic_api_key"),
    "serpapi": SecretSpec(("api_keys", "serpapi"), "serpapi_api_key"),
    "google_maps": SecretSpec(("api_keys", "google_maps"), "google_maps_api_key"),
    "smtp_password": SecretSpec(("notifications", "email", "password"), "smtp_password"),
    "kakao_rest_api_key": SecretSpec(
        ("notifications", "kakao", "rest_api_key"), "kakao_rest_api_key"
    ),
    "kakao_client_secret": SecretSpec(
        ("notifications", "kakao", "client_secret"), "kakao_client_secret"
    ),
    "kakao_access_token": SecretSpec(("notifications", "kakao", "access_token"), None),
    "kakao_refresh_token": SecretSpec(("notifications", "kakao", "refresh_token"), None),
    "telegram_bot_token": SecretSpec(
        ("notifications", "telegram", "bot_token"), "telegram_bot_token"
    ),
    "ntfy_token": SecretSpec(("notifications", "ntfy", "token"), None),
}
_SECRET_PATHS = {spec.path: name for name, spec in SECRETS.items()}
_GROUP_PATHS = {
    ("notifications",),
    ("notifications", "desktop"),
    ("notifications", "email"),
    ("notifications", "kakao"),
    ("notifications", "telegram"),
    ("notifications", "ntfy"),
    ("api_keys",),
}


# --------------------------------------------------------------------------- models


def _coerce_effort(value: Any) -> Any:
    if value in LLM_EFFORTS:
        return value
    logger.warning("Invalid LLM effort %r; using %r", value, DEFAULT_LLM_EFFORT)
    return DEFAULT_LLM_EFFORT


class DesktopNotify(BaseModel):
    """Windows toast notifications."""

    enabled: bool = True


class EmailNotify(BaseModel):
    """SMTP e-mail notifications."""

    enabled: bool = False
    smtp_host: str | None = None
    smtp_port: int = 587
    username: str | None = None
    password: SecretStr | None = None
    from_addr: str | None = None
    to_addrs: list[str] = Field(default_factory=list)
    use_starttls: bool = True


class KakaoNotify(BaseModel):
    """KakaoTalk "send to me" notifications; expiry datetimes are naive UTC."""

    enabled: bool = False
    rest_api_key: SecretStr | None = None
    client_secret: SecretStr | None = None
    redirect_uri: str | None = None
    access_token: SecretStr | None = None
    refresh_token: SecretStr | None = None
    access_expires_at: datetime | None = None
    refresh_expires_at: datetime | None = None

    @field_validator("access_expires_at", "refresh_expires_at")
    @classmethod
    def _naive_utc(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is not None:
            return value.astimezone(UTC).replace(tzinfo=None)
        return value


class TelegramNotify(BaseModel):
    """Telegram bot notifications."""

    enabled: bool = False
    bot_token: SecretStr | None = None
    chat_id: str | None = None


class NtfyNotify(BaseModel):
    """ntfy push notifications (``POST {server}`` with a JSON body)."""

    enabled: bool = False
    server: str = "https://ntfy.sh"
    topic: str | None = None
    token: SecretStr | None = None


class NotificationSettings(BaseModel):
    """All notification channels."""

    desktop: DesktopNotify = Field(default_factory=DesktopNotify)
    email: EmailNotify = Field(default_factory=EmailNotify)
    kakao: KakaoNotify = Field(default_factory=KakaoNotify)
    telegram: TelegramNotify = Field(default_factory=TelegramNotify)
    ntfy: NtfyNotify = Field(default_factory=NtfyNotify)


class ApiKeys(BaseModel):
    """Third-party API credentials."""

    anthropic: SecretStr | None = None
    serpapi: SecretStr | None = None
    google_maps: SecretStr | None = None


class RuntimeSettings(BaseModel):
    """Effective runtime settings (env defaults + UI overrides), secrets decrypted in memory."""

    browser_channel: str = "msedge"
    browser_headless: bool = True
    llm_model: str = "claude-opus-5-5"
    llm_effort: Annotated[LlmEffort, BeforeValidator(_coerce_effort)] = DEFAULT_LLM_EFFORT
    max_search_combinations: int = 12
    default_providers: list[str] = Field(default_factory=list)
    provider_options: dict[str, dict[str, Any]] = Field(default_factory=dict)
    """Per-provider options passed to ``ProviderContext.options``."""
    language: Language = "ko"
    """Language for generated itineraries / notification text."""
    notifications: NotificationSettings = Field(default_factory=NotificationSettings)
    api_keys: ApiKeys = Field(default_factory=ApiKeys)

    _secret_sources: dict[str, str] = PrivateAttr(default_factory=dict)

    def secret_source(self, name: str) -> str | None:
        """``"ui"``, ``"env"`` or ``None`` for the secret ``name`` (see :data:`SECRETS`)."""
        return self._secret_sources.get(name)


# --------------------------------------------------------------------------- nested-dict helpers


def _get_path(data: dict[str, Any], path: tuple[str, ...]) -> Any:
    node: Any = data
    for part in path:
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _set_path(data: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    node = data
    for part in path[:-1]:
        node = node.setdefault(part, {})
    node[path[-1]] = value


def _pop_path(data: dict[str, Any], path: tuple[str, ...]) -> None:
    parent = _get_path(data, path[:-1]) if len(path) > 1 else data
    if isinstance(parent, dict):
        parent.pop(path[-1], None)


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


# --------------------------------------------------------------------------- resolution


def _env_defaults(settings: Settings) -> dict[str, Any]:
    """Non-secret defaults derived from env/.env."""
    telegram_ready = bool(settings.telegram_bot_token and settings.telegram_chat_id)
    return {
        "browser_channel": settings.browser_channel,
        "browser_headless": settings.browser_headless,
        "llm_model": settings.llm_model,
        "llm_effort": settings.llm_effort,
        "max_search_combinations": settings.max_search_combinations,
        "default_providers": [],
        "provider_options": {},
        "language": settings.language,
        "notifications": {
            "desktop": {"enabled": True},
            "email": {
                "enabled": bool(settings.smtp_host and settings.smtp_to_list),
                "smtp_host": settings.smtp_host,
                "smtp_port": settings.smtp_port,
                "username": settings.smtp_username,
                "from_addr": settings.smtp_from,
                "to_addrs": settings.smtp_to_list,
                "use_starttls": settings.smtp_starttls,
            },
            "kakao": {"enabled": False, "redirect_uri": settings.kakao_redirect_uri},
            "telegram": {"enabled": telegram_ready, "chat_id": settings.telegram_chat_id},
            "ntfy": {"enabled": False, "server": "https://ntfy.sh"},
        },
    }


def _env_secret(settings: Settings, spec: SecretSpec) -> str | None:
    if spec.env_field is None:
        return None
    value: SecretStr | None = getattr(settings, spec.env_field)
    return value.get_secret_value() if value is not None else None


def _decrypt_override(overrides: dict[str, Any], name: str) -> str | None:
    token = _get_path(overrides, SECRETS[name].path)
    if not token:
        return None
    try:
        return get_secretbox().decrypt_str(token)
    except SecretBoxError:
        logger.warning("Stored secret %r cannot be decrypted; ignoring it", name)
        return None


def _read_overrides(db: Session) -> dict[str, Any]:
    row = db.get(AppSetting, RUNTIME_KEY)
    return copy.deepcopy(dict(row.value)) if row is not None and row.value else {}


def _build(overrides: dict[str, Any], settings: Settings) -> RuntimeSettings:
    """Merge env defaults, non-secret overrides and resolved secrets into RuntimeSettings."""
    plain = copy.deepcopy(overrides)
    for path in _SECRET_PATHS:
        _pop_path(plain, path)
    merged = _deep_merge(_env_defaults(settings), plain)
    sources: dict[str, str] = {}
    for name, spec in SECRETS.items():
        ui_value = _decrypt_override(overrides, name)
        value = ui_value or _env_secret(settings, spec)
        if value:
            _set_path(merged, spec.path, value)
            sources[name] = "ui" if ui_value else "env"
    runtime = RuntimeSettings.model_validate(merged)
    runtime._secret_sources = sources
    return runtime


def _apply_patch(
    overrides: dict[str, Any], patch: dict[str, Any], prefix: tuple[str, ...] = ()
) -> None:
    for key, value in patch.items():
        path = (*prefix, key)
        if path in _SECRET_PATHS:
            if value is None:
                continue
            if value == "":
                _pop_path(overrides, path)
            else:
                _set_path(overrides, path, get_secretbox().encrypt_str(value))
        elif path in _GROUP_PATHS:
            if isinstance(value, dict):
                _apply_patch(overrides, value, path)
        elif value is None:
            _pop_path(overrides, path)
        else:
            _set_path(overrides, path, value)


# --------------------------------------------------------------------------- public API


def load_runtime_settings(db: Session) -> RuntimeSettings:
    """Effective runtime settings: env defaults overlaid with saved UI values."""
    return _build(_read_overrides(db), get_settings())


def save_runtime_settings(
    db: Session, update: RuntimeSettingsIn | dict[str, Any]
) -> RuntimeSettings:
    """Apply a partial update and commit it; returns the new effective settings.

    Secret fields: ``None``/omitted = unchanged, ``""`` = clear the UI value (env fallback
    applies again), any other string = encrypt and store. Non-secret fields: omitted =
    unchanged, ``None`` = drop the override (back to env/default). Raises
    ``pydantic.ValidationError`` for invalid input.
    """
    patch_model = (
        update if isinstance(update, RuntimeSettingsIn) else RuntimeSettingsIn.model_validate(update)
    )
    patch = patch_model.model_dump(mode="json", exclude_unset=True)
    overrides = _read_overrides(db)
    _apply_patch(overrides, patch)
    runtime = _build(overrides, get_settings())  # validates before persisting

    row = db.get(AppSetting, RUNTIME_KEY)
    if row is None:
        db.add(AppSetting(key=RUNTIME_KEY, value=overrides))
    else:
        row.value = overrides
    db.commit()
    return runtime


def to_public(runtime: RuntimeSettings) -> RuntimeSettingsOut:
    """Redacted view for ``GET /api/settings`` (secrets become ``{is_set, source}``)."""
    data = runtime.model_dump()
    for name, spec in SECRETS.items():
        is_set = _get_path(data, spec.path) is not None
        _set_path(data, spec.path, {"is_set": is_set, "source": runtime.secret_source(name)})
    return RuntimeSettingsOut.model_validate(data)


def get_secret(name: str, db: Session | None = None) -> str | None:
    """Resolve a secret: UI value -> env var -> ``None``. ``name`` is a key of :data:`SECRETS`."""
    if name not in SECRETS:
        raise KeyError(f"unknown secret {name!r}; expected one of {', '.join(SECRETS)}")
    if db is None:
        with session_scope() as session:
            return get_secret(name, session)
    return _decrypt_override(_read_overrides(db), name) or _env_secret(
        get_settings(), SECRETS[name]
    )
