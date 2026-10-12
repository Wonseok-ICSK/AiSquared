"""Telegram, ntfy, e-mail and desktop channels (HTTP via respx, smtplib/windows_toasts faked)."""

from __future__ import annotations

import importlib.machinery
import json
import smtplib
import sys
import types
from collections.abc import Iterator
from email.message import EmailMessage
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy.orm import Session

from app.services.events import NotificationMessage
from app.services.notifications import desktop, email, ntfy, telegram
from app.services.notifications.base import (
    CHANNELS,
    ChannelError,
    get_channel,
    list_channels,
    register_channel,
)
from app.settings_store import RuntimeSettings, load_runtime_settings, save_runtime_settings

TOKEN = "123456:ABC-secret-token"
TG = f"https://api.telegram.org/bot{TOKEN}"

MESSAGE = NotificationMessage(
    title="항공권 가격 알림",
    body="서울 (ICN) ↔ 도쿄 (NRT)\n\n현재 가격:\nKRW 278,000",
    url="https://example.com/book?id=1",
    url_label="예약 페이지 열기",
    data={
        "kind": "price_alert",
        "subject": "항공권 가격 알림: ICN ↔ NRT KRW 278,000 (목표가 이하)",
        "summary": "서울 (ICN) ↔ 도쿄 (NRT)\n현재 KRW 278,000",
    },
)


def configure(db: Session, **notifications: Any) -> RuntimeSettings:
    return save_runtime_settings(db, {"notifications": notifications})


CANONICAL_ORDER = ["inapp", "desktop", "telegram", "email", "ntfy", "kakao"]


def test_registry_order() -> None:
    assert [c.key for c in list_channels()] == CANONICAL_ORDER
    assert list(CHANNELS) == CANONICAL_ORDER  # the exported dict itself, not only list_channels
    assert get_channel("kakao").description.startswith("Records to KakaoTalk")
    with pytest.raises(KeyError, match="inapp, desktop, telegram, email, ntfy, kakao"):
        get_channel("sms")


def test_register_channel_keeps_canonical_order_whatever_the_import_order() -> None:
    saved = dict(CHANNELS)
    try:
        CHANNELS.clear()
        for key in reversed(CANONICAL_ORDER):
            register_channel(saved[key])
        assert list(CHANNELS) == CANONICAL_ORDER
        assert all(CHANNELS[key] is saved[key] for key in CANONICAL_ORDER)
        with pytest.raises(ValueError, match="unknown notification channel"):
            register_channel(type("Sms", (), {"key": "sms"})())  # type: ignore[arg-type]
    finally:
        CHANNELS.clear()
        CHANNELS.update(saved)


# --------------------------------------------------------------------------- telegram


def _ok(result: Any = None) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "result": result if result is not None else {}})


@respx.mock
def test_telegram_send_plain_text_with_url_button() -> None:
    route = respx.post(f"{TG}/sendMessage").mock(return_value=_ok())
    telegram.send_message(TOKEN, "42", "hello", url=MESSAGE.url, url_label="Book")
    body = json.loads(route.calls.last.request.content)
    assert body["chat_id"] == "42" and body["text"] == "hello"
    assert "parse_mode" not in body
    assert body["reply_markup"] == {"inline_keyboard": [[{"text": "Book", "url": MESSAGE.url}]]}
    assert body["link_preview_options"] == {"is_disabled": True}


@respx.mock
def test_telegram_local_or_non_http_url_is_appended_as_text() -> None:
    route = respx.post(f"{TG}/sendMessage").mock(return_value=_ok())
    telegram.send_message(TOKEN, "42", "hi", url="http://127.0.0.1:8765/trips/1", url_label="Open")
    body = json.loads(route.calls.last.request.content)
    assert "reply_markup" not in body
    assert body["text"] == "hi\n\nOpen: http://127.0.0.1:8765/trips/1"
    telegram.send_message(TOKEN, "42", "hi", url="ftp://x/y", url_label="Open")
    assert json.loads(route.calls.last.request.content)["text"].endswith("Open: ftp://x/y")


