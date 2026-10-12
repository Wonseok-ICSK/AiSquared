"""KakaoTalk OAuth, token refresh/rotation (incl. race lock), -401 retry and error codes."""

from __future__ import annotations

import json
import threading
import time
from datetime import timedelta
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx
from sqlalchemy.orm import Session

from app.db import session_scope
from app.models import AppSetting
from app.services.events import NotificationMessage
from app.services.notifications import kakao
from app.services.notifications.base import ChannelError, get_channel
from app.settings_store import (
    RuntimeSettings,
    get_secret,
    load_runtime_settings,
    save_runtime_settings,
)
from app.timeutil import utcnow

MESSAGE = NotificationMessage(
    title="Flight Price Alert",
    body="Seoul (ICN) ↔ Tokyo (NRT)\n\nCurrent Price:\nKRW 278,000",
    url="https://flight.naver.com/flights/x",
    url_label="Open booking page",
    data={"summary": "Seoul (ICN) ↔ Tokyo (NRT) · Nov 20 – Nov 23\nNow KRW 278,000"},
)


def form(request: httpx.Request) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(request.content.decode()).items()}


def token_response(access: str, refresh: str | None = None, **extra: Any) -> httpx.Response:
    body: dict[str, Any] = {"access_token": access, "token_type": "bearer", "expires_in": 21599}
    if refresh:
        body |= {"refresh_token": refresh, "refresh_token_expires_in": 5_183_999}
    return httpx.Response(200, json=body | extra)


def connect(
    db: Session,
    *,
    access: str | None = "a1",
    refresh: str | None = "r1",
    access_in: timedelta = timedelta(hours=5),
    secret: str | None = "client-secret",
) -> RuntimeSettings:
    kakao_cfg: dict[str, Any] = {
        "enabled": True,
        "rest_api_key": "rest-key",
        "access_token": access,
        "refresh_token": refresh,
        "access_expires_at": (utcnow() + access_in).isoformat(),
        "refresh_expires_at": (utcnow() + timedelta(days=50)).isoformat(),
    }
    if secret:
        kakao_cfg["client_secret"] = secret
    return save_runtime_settings(db, {"notifications": {"kakao": kakao_cfg}})


def fresh(db: Session) -> RuntimeSettings:
    db.expire_all()
    return load_runtime_settings(db)


def issue_state(db: Session) -> str:
    """A state as the Settings page gets it from authorize_url (needs the REST API key)."""
    return parse_qs(urlparse(kakao.authorize_url(load_runtime_settings(db))).query)["state"][0]


FORM_CONTENT_TYPE = "application/x-www-form-urlencoded;charset=utf-8"


# --------------------------------------------------------------------------- OAuth


def test_authorize_url(db: Session) -> None:
    rs = connect(db)
    url = kakao.authorize_url(rs, "state-1")
    parsed = urlparse(url)
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == kakao.AUTHORIZE_URL
    query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
    assert query == {
        "client_id": "rest-key",
        "redirect_uri": "http://127.0.0.1:8765/api/integrations/kakao/callback",
        "response_type": "code",
        "scope": "talk_message",
        "state": "state-1",
    }
    generated = parse_qs(urlparse(kakao.authorize_url(rs)).query)["state"][0]
    assert len(generated) >= 20


def test_authorize_url_requires_rest_key(db: Session) -> None:
    with pytest.raises(kakao.KakaoAuthError, match="REST API key"):
        kakao.authorize_url(load_runtime_settings(db), "s")


