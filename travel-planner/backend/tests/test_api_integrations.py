"""``/api/integrations/kakao``: OAuth authorize redirect and the HTML callback page."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.services.notifications import kakao
from app.services.notifications.kakao import KakaoAuthError, KakaoConnection, KakaoError

AUTHORIZE = "/api/integrations/kakao/authorize"
CALLBACK = "/api/integrations/kakao/callback"


def configure_kakao(client: TestClient, **extra: Any) -> None:
    body = {"rest_api_key": "rest-key-123", "redirect_uri": "http://127.0.0.1:8765/cb", **extra}
    assert client.put("/api/settings", json={"notifications": {"kakao": body}}).status_code == 200


def authorize_state(client: TestClient) -> tuple[str, dict[str, list[str]]]:
    response = client.get(AUTHORIZE, follow_redirects=False)
    assert response.status_code == 302
    location = response.headers["location"]
    assert location.startswith(kakao.AUTHORIZE_URL + "?")
    params = parse_qs(urlsplit(location).query)
    return params["state"][0], params


def assert_html(response: httpx.Response, status: int, *texts: str) -> None:
    assert response.status_code == status
    assert response.headers["content-type"].startswith("text/html")
    for text in texts:
        assert text in response.text


def test_authorize_redirects_to_kakao(client: TestClient) -> None:
    configure_kakao(client)
    state, params = authorize_state(client)
    assert params["client_id"] == ["rest-key-123"]
    assert params["redirect_uri"] == ["http://127.0.0.1:8765/cb"]
    assert params["scope"] == ["talk_message"] and params["response_type"] == ["code"]
    assert len(state) >= 20
    assert authorize_state(client)[0] != state  # a fresh state per attempt


def test_authorize_without_rest_key_is_a_400_page(client: TestClient) -> None:
    response = client.get(AUTHORIZE, follow_redirects=False)
    assert_html(response, 400, "KakaoTalk is not set up", "REST API key")


def test_callback_success(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str | None]] = []

    def exchange(_db: Any, code: str, state: str | None = None) -> KakaoConnection:
        calls.append((code, state))
        return KakaoConnection(
            scopes=["talk_message"],
            talk_message=True,
            access_expires_at=datetime(2026, 10, 8, 18),
            refresh_expires_at=None,
        )

    monkeypatch.setattr(kakao, "exchange_code", exchange)
    response = client.get(CALLBACK, params={"code": "abc", "state": "s1"})
    assert_html(response, 200, "KakaoTalk connected — you can close this tab")
    assert "talk_message" not in response.text  # no scope warning
    assert calls == [("abc", "s1")]


def test_callback_warns_without_talk_message_scope(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection = KakaoConnection(scopes=[], talk_message=False, access_expires_at=None, refresh_expires_at=None)
    monkeypatch.setattr(kakao, "exchange_code", lambda *_a, **_k: connection)
    response = client.get(CALLBACK, params={"code": "abc", "state": "s1"})
    assert_html(response, 200, "KakaoTalk connected", "talk_message")


@pytest.mark.parametrize(
    ("error", "shown"),
    [
        (
            KakaoAuthError("invalid or expired OAuth state - click 'Connect KakaoTalk' again"),
            "invalid or expired OAuth state",
        ),
        (
            KakaoError("Kakao token error KOE320: <script>alert(1)</script>", code="KOE320"),
            "KOE320: &lt;script&gt;alert(1)&lt;/script&gt;",
        ),
    ],
)
def test_callback_failures_are_escaped_400_pages(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, error: KakaoError, shown: str
) -> None:
    def exchange(*_args: Any, **_kwargs: Any) -> KakaoConnection:
        raise error

    monkeypatch.setattr(kakao, "exchange_code", exchange)
    response = client.get(CALLBACK, params={"code": "abc", "state": "s1"})
    assert_html(response, 400, "KakaoTalk was not connected", shown)
    assert "<script>" not in response.text


def test_callback_requires_state_and_code(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(kakao, "exchange_code", lambda *_a, **_k: pytest.fail("must not exchange"))
    assert_html(client.get(CALLBACK, params={"code": "abc"}), 400, "state")
    assert_html(client.get(CALLBACK, params={"state": "s1"}), 400, "authorization code")
    denied = client.get(
        CALLBACK, params={"error": "access_denied", "error_description": "User denied", "state": "s"}
    )
    assert_html(denied, 400, "cancelled or denied")


def test_callback_unexpected_error_is_a_500_page(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def exchange(*_args: Any, **_kwargs: Any) -> KakaoConnection:
        raise RuntimeError("database is locked")

    monkeypatch.setattr(kakao, "exchange_code", exchange)
    assert_html(client.get(CALLBACK, params={"code": "c", "state": "s"}), 500, "unexpected error")


def test_full_oauth_round_trip_with_real_exchange(client: TestClient) -> None:
    configure_kakao(client, client_secret="client-secret-xyz")
    state, _params = authorize_state(client)
    tokens = {
        "access_token": "access-AAA",
        "refresh_token": "refresh-BBB",
        "expires_in": 21599,
        "refresh_token_expires_in": 5183999,
        "scope": "talk_message profile_nickname",
    }
    with respx.mock(assert_all_called=True) as router:
        route = router.post(kakao.TOKEN_URL).mock(return_value=httpx.Response(200, json=tokens))
        response = client.get(CALLBACK, params={"code": "the-code", "state": state})
    assert_html(response, 200, "KakaoTalk connected — you can close this tab")
    form = parse_qs(route.calls.last.request.content.decode())
    assert form["code"] == ["the-code"] and form["client_secret"] == ["client-secret-xyz"]
    assert form["redirect_uri"] == ["http://127.0.0.1:8765/cb"]

    cfg = client.get("/api/settings").json()["notifications"]["kakao"]
    assert cfg["enabled"] is True
    assert cfg["access_token"]["is_set"] and cfg["refresh_token"]["is_set"]
    assert cfg["access_expires_at"].endswith("Z") and cfg["refresh_expires_at"].endswith("Z")
    assert "access-AAA" not in str(cfg)

    replay = client.get(CALLBACK, params={"code": "the-code", "state": state})  # single-use state
    assert_html(replay, 400, "invalid or expired OAuth state")