@respx.mock
def test_telegram_retries_without_button_when_rejected() -> None:
    route = respx.post(f"{TG}/sendMessage").mock(
        side_effect=[
            httpx.Response(
                400,
                json={
                    "ok": False,
                    "error_code": 400,
                    "description": "Bad Request: inline keyboard button URL is invalid",
                },
            ),
            _ok(),
        ]
    )
    telegram.send_message(TOKEN, "42", "hi", url="https://example.com/x", url_label="Open")
    assert route.call_count == 2
    second = json.loads(route.calls[1].request.content)
    assert "reply_markup" not in second and second["text"].endswith("Open: https://example.com/x")


@respx.mock
def test_telegram_text_is_truncated_to_4096() -> None:
    route = respx.post(f"{TG}/sendMessage").mock(return_value=_ok())
    telegram.send_message(TOKEN, "42", "word " * 2000, url="http://localhost/x", url_label="Open")
    text = json.loads(route.calls.last.request.content)["text"]
    assert len(text) <= 4096 and text.endswith("Open: http://localhost/x")


@pytest.mark.parametrize(
    ("status", "payload", "expected"),
    [
        (401, {"ok": False, "error_code": 401, "description": "Unauthorized"}, "invalid bot token"),
        (404, {"ok": False, "error_code": 404, "description": "Not Found"}, "invalid bot token"),
        (
            403,
            {"ok": False, "error_code": 403, "description": "Forbidden: bot was blocked"},
            "press Start",
        ),
        (
            400,
            {"ok": False, "error_code": 400, "description": "Bad Request: chat not found"},
            "chat not found",
        ),
        (
            429,
            {"ok": False, "error_code": 429, "parameters": {"retry_after": 7}},
            "retry after 7 s",
        ),
    ],
)
@respx.mock
def test_telegram_errors_are_explained(status: int, payload: dict, expected: str) -> None:
    respx.post(f"{TG}/sendMessage").mock(return_value=httpx.Response(status, json=payload))
    with pytest.raises(ChannelError, match=expected) as info:
        telegram.send_message(TOKEN, "42", "hi")
    assert TOKEN not in str(info.value)


@respx.mock
def test_telegram_network_error_redacts_token() -> None:
    respx.post(f"{TG}/sendMessage").mock(side_effect=httpx.ConnectError(f"boom {TG}/sendMessage"))
    with pytest.raises(ChannelError) as info:
        telegram.send_message(TOKEN, "42", "hi")
    assert TOKEN not in str(info.value)
    assert "cannot reach Telegram" in str(info.value)


@respx.mock
def test_detect_chat_id_prefers_newest_private_chat() -> None:
    updates = [
        {"update_id": 1, "message": {"chat": {"id": 111, "type": "private", "username": "old"}}},
        {"update_id": 2, "message": {"chat": {"id": -5, "type": "group", "title": "Family"}}},
        {
            "update_id": 3,
            "message": {
                "chat": {"id": 222, "type": "private", "first_name": "Min", "last_name": "Kim"}
            },
        },
        {"update_id": 4, "my_chat_member": {"chat": {"id": -9, "type": "group", "title": "G"}}},
    ]
    respx.post(f"{TG}/getUpdates").mock(return_value=_ok(updates))
    assert telegram.detect_chat_id(TOKEN) == {"chat_id": "222", "username": "Min Kim"}


@respx.mock
def test_detect_chat_id_without_updates_explains_start_and_24h() -> None:
    respx.post(f"{TG}/getUpdates").mock(return_value=_ok([]))
    with pytest.raises(ChannelError, match=r"/start.*24 h"):
        telegram.detect_chat_id(TOKEN)


@respx.mock
def test_detect_chat_id_webhook_conflict() -> None:
    respx.post(f"{TG}/getUpdates").mock(
        return_value=httpx.Response(
            409,
            json={"ok": False, "error_code": 409, "description": "Conflict: webhook is active"},
        )
    )
    with pytest.raises(ChannelError, match="webhook"):
        telegram.detect_chat_id(TOKEN)