@respx.mock
def test_exchange_code_persists_encrypted_tokens_and_enables(db: Session) -> None:
    save_runtime_settings(
        db, {"notifications": {"kakao": {"rest_api_key": "rest-key", "client_secret": "cs"}}}
    )
    state_url = kakao.authorize_url(load_runtime_settings(db))
    state = parse_qs(urlparse(state_url).query)["state"][0]
    route = respx.post(kakao.TOKEN_URL).mock(
        return_value=token_response("a1", "r1", scope="talk_message profile_nickname")
    )
    connection = kakao.exchange_code(db, "the-code", state)
    assert connection.talk_message is True and "talk_message" in connection.scopes
    assert route.calls.last.request.headers["Content-Type"] == FORM_CONTENT_TYPE
    assert form(route.calls.last.request) == {
        "grant_type": "authorization_code",
        "client_id": "rest-key",
        "redirect_uri": "http://127.0.0.1:8765/api/integrations/kakao/callback",
        "code": "the-code",
        "client_secret": "cs",
    }
    rs = fresh(db)
    cfg = rs.notifications.kakao
    assert cfg.enabled is True
    assert cfg.access_token.get_secret_value() == "a1"
    assert cfg.refresh_token.get_secret_value() == "r1"
    assert timedelta(hours=5) < cfg.access_expires_at - utcnow() <= timedelta(hours=6)
    assert timedelta(days=59) < cfg.refresh_expires_at - utcnow() <= timedelta(days=60)
    raw = json.dumps(db.get(AppSetting, "runtime").value)
    assert '"r1"' not in raw and '"a1"' not in raw
    assert get_channel("kakao").is_configured(rs) == (True, "")
    # a state is single-use
    with pytest.raises(kakao.KakaoAuthError, match="state"):
        kakao.exchange_code(db, "other-code", state)


@respx.mock
def test_exchange_code_without_client_secret_and_unknown_state(db: Session) -> None:
    save_runtime_settings(db, {"notifications": {"kakao": {"rest_api_key": "rest-key"}}})
    with pytest.raises(kakao.KakaoAuthError, match="state"):
        kakao.exchange_code(db, "code", "never-issued")
    route = respx.post(kakao.TOKEN_URL).mock(return_value=token_response("a1", "r1"))
    connection = kakao.exchange_code(db, "code", issue_state(db))
    assert "client_secret" not in form(route.calls.last.request)
    assert connection.talk_message is False


@pytest.mark.parametrize("state", [None, ""])
@respx.mock
def test_exchange_code_without_state_is_rejected(db: Session, state: str | None) -> None:
    """CSRF: a callback request lacking ``state`` must not connect (or enable) anything."""
    save_runtime_settings(
        db, {"notifications": {"kakao": {"rest_api_key": "rest-key", "enabled": False}}}
    )
    issue_state(db)  # a pending, legitimately issued state must not help a state-less call
    route = respx.post(kakao.TOKEN_URL).mock(return_value=token_response("evil-a", "evil-r"))
    with pytest.raises(kakao.KakaoAuthError, match="OAuth state"):
        kakao.exchange_code(db, "attacker-code", state)
    with pytest.raises(kakao.KakaoAuthError, match="OAuth state"):
        kakao.exchange_code(db, "attacker-code")
    assert route.call_count == 0
    cfg = fresh(db).notifications.kakao
    assert cfg.enabled is False and cfg.access_token is None and cfg.refresh_token is None
    # explicit opt-out (tests / tooling only)
    kakao.exchange_code(db, "code", verify_state=False)
    assert route.call_count == 1


@respx.mock
def test_exchange_code_error_is_explained_without_leaking_code(db: Session) -> None:
    connect(db)
    respx.post(kakao.TOKEN_URL).mock(
        return_value=httpx.Response(
            400,
            json={
                "error": "invalid_grant",
                "error_description": "authorization code not found for code=secret-code",
                "error_code": "KOE320",
            },
        )
    )
    with pytest.raises(kakao.KakaoError, match="already used") as info:
        kakao.exchange_code(db, "secret-code", issue_state(db))
    assert not isinstance(info.value, kakao.KakaoAuthError)
    assert "secret-code" not in str(info.value)
    # existing tokens are untouched
    assert get_secret("kakao_refresh_token", db) == "r1"


@respx.mock
def test_exchange_code_redirect_mismatch(db: Session) -> None:
    connect(db)
    respx.post(kakao.TOKEN_URL).mock(
        return_value=httpx.Response(
            400,
            json={"error": "invalid_grant", "error_description": "x", "error_code": "KOE006"},
        )
    )
    with pytest.raises(kakao.KakaoError, match="redirect URI"):
        kakao.exchange_code(db, "c", issue_state(db))


# --------------------------------------------------------------------------- tokens


@respx.mock
def test_fresh_token_is_used_without_refresh(db: Session) -> None:
    connect(db)
    route = respx.post(kakao.TOKEN_URL)
    assert kakao.ensure_access_token(db) == "a1"
    assert route.call_count == 0


