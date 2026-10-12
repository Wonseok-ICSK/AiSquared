"""KakaoTalk "나에게 보내기" (send to me) channel + OAuth helpers (DESIGN §13.3).

Delivery: ``POST https://kapi.kakao.com/v2/api/talk/memo/default/send`` with a *text*
template (``text`` <= 200 characters shown, a ``link`` with ``web_url`` and
``mobile_web_url`` is always included - Kakao rejects templates without one - plus
``button_title``). Messages land in the user's "chat with myself" **without a push alert**,
so this channel is a record/archive, not a real-time alert.

OAuth (authorization-code flow, scope ``talk_message``): :func:`authorize_url` ->
user consents on kauth.kakao.com -> ``/api/integrations/kakao/callback?code=&state=`` ->
:func:`exchange_code` (a callback without a valid ``state`` is rejected - CSRF).
``client_secret`` is sent whenever it is set (it is ON by default for
REST API keys created in the 2025-12 console). The access token lives ~6 h, the refresh
token ~2 months. A refresh *may rotate* the refresh token (the old one is revoked at once),
so :func:`ensure_access_token` refreshes under one process-wide lock and persists the new
tokens immediately, before using them.

Link domains: every link must use a domain registered under [앱] > [제품 링크 관리] >
[웹 도메인]; links to unregistered domains are silently replaced by the *first* registered
(default) domain - register a public booking domain first, not localhost.

Error codes (branch on ``code``, not ``msg``): ``-401`` invalid/expired access token (or a
configured "allowed IP" list) -> refresh once and retry; ``-402`` the user did not grant
``talk_message``; ``-3`` the talk_message consent item is not enabled in the app; ``-2``
template parse error; ``-501..-532`` messaging restrictions. Token endpoint: ``KOE320``
bad/used code, ``KOE322``/``invalid_grant`` dead refresh token (reconnect needed),
``KOE006`` redirect URI mismatch, ``KOE010`` wrong client secret, ``KOE004`` Kakao Login off.
"""

from __future__ import annotations

import json
import logging
import secrets
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import session_scope
from app.models import AppSetting
from app.services.events import NotificationMessage
from app.services.notifications.base import (
    ChannelError,
    NotificationChannel,
    app_url,
    is_http_url,
    redact,
    register_channel,
    secret_value,
    summary_text,
    truncate_text,
)
from app.settings_store import (
    RUNTIME_KEY,
    KakaoNotify,
    RuntimeSettings,
    load_runtime_settings,
    save_runtime_settings,
)
from app.timeutil import utcnow

logger = logging.getLogger(__name__)

AUTHORIZE_URL = "https://kauth.kakao.com/oauth/authorize"
TOKEN_URL = "https://kauth.kakao.com/oauth/token"
MEMO_SEND_URL = "https://kapi.kakao.com/v2/api/talk/memo/default/send"
SCOPE = "talk_message"
TEXT_LIMIT = 200
BUTTON_TITLE_LIMIT = 14
REFRESH_MARGIN = timedelta(minutes=10)
STATE_TTL = timedelta(minutes=15)
DEFAULT_ACCESS_TTL_S = 6 * 3600
TIMEOUT_S = 15.0
# Kakao documents this exact Content-Type for both kauth and kapi form posts.
FORM_HEADERS = {"Content-Type": "application/x-www-form-urlencoded;charset=utf-8"}
_PERSIST_ATTEMPTS = 3

_refresh_lock = threading.Lock()
_states: dict[str, datetime] = {}
_states_lock = threading.Lock()


class KakaoError(ChannelError):
    """Kakao API/token failure; ``code`` is Kakao's numeric API code or ``KOE...`` string."""

    def __init__(self, message: str, *, code: int | str | None = None) -> None:
        super().__init__(message)
        self.code = code


class KakaoAuthError(KakaoError):
    """The user must (re)connect KakaoTalk in Settings."""


@dataclass(frozen=True)
class KakaoConnection:
    """Result of a successful :func:`exchange_code` (no token values)."""

    scopes: list[str]
    talk_message: bool
    access_expires_at: datetime | None
    refresh_expires_at: datetime | None


# --------------------------------------------------------------------------- OAuth state


def _remember_state(state: str) -> None:
    now = utcnow()
    with _states_lock:
        for key in [k for k, expires in _states.items() if expires <= now]:
            del _states[key]
        _states[state] = now + STATE_TTL


def _consume_state(state: str) -> bool:
    with _states_lock:
        expires = _states.pop(state, None)
    return expires is not None and expires > utcnow()