def test_detect_chat_id_requires_token() -> None:
    with pytest.raises(ChannelError, match="BotFather"):
        telegram.detect_chat_id("  ")


@respx.mock
def test_telegram_channel_uses_runtime_settings(db: Session) -> None:
    channel = get_channel("telegram")
    rs = load_runtime_settings(db)
    assert channel.is_configured(rs)[1] == "bot token is not set (create a bot with @BotFather)"
    rs = configure(db, telegram={"enabled": True, "bot_token": TOKEN})
    assert channel.is_configured(rs)[0] is False
    rs = configure(db, telegram={"chat_id": "42"})
    assert channel.is_enabled(rs) and channel.is_configured(rs) == (True, "")
    route = respx.post(f"{TG}/sendMessage").mock(return_value=_ok())
    channel.send(MESSAGE, rs)
    body = json.loads(route.calls.last.request.content)
    assert body["text"].startswith("항공권 가격 알림\n\n서울 (ICN) ↔ 도쿄 (NRT)")
    assert body["reply_markup"]["inline_keyboard"][0][0]["text"] == "예약 페이지 열기"


# --------------------------------------------------------------------------- ntfy


@respx.mock
def test_ntfy_publishes_json_with_korean_title_and_token(db: Session) -> None:
    rs = configure(db, ntfy={"enabled": True, "topic": "aisq-test_123", "token": "tk_secret"})
    channel = get_channel("ntfy")
    assert channel.is_configured(rs) == (True, "")
    route = respx.post("https://ntfy.sh/").mock(
        return_value=httpx.Response(200, json={"id": "abc", "event": "message"})
    )
    channel.send(MESSAGE, rs)
    request = route.calls.last.request
    assert request.headers["Authorization"] == "Bearer tk_secret"
    assert "title" not in {k.lower() for k in request.headers}
    body = json.loads(request.content)
    assert body == {
        "topic": "aisq-test_123",
        "title": MESSAGE.data["subject"],
        "message": MESSAGE.body,
        "tags": ["airplane"],
        "priority": 4,
        "click": MESSAGE.url,
        "actions": [{"action": "view", "label": "예약 페이지 열기", "url": MESSAGE.url}],
    }


@respx.mock
def test_ntfy_self_hosted_server_without_link_or_token() -> None:
    route = respx.post("https://ntfy.example.org/").mock(return_value=httpx.Response(200, json={}))
    message = NotificationMessage(title="Test", body="Body", url=None, data={"kind": "system"})
    ntfy.publish("https://ntfy.example.org/", "topic", message)
    request = route.calls.last.request
    body = json.loads(request.content)
    assert "Authorization" not in request.headers
    assert "click" not in body and "actions" not in body
    assert body["priority"] == 3 and body["tags"] == ["bell"]


def test_ntfy_message_is_capped_at_4096_bytes() -> None:
    message = NotificationMessage(title="T", body="가" * 3000)
    payload = ntfy.build_payload("t", message)
    assert len(payload["message"].encode("utf-8")) <= 4096


@pytest.mark.parametrize(
    ("status", "expected"),
    [(429, "rate limited"), (403, "access denied"), (400, "invalid topic")],
)
@respx.mock
def test_ntfy_errors(status: int, expected: str) -> None:
    respx.post("https://ntfy.sh/").mock(
        return_value=httpx.Response(status, json={"http": status, "error": "invalid topic"})
    )
    with pytest.raises(ChannelError, match=expected) as info:
        ntfy.publish("https://ntfy.sh", "t", MESSAGE, token="tk_secret")
    assert "tk_secret" not in str(info.value)


def test_ntfy_configuration_checks(db: Session) -> None:
    channel = get_channel("ntfy")
    assert channel.is_configured(load_runtime_settings(db)) == (False, "topic is not set")
    rs = configure(db, ntfy={"topic": "bad topic!"})
    assert "only contain" in channel.is_configured(rs)[1]
    rs = configure(db, ntfy={"topic": "ok", "server": "ftp://x"})
    assert channel.is_configured(rs) == (False, "server must be an http(s) URL")