@respx.mock
def test_expiring_token_is_refreshed_and_rotated_refresh_token_persisted(db: Session) -> None:
    connect(db, access_in=timedelta(minutes=5))
    route = respx.post(kakao.TOKEN_URL).mock(return_value=token_response("a2", "r2"))
    assert kakao.ensure_access_token(db) == "a2"
    assert form(route.calls.last.request) == {
        "grant_type": "refresh_token",
        "client_id": "rest-key",
        "refresh_token": "r1",
        "client_secret": "client-secret",
    }
    cfg = fresh(db).notifications.kakao
    assert cfg.refresh_token.get_secret_value() == "r2"
    assert cfg.access_token.get_secret_value() == "a2"
    assert cfg.refresh_expires_at - utcnow() > timedelta(days=59)


@respx.mock
def test_refresh_without_rotation_keeps_refresh_token(db: Session) -> None:
    connect(db, access_in=timedelta(minutes=-1))
    respx.post(kakao.TOKEN_URL).mock(return_value=token_response("a2"))
    assert kakao.ensure_access_token(db) == "a2"
    cfg = fresh(db).notifications.kakao
    assert cfg.refresh_token.get_secret_value() == "r1"
    assert cfg.refresh_expires_at - utcnow() < timedelta(days=51)


@respx.mock
def test_concurrent_refresh_runs_once_under_lock(db: Session) -> None:
    connect(db, access_in=timedelta(minutes=-1))
    calls: list[dict[str, str]] = []

    def slow_token(request: httpx.Request) -> httpx.Response:
        calls.append(form(request))
        time.sleep(0.2)  # widen the race window
        return token_response("a2", "r2")

    respx.post(kakao.TOKEN_URL).mock(side_effect=slow_token)
    results: list[str] = []
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            with session_scope() as session:
                results.append(kakao.ensure_access_token(session))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert not errors
    assert results == ["a2"] * 4
    assert len(calls) == 1 and calls[0]["refresh_token"] == "r1"
    assert get_secret("kakao_refresh_token", fresh_session(db)) == "r2"


def fresh_session(db: Session) -> Session:
    db.expire_all()
    return db


@respx.mock
def test_dead_refresh_token_clears_tokens(db: Session) -> None:
    connect(db, access_in=timedelta(minutes=-1))
    respx.post(kakao.TOKEN_URL).mock(
        return_value=httpx.Response(
            400,
            json={
                "error": "invalid_grant",
                "error_description": "Invalid refresh token: r1",
                "error_code": "KOE322",
            },
        )
    )
    with pytest.raises(kakao.KakaoAuthError, match="reconnect") as info:
        kakao.ensure_access_token(db)
    assert "r1" not in str(info.value).replace("reconnect", "")
    rs = fresh(db)
    assert rs.notifications.kakao.refresh_token is None
    assert rs.notifications.kakao.access_token is None
    assert get_channel("kakao").is_configured(rs)[1].startswith("not connected")


def test_not_connected(db: Session) -> None:
    connect(db, access=None, refresh=None)
    with pytest.raises(kakao.KakaoAuthError, match="not connected"):
        kakao.ensure_access_token(db)


def test_is_configured_states(db: Session) -> None:
    channel = get_channel("kakao")
    assert channel.is_configured(load_runtime_settings(db))[1].startswith("REST API key")
    rs = connect(db, refresh=None, access_in=timedelta(minutes=-5))
    assert channel.is_configured(rs) == (
        False,
        "authorization expired - reconnect KakaoTalk in Settings",
    )
    rs = connect(db, refresh="r1", access_in=timedelta(minutes=-5))
    assert channel.is_configured(rs) == (True, "")
    assert "no push alert" in channel.description


# --------------------------------------------------------------------------- sending


def memo_ok() -> httpx.Response:
    return httpx.Response(200, json={"result_code": 0})


@respx.mock
def test_send_text_template_always_has_link(db: Session) -> None:
    rs = connect(db)
    route = respx.post(kakao.MEMO_SEND_URL).mock(return_value=memo_ok())
    get_channel("kakao").send(MESSAGE, rs)
    request = route.calls.last.request
    assert request.headers["Authorization"] == "Bearer a1"
    assert request.headers["Content-Type"] == FORM_CONTENT_TYPE
    template = json.loads(form(request)["template_object"])
    assert template == {
        "object_type": "text",
        "text": "Flight Price Alert\n" + MESSAGE.data["summary"],
        "link": {"web_url": MESSAGE.url, "mobile_web_url": MESSAGE.url},
        "button_title": "Open booking",
    }