# --------------------------------------------------------------------------- token endpoint


def _credentials(cfg: KakaoNotify) -> tuple[str, str | None]:
    client_id = secret_value(cfg.rest_api_key)
    if not client_id:
        raise KakaoAuthError(
            "Kakao REST API key is not set (Kakao Developers > [앱] > [플랫폼 키] > REST API 키)"
        )
    return client_id, secret_value(cfg.client_secret)


def _redirect_uri(cfg: KakaoNotify) -> str:
    return (cfg.redirect_uri or "").strip() or str(get_settings().kakao_redirect_uri)


def _token_error(data: dict[str, Any], status: int, *sensitive: str | None) -> KakaoError:
    error = str(data.get("error") or "")
    code = str(data.get("error_code") or error or f"HTTP {status}")
    description = redact(str(data.get("error_description") or ""), *sensitive)
    hints = {
        "KOE320": "authorization code is invalid or was already used - connect KakaoTalk again",
        "KOE322": "Kakao authorization expired or was revoked - reconnect KakaoTalk in Settings",
        "KOE006": "redirect URI mismatch - register the exact callback URL under [앱] > "
        "[플랫폼 키] > [REST API 키] > 리다이렉트 URI",
        "KOE010": "client secret mismatch - copy the 클라이언트 시크릿 from the REST API key "
        "page (or clear it here if disabled)",
        "KOE101": "invalid REST API key",
        "KOE004": "Kakao Login is disabled - turn on [카카오 로그인] > 사용 설정",
    }
    message = f"Kakao token error {code}: {hints.get(code) or description or error or 'failed'}"
    if code == "KOE322" or (error == "invalid_grant" and code != "KOE320"):
        return KakaoAuthError(message, code=code)
    return KakaoError(message, code=code)


def _token_request(form: dict[str, str], *sensitive: str | None) -> dict[str, Any]:
    """POST the token endpoint; returns the JSON token response."""
    try:
        response = httpx.post(TOKEN_URL, data=form, headers=FORM_HEADERS, timeout=TIMEOUT_S)
    except httpx.HTTPError as exc:
        raise KakaoError(f"cannot reach Kakao: {type(exc).__name__}") from None
    try:
        data = response.json()
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    if response.status_code == 200 and data.get("access_token"):
        return data
    raise _token_error(data, response.status_code, *sensitive)


def _token_update(tokens: dict[str, Any]) -> dict[str, Any]:
    now = utcnow()
    update: dict[str, Any] = {
        "access_token": str(tokens["access_token"]),
        "access_expires_at": now
        + timedelta(seconds=int(tokens.get("expires_in") or DEFAULT_ACCESS_TTL_S)),
    }
    if tokens.get("refresh_token"):  # only present when Kakao issued (rotated) a new one
        update["refresh_token"] = str(tokens["refresh_token"])
        if tokens.get("refresh_token_expires_in"):
            seconds = int(tokens["refresh_token_expires_in"])
            update["refresh_expires_at"] = now + timedelta(seconds=seconds)
    return update


def _persist_kakao(db: Session, update: dict[str, Any]) -> None:
    """Save Kakao settings, retrying briefly: a lost rotated refresh token forces re-auth."""
    for attempt in range(1, _PERSIST_ATTEMPTS + 1):
        try:
            save_runtime_settings(db, {"notifications": {"kakao": update}})
            return
        except Exception:
            db.rollback()
            if attempt == _PERSIST_ATTEMPTS:
                logger.error("Could not persist Kakao tokens after %d attempts", attempt)
                raise
            logger.warning("Persisting Kakao tokens failed (attempt %d); retrying", attempt)


# --------------------------------------------------------------------------- public OAuth API


def authorize_url(rs: RuntimeSettings, state: str | None = None) -> str:
    """Kakao consent URL (scope ``talk_message``); ``state`` is remembered for the callback."""
    cfg = rs.notifications.kakao
    client_id, _secret = _credentials(cfg)
    state = state or secrets.token_urlsafe(24)
    _remember_state(state)
    query = {
        "client_id": client_id,
        "redirect_uri": _redirect_uri(cfg),
        "response_type": "code",
        "scope": SCOPE,
        "state": state,
    }
    return f"{AUTHORIZE_URL}?{urlencode(query)}"