# --------------------------------------------------------------------------- email


class FakeSMTP:
    """Stand-in for smtplib.SMTP / SMTP_SSL recording the conversation."""

    instances: list[FakeSMTP] = []
    fail_login = False
    no_starttls = False  # EHLO without STARTTLS (smtplib raises SMTPNotSupportedError)
    no_auth = False  # EHLO without AUTH (login raises SMTPNotSupportedError)
    no_smtputf8 = False  # send_message needs SMTPUTF8 the server lacks

    def __init__(self, host: str, port: int, timeout: float | None = None, **kwargs: Any) -> None:
        self.host, self.port, self.timeout, self.kwargs = host, port, timeout, kwargs
        self.calls: list[Any] = []
        self.sent: list[EmailMessage] = []
        FakeSMTP.instances.append(self)

    def __enter__(self) -> FakeSMTP:
        return self

    def __exit__(self, *exc: object) -> None:
        self.calls.append("quit")

    def ehlo(self) -> None:
        self.calls.append("ehlo")

    def starttls(self, context: Any = None) -> None:
        self.calls.append("starttls")
        if FakeSMTP.no_starttls:
            raise smtplib.SMTPNotSupportedError("STARTTLS extension not supported by server.")

    def login(self, user: str, password: str) -> None:
        self.calls.append(("login", user, password))
        if FakeSMTP.no_auth:
            raise smtplib.SMTPNotSupportedError("SMTP AUTH extension not supported by server.")
        if FakeSMTP.fail_login:
            raise smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted")

    def send_message(self, message: EmailMessage) -> None:
        self.calls.append("send")
        if FakeSMTP.no_smtputf8:
            raise smtplib.SMTPNotSupportedError(
                "One or more source or delivery addresses require internationalized "
                "email support, but the server does not advertise the required SMTPUTF8 "
                "capability"
            )
        self.sent.append(message)


class FakeSMTPSSL(FakeSMTP):
    pass


