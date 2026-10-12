"""Live checks against the real services (skipped unless RUN_LIVE=1; tiny volume).

No credentials are needed: ntfy.sh publishes to a random throwaway topic; Telegram and
Kakao are called with deliberately invalid credentials to verify the error mapping;
SMTP presets only get an EHLO/STARTTLS handshake (no login). The Windows toast test
additionally needs RUN_LIVE_TOAST=1 because it pops up a real notification.
"""

from __future__ import annotations

import json
import os
import secrets
import smtplib
import ssl
import time

import httpx
import pytest

from app.services.events import NotificationMessage
from app.services.notifications import desktop, email, kakao, ntfy, telegram
from app.services.notifications.base import ChannelError
from app.services.notifications.formatter import format_test_message

pytestmark = pytest.mark.live

KOREAN = NotificationMessage(
    title="항공권 가격 알림",
    body="서울 (ICN) ↔ 도쿄 (NRT)\n현재 가격: KRW 278,000",
    url="https://example.com/book",
    url_label="예약 페이지 열기",
    data={"kind": "price_alert", "subject": "항공권 가격 알림: ICN ↔ NRT KRW 278,000"},
)


def _poll_ntfy(topic: str, attempts: int = 6) -> list[dict]:
    """Cached messages of ``topic``; ntfy.sh may need a moment before a poll sees a publish."""
    messages: list[dict] = []
    for attempt in range(attempts):
        if attempt:
            time.sleep(0.5 * attempt)  # 0.5, 1.0, 1.5, ... s (7.5 s in total at most)
        polled = httpx.get(
            f"{ntfy.DEFAULT_SERVER}/{topic}/json",
            params={"poll": "1", "since": "all"},
            timeout=15,
        )
        polled.raise_for_status()
        messages = [json.loads(line) for line in polled.text.splitlines() if line.strip()]
        messages = [m for m in messages if m.get("event") == "message"]
        if messages:
            break
    return messages


def test_ntfy_publish_json_roundtrip() -> None:
    topic = f"aisq-live-{secrets.token_hex(8)}"
    reply = ntfy.publish(ntfy.DEFAULT_SERVER, topic, KOREAN)
    assert reply["event"] == "message" and reply["topic"] == topic
    # The publish reply already echoes what ntfy stored ...
    assert reply["title"] == KOREAN.data["subject"]
    assert reply["click"] == KOREAN.url and reply["message"] == KOREAN.body
    # ... and a subscriber polling the topic sees the same (retried: the poll can lag).
    messages = _poll_ntfy(topic)
    assert messages, "message not visible to a poll after ~7.5 s"
    assert messages[-1]["id"] == reply["id"]
    assert messages[-1]["title"] == KOREAN.data["subject"]
    assert messages[-1]["click"] == KOREAN.url
    assert messages[-1]["message"] == KOREAN.body


def test_telegram_invalid_token_is_explained() -> None:
    with pytest.raises(ChannelError, match="invalid bot token") as info:
        telegram.detect_chat_id("123456789:AAinvalid-token-for-aisquared-live-test")
    assert "AAinvalid" not in str(info.value)


def test_kakao_invalid_access_token_returns_minus_401() -> None:
    template = kakao.build_template("live check", "https://example.com/", "Open")
    with pytest.raises(kakao.KakaoError) as info:
        kakao.send_memo("invalid-access-token", template)
    assert info.value.code == -401


def test_kakao_token_endpoint_error_is_parsed() -> None:
    with pytest.raises(kakao.KakaoError) as info:
        kakao._token_request(  # noqa: SLF001 - verify the real error payload shape
            {
                "grant_type": "authorization_code",
                "client_id": "0" * 32,
                "redirect_uri": "http://127.0.0.1:8765/api/integrations/kakao/callback",
                "code": "invalid-code",
            },
            "invalid-code",
        )
    assert str(info.value.code).startswith("KOE") or info.value.code
    assert "invalid-code" not in str(info.value)


@pytest.mark.parametrize("preset", sorted(email.PRESETS))
def test_smtp_preset_offers_starttls_and_auth(preset: str) -> None:
    cfg = email.PRESETS[preset]
    with smtplib.SMTP(cfg.host, cfg.port, timeout=email.SMTP_TIMEOUT_S) as smtp:
        smtp.ehlo()
        assert smtp.has_extn("starttls")
        smtp.starttls(context=ssl.create_default_context())
        smtp.ehlo()
        assert smtp.has_extn("auth")


@pytest.mark.skipif(os.environ.get("RUN_LIVE_TOAST") != "1", reason="set RUN_LIVE_TOAST=1")
def test_windows_toast_with_registered_aumid() -> None:
    from winrt.windows.ui.notifications import ToastNotificationManager

    desktop.DesktopChannel().send(format_test_message(), None)  # type: ignore[arg-type]
    assert desktop.register_aumid() is True
    # After the first toast Windows knows the AUMID: no "Element not found" any more.
    notifier = ToastNotificationManager.create_toast_notifier_with_id(desktop.AUMID)
    assert int(notifier.setting) == 0  # NotificationSetting.ENABLED
    history = ToastNotificationManager.history.get_history_with_id(desktop.AUMID)
    assert history.size >= 1