def exchange_code(
    db: Session, code: str, state: str | None = None, *, verify_state: bool = True
) -> KakaoConnection:
    """Exchange the callback ``code`` for tokens, persist them and enable the channel.

    ``state`` must be one issued by :func:`authorize_url` (CSRF protection; single-use,
    valid 15 min). A missing or empty ``state`` is rejected like a wrong one, so a callback
    request without ``state`` can never connect an account; only ``verify_state=False``
    (tests / manual tooling) skips the check. Commits ``db``.
    """
    if verify_state and not (state and _consume_state(state)):
        raise KakaoAuthError("invalid or expired OAuth state - click 'Connect KakaoTalk' again")
    if not code:
        raise KakaoAuthError("missing authorization code")
    cfg = load_runtime_settings(db).notifications.kakao
    client_id, client_secret = _credentials(cfg)
    form = {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "redirect_uri": _redirect_uri(cfg),
        "code": code,
    }
    if client_secret:
        form["client_secret"] = client_secret
    with _refresh_lock:
        tokens = _token_request(form, code, client_secret, client_id)
        update = _token_update(tokens) | {"enabled": True}
        _persist_kakao(db, update)
    scopes = str(tokens.get("scope") or "").split()
    if SCOPE not in scopes:
        logger.warning("Kakao connected without the talk_message scope; sending will fail")
    logger.info("KakaoTalk connected (scopes: %s)", ", ".join(scopes) or "-")
    return KakaoConnection(
        scopes=scopes,
        talk_message=SCOPE in scopes,
        access_expires_at=update["access_expires_at"],
        refresh_expires_at=update.get("refresh_expires_at"),
    )


def clear_tokens(db: Session) -> None:
    """Forget the Kakao tokens (disconnect / dead refresh token). Commits ``db``."""
    _persist_kakao(
        db,
        {
            "access_token": "",
            "refresh_token": "",
            "access_expires_at": None,
            "refresh_expires_at": None,
        },
    )


def _fresh_kakao_settings(db: Session) -> KakaoNotify:
    """Kakao settings re-read from the DB (another thread may have rotated the tokens)."""
    row = db.get(AppSetting, RUNTIME_KEY)
    if row is not None:
        db.refresh(row)
    return load_runtime_settings(db).notifications.kakao


def _is_fresh(expires_at: datetime | None) -> bool:
    return expires_at is None or expires_at - utcnow() > REFRESH_MARGIN


def ensure_access_token(
    db: Session, *, force_refresh: bool = False, stale_token: str | None = None
) -> str:
    """A valid access token, refreshing it (under a lock) when it expires within 10 min.

    ``force_refresh`` (after a ``-401``) refreshes even an unexpired token unless another
    caller already replaced ``stale_token``. A rotated refresh token is persisted before
    this returns. A dead refresh token clears the stored tokens and raises
    :class:`KakaoAuthError`. Commits ``db`` when it refreshes.
    """
    with _refresh_lock:
        cfg = _fresh_kakao_settings(db)
        access = secret_value(cfg.access_token)
        if access and _is_fresh(cfg.access_expires_at):
            if not force_refresh or (stale_token is not None and access != stale_token):
                return access
        return _refresh_locked(db, cfg)


def _refresh_locked(db: Session, cfg: KakaoNotify) -> str:
    refresh_token = secret_value(cfg.refresh_token)
    if not refresh_token:
        raise KakaoAuthError("KakaoTalk is not connected - click 'Connect KakaoTalk' in Settings")
    client_id, client_secret = _credentials(cfg)
    form = {"grant_type": "refresh_token", "client_id": client_id, "refresh_token": refresh_token}
    if client_secret:
        form["client_secret"] = client_secret
    try:
        tokens = _token_request(form, refresh_token, client_secret, client_id)
    except KakaoAuthError:
        logger.warning("Kakao refresh token rejected; clearing tokens (reconnect required)")
        clear_tokens(db)
        raise
    _persist_kakao(db, _token_update(tokens))  # immediately: the old refresh token may be dead
    if tokens.get("refresh_token"):
        logger.info("Kakao refresh token rotated and saved")
    return str(tokens["access_token"])


# --------------------------------------------------------------------------- sending