@pytest.fixture
def fake_smtp(monkeypatch: pytest.MonkeyPatch) -> Iterator[type[FakeSMTP]]:
    FakeSMTP.instances = []
    FakeSMTP.fail_login = False
    FakeSMTP.no_starttls = FakeSMTP.no_auth = FakeSMTP.no_smtputf8 = False
    monkeypatch.setattr(email.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(email.smtplib, "SMTP_SSL", FakeSMTPSSL)
    yield FakeSMTP


def _gmail(db: Session, **extra: Any) -> RuntimeSettings:
    preset = email.PRESETS["gmail"]
    return configure(
        db,
        email={
            "enabled": True,
            "smtp_host": preset.host,
            "smtp_port": preset.port,
            "username": "me@gmail.com",
            "password": "abcd efgh ijkl mnop",
            "to_addrs": ["me@gmail.com", "you@naver.com"],
        }
        | extra,
    )


def test_email_starttls_login_and_multipart(db: Session, fake_smtp: type[FakeSMTP]) -> None:
    rs = _gmail(db)
    channel = get_channel("email")
    assert channel.is_configured(rs) == (True, "")
    channel.send(MESSAGE, rs)
    smtp = fake_smtp.instances[-1]
    assert (smtp.host, smtp.port, smtp.timeout) == ("smtp.gmail.com", 587, 20.0)
    assert smtp.calls == [
        "ehlo",
        "starttls",
        "ehlo",
        ("login", "me@gmail.com", "abcd efgh ijkl mnop"),
        "send",
        "quit",
    ]
    mail = smtp.sent[0]
    assert mail["Subject"] == MESSAGE.data["subject"]
    assert mail["From"] == "AiSquared Travel <me@gmail.com>"
    assert mail["To"] == "me@gmail.com, you@naver.com"
    assert mail.get_content_type() == "multipart/alternative"
    plain = mail.get_body(("plain",)).get_content()
    html_part = mail.get_body(("html",)).get_content()
    assert plain.startswith("항공권 가격 알림\n\n서울 (ICN) ↔ 도쿄 (NRT)")
    assert f"예약 페이지 열기: {MESSAGE.url}" in plain
    assert f'href="{MESSAGE.url}"' in html_part
    assert "<strong>현재 가격:</strong>" in html_part


def test_email_port_465_uses_implicit_tls(db: Session, fake_smtp: type[FakeSMTP]) -> None:
    rs = _gmail(db, smtp_port=465)
    get_channel("email").send(MESSAGE, rs)
    smtp = fake_smtp.instances[-1]
    assert isinstance(smtp, FakeSMTPSSL) and "context" in smtp.kwargs
    assert "starttls" not in smtp.calls


def test_email_auth_failure_explains_app_password(db: Session, fake_smtp: type[FakeSMTP]) -> None:
    rs = _gmail(db)
    fake_smtp.fail_login = True
    with pytest.raises(ChannelError, match="app password") as info:
        get_channel("email").send(MESSAGE, rs)
    assert "abcd efgh" not in str(info.value)


def test_email_server_without_starttls(db: Session, fake_smtp: type[FakeSMTP]) -> None:
    fake_smtp.no_starttls = True
    with pytest.raises(ChannelError, match="smtp.gmail.com:587 does not support STARTTLS"):
        get_channel("email").send(MESSAGE, _gmail(db))
    assert not any(isinstance(call, tuple) for call in fake_smtp.instances[-1].calls)  # no login


def test_email_login_unsupported_without_starttls_is_not_blamed_on_starttls(
    db: Session, fake_smtp: type[FakeSMTP]
) -> None:
    # Gmail/Naver on 587 advertise AUTH only after STARTTLS
    fake_smtp.no_auth = True
    with pytest.raises(ChannelError) as info:
        get_channel("email").send(MESSAGE, _gmail(db, use_starttls=False))
    reason = str(info.value)
    assert "does not support STARTTLS" not in reason
    assert reason == (
        "the SMTP server does not offer login (AUTH) on this connection - "
        "turn on STARTTLS (port 587) or use port 465 (SSL)"
    )
    assert "starttls" not in fake_smtp.instances[-1].calls
    # with STARTTLS already on, the hint points elsewhere
    with pytest.raises(ChannelError, match="SMTP access is enabled") as info:
        get_channel("email").send(MESSAGE, _gmail(db, use_starttls=True))
    assert "abcd efgh" not in str(info.value)


def test_email_smtputf8_not_supported_is_a_generic_smtp_error(
    db: Session, fake_smtp: type[FakeSMTP]
) -> None:
    fake_smtp.no_smtputf8 = True
    with pytest.raises(ChannelError, match="^SMTP error: SMTPNotSupportedError: .*SMTPUTF8"):
        get_channel("email").send(MESSAGE, _gmail(db))


def test_email_connection_error(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_args: Any, **_kwargs: Any) -> None:
        raise ConnectionRefusedError(10061, "refused")

    monkeypatch.setattr(email.smtplib, "SMTP", refuse)
    with pytest.raises(ChannelError, match="cannot connect to smtp.gmail.com:587"):
        get_channel("email").send(MESSAGE, _gmail(db))


def test_email_configuration_checks_and_naver_sender(db: Session) -> None:
    channel = get_channel("email")
    assert channel.is_configured(load_runtime_settings(db))[1].startswith("SMTP server")
    rs = configure(db, email={"smtp_host": "smtp.naver.com", "username": "minkim"})
    assert channel.is_configured(rs) == (False, "no recipient address")
    rs = configure(db, email={"to_addrs": ["minkim@naver.com"]})
    assert channel.is_configured(rs) == (False, "SMTP password (app password) is not set")
    rs = configure(db, email={"password": "app-pw"})
    assert channel.is_configured(rs) == (True, "")
    assert email.resolve_sender(rs.notifications.email) == "minkim@naver.com"
    assert email.PRESETS["naver"].host == "smtp.naver.com" and email.PRESETS["naver"].port == 587


# --------------------------------------------------------------------------- desktop


class FakeToast:
    def __init__(self, text_fields: list[str], group: str, launch_action: str | None, actions: Any):
        self.text_fields, self.group, self.launch_action = text_fields, group, launch_action
        self.actions = list(actions)


class FakeButton:
    def __init__(self, content: str, launch: str | None = None) -> None:
        self.content, self.launch = content, launch


class FakeToaster:
    shown: list[tuple[FakeToaster, FakeToast]] = []
    fail = False

    def __init__(self, text: str, notifierAUMID: str | None = None) -> None:  # noqa: N803
        self.application_text, self.aumid = text, notifierAUMID

    def show_toast(self, toast: FakeToast) -> None:
        if FakeToaster.fail:
            raise OSError("Element not found")
        FakeToaster.shown.append((self, toast))


@pytest.fixture
def fake_toasts(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    module = types.ModuleType("windows_toasts")
    module.__spec__ = importlib.machinery.ModuleSpec("windows_toasts", None)
    module.Toast = FakeToast  # type: ignore[attr-defined]
    module.ToastButton = FakeButton  # type: ignore[attr-defined]
    module.InteractableWindowsToaster = FakeToaster  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "windows_toasts", module)
    monkeypatch.setattr(desktop, "_is_windows", lambda: True)
    monkeypatch.setattr(desktop, "_registered", False)
    registrations: list[str] = []
    monkeypatch.setattr(
        desktop, "_write_aumid_key", lambda name, icon: registrations.append(name)
    )
    FakeToaster.shown = []
    FakeToaster.fail = False
    yield registrations


def test_desktop_toast_opens_url_and_registers_aumid_once(
    db: Session, fake_toasts: list[str]
) -> None:
    rs = load_runtime_settings(db)
    channel = get_channel("desktop")
    assert channel.is_enabled(rs) and channel.is_configured(rs) == (True, "")
    channel.send(MESSAGE, rs)
    channel.send(MESSAGE, rs)
    assert fake_toasts == ["AiSquared Travel"]
    toaster, toast = FakeToaster.shown[0]
    assert toaster.aumid == "AiSquared.Travel" and toaster.application_text == "AiSquared Travel"
    assert toast.text_fields == ["항공권 가격 알림", MESSAGE.data["summary"]]
    assert toast.launch_action == MESSAGE.url
    assert toast.actions[0].content == "예약 페이지 열기" and toast.actions[0].launch == MESSAGE.url


def test_desktop_toast_without_http_url_has_no_launch(db: Session, fake_toasts: list[str]) -> None:
    message = NotificationMessage(title="T", body="B", url="file:///c:/x")
    get_channel("desktop").send(message, load_runtime_settings(db))
    _, toast = FakeToaster.shown[0]
    assert toast.launch_action is None and toast.actions == []


def test_desktop_failure_becomes_channel_error(db: Session, fake_toasts: list[str]) -> None:
    FakeToaster.fail = True
    with pytest.raises(ChannelError, match="toast failed: OSError"):
        get_channel("desktop").send(MESSAGE, load_runtime_settings(db))


def test_desktop_not_available_off_windows(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(desktop, "_is_windows", lambda: False)
    channel = get_channel("desktop")
    rs = load_runtime_settings(db)
    assert channel.is_configured(rs) == (False, "desktop toasts are only available on Windows")
    assert desktop.register_aumid() is False
    with pytest.raises(ChannelError, match="only available on Windows"):
        channel.send(MESSAGE, rs)


def test_register_aumid_failure_is_not_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(desktop, "_is_windows", lambda: True)
    monkeypatch.setattr(desktop, "_registered", False)

    def deny(_name: str, _icon: Any) -> None:
        raise PermissionError("denied")

    monkeypatch.setattr(desktop, "_write_aumid_key", deny)
    assert desktop.register_aumid() is False