@respx.mock
def test_send_korean_body_is_pure_ascii_and_declares_utf8(db: Session) -> None:
    """Korean survives whatever form charset Kakao assumes: the JSON is \\u-escaped."""
    rs = connect(db)
    route = respx.post(kakao.MEMO_SEND_URL).mock(return_value=memo_ok())
    korean = NotificationMessage(
        title="항공권 가격 알림",
        body="서울 (ICN) ↔ 도쿄 (NRT)",
        url="https://flight.naver.com/x",
        url_label="예약 페이지 열기",
    )
    get_channel("kakao").send(korean, rs)
    request = route.calls.last.request
    assert request.headers["Content-Type"] == FORM_CONTENT_TYPE
    request.content.decode("ascii")  # no raw UTF-8 bytes on the wire
    raw_json = form(request)["template_object"]
    assert raw_json.isascii() and "\\ud56d\\uacf5\\uad8c" in raw_json  # 항공권
    template = json.loads(raw_json)
    assert template["text"] == "항공권 가격 알림\n서울 (ICN) ↔ 도쿄 (NRT)"
    assert template["button_title"] == "예약 페이지 열기"


@respx.mock
def test_send_without_http_url_links_to_app_and_truncates(db: Session) -> None:
    rs = connect(db)
    route = respx.post(kakao.MEMO_SEND_URL).mock(return_value=memo_ok())
    message = NotificationMessage(title="T", body="word " * 100, url=None, url_label="열기")
    get_channel("kakao").send(message, rs)
    template = json.loads(form(route.calls.last.request)["template_object"])
    assert template["link"] == {
        "web_url": "http://127.0.0.1:8765/",
        "mobile_web_url": "http://127.0.0.1:8765/",
    }
    assert len(template["text"]) <= 200 and template["text"].endswith("…")
    assert template["button_title"] == "열기"


@respx.mock
def test_send_retries_once_after_401_with_refreshed_token(db: Session) -> None:
    rs = connect(db)
    memo = respx.post(kakao.MEMO_SEND_URL).mock(
        side_effect=[
            httpx.Response(401, json={"msg": "this access token does not exist", "code": -401}),
            memo_ok(),
        ]
    )
    token = respx.post(kakao.TOKEN_URL).mock(return_value=token_response("a2", "r2"))
    get_channel("kakao").send(MESSAGE, rs)
    assert token.call_count == 1
    assert [c.request.headers["Authorization"] for c in memo.calls] == ["Bearer a1", "Bearer a2"]
    assert get_secret("kakao_refresh_token", fresh_session(db)) == "r2"


@respx.mock
def test_send_gives_up_after_second_401(db: Session) -> None:
    rs = connect(db)
    respx.post(kakao.MEMO_SEND_URL).mock(
        return_value=httpx.Response(401, json={"msg": "ip mismatched", "code": -401})
    )
    token = respx.post(kakao.TOKEN_URL).mock(return_value=token_response("a2"))
    with pytest.raises(ChannelError, match="-401"):
        get_channel("kakao").send(MESSAGE, rs)
    assert token.call_count == 1


@pytest.mark.parametrize(
    ("status", "payload", "expected"),
    [
        (403, {"msg": "insufficient scopes.", "code": -402}, "was not granted"),
        (
            403,
            {
                "msg": "App disabled [talk_message] scopes for [TALK_MEMO_SEND] API.",
                "code": -3,
            },
            "consent item is not enabled",
        ),
        (400, {"msg": "failed to parse parameter. name=template_object", "code": -2}, "template"),
        (403, {"msg": "not a kakaotalk user", "code": -501}, "restricted"),
    ],
)
@respx.mock
def test_send_error_codes(db: Session, status: int, payload: dict, expected: str) -> None:
    rs = connect(db)
    respx.post(kakao.MEMO_SEND_URL).mock(return_value=httpx.Response(status, json=payload))
    token = respx.post(kakao.TOKEN_URL)
    with pytest.raises(kakao.KakaoError, match=expected) as info:
        get_channel("kakao").send(MESSAGE, rs)
    assert info.value.code == payload["code"]
    assert token.call_count == 0