def _int_code(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _api_error(status: int, data: dict[str, Any]) -> KakaoError:
    code = _int_code(data.get("code"))
    msg = str(data.get("msg") or f"HTTP {status}")
    if code == -401:
        text = "access token invalid or expired (or this PC's IP is not in the app's allowed IPs)"
    elif code == -402:
        text = (
            "KakaoTalk message permission (talk_message) was not granted - reconnect KakaoTalk "
            "and accept '카카오톡 메시지 전송'"
        )
    elif code == -3:
        text = (
            "the '카카오톡 메시지 전송 (talk_message)' consent item is not enabled in your Kakao "
            "app - enable it under [카카오 로그인] > [동의항목], then reconnect"
        )
    elif code == -2:
        text = f"invalid message template: {msg}"
    elif code is not None and -532 <= code <= -501:
        text = f"messaging restricted by Kakao: {msg}"
    else:
        text = msg
    error_cls = KakaoAuthError if code in (-402, -3) else KakaoError
    return error_cls(f"Kakao error {code if code is not None else status}: {text}", code=code)


def build_template(text: str, url: str, button_title: str) -> dict[str, Any]:
    """Text template; ``link`` (web + mobile) is always present as Kakao requires."""
    return {
        "object_type": "text",
        "text": truncate_text(text, TEXT_LIMIT),
        "link": {"web_url": url, "mobile_web_url": url},
        "button_title": button_title,
    }


def send_memo(access_token: str, template: dict[str, Any]) -> None:
    """POST the "send to me" API; raises :class:`KakaoError` with Kakao's ``code``.

    The template JSON is ASCII-only (Korean as ``\\uXXXX`` escapes) and the documented
    ``charset=utf-8`` Content-Type is sent, so the text never depends on Kakao's default
    form charset.
    """
    try:
        response = httpx.post(
            MEMO_SEND_URL,
            headers=FORM_HEADERS | {"Authorization": f"Bearer {access_token}"},
            data={"template_object": json.dumps(template, ensure_ascii=True)},
            timeout=TIMEOUT_S,
        )
    except httpx.HTTPError as exc:
        raise KakaoError(f"cannot reach Kakao: {type(exc).__name__}") from None
    try:
        data = response.json()
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    if response.status_code == 200 and data.get("result_code") == 0:
        return
    raise _api_error(response.status_code, data)


def message_text(message: NotificationMessage) -> str:
    """Title + short summary, cut to Kakao's 200 visible characters at a line/word boundary."""
    return truncate_text(f"{message.title}\n{summary_text(message)}".strip(), TEXT_LIMIT)


def _button_title(label: str) -> str:
    if len(label) <= BUTTON_TITLE_LIMIT:
        return label
    words: list[str] = []
    for word in label.split():
        if len(" ".join([*words, word])) > BUTTON_TITLE_LIMIT:
            break
        words.append(word)
    return " ".join(words) or label[:BUTTON_TITLE_LIMIT]


def _template_for(message: NotificationMessage) -> dict[str, Any]:
    url = message.url if is_http_url(message.url) else app_url("/")
    return build_template(message_text(message), url, _button_title(message.url_label))


class KakaoChannel(NotificationChannel):
    """KakaoTalk 'chat with myself' record (no push alert)."""

    key = "kakao"
    display_name = "KakaoTalk (send to me)"
    description = "Records to KakaoTalk 'chat with myself' - no push alert."

    def is_configured(self, rs: RuntimeSettings) -> tuple[bool, str]:
        """Needs the REST API key and a (not yet expired) OAuth connection."""
        cfg = rs.notifications.kakao
        if not secret_value(cfg.rest_api_key):
            return False, "REST API key is not set (Kakao Developers > [앱] > [플랫폼 키])"
        refresh, access = secret_value(cfg.refresh_token), secret_value(cfg.access_token)
        if not refresh and not access:
            return False, "not connected - click 'Connect KakaoTalk' in Settings"
        now = utcnow()
        access_dead = not access or (
            cfg.access_expires_at is not None and cfg.access_expires_at <= now
        )
        refresh_dead = not refresh or (
            cfg.refresh_expires_at is not None and cfg.refresh_expires_at <= now
        )
        if access_dead and refresh_dead:
            return False, "authorization expired - reconnect KakaoTalk in Settings"
        return True, ""

    def send(self, message: NotificationMessage, rs: RuntimeSettings) -> None:
        """Send a text memo; on ``-401`` refresh once and retry."""
        template = _template_for(message)
        with session_scope() as db:
            token = ensure_access_token(db)
        try:
            send_memo(token, template)
        except KakaoError as err:
            if err.code != -401:
                raise
            logger.info("Kakao access token rejected (-401); refreshing once and retrying")
            with session_scope() as db:
                token = ensure_access_token(db, force_refresh=True, stale_token=token)
            send_memo(token, template)


register_channel(KakaoChannel())
