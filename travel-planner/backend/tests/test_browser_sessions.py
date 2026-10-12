"""Browser sessions: profile paths, encrypted storage_state, DB status, interactive login.

``start_login`` is exercised with fake managers/handles/contexts only: no real window opens.
"""

from __future__ import annotations

import inspect
import logging
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from cryptography.fernet import Fernet
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from sqlalchemy.orm import Session

from app.browser import sessions
from app.browser.manager import BrowserHandle, BrowserManager, BrowserUnavailableError
from app.browser.sessions import (
    LOGIN_GRACE_S,
    LOGIN_QUEUE_TIMEOUT_S,
    delete_session,
    filter_storage_state,
    load_storage_state,
    mark_session_expired,
    profile_dir,
    sanitize_key,
    save_storage_state,
    session_status,
    site_domain,
    start_login,
    state_path,
)
from app.config import Settings
from app.models import ProviderSession
from app.security.secrets import SecretBox

SESSION_COOKIE = {
    "name": "SID",
    "value": "very-secret-cookie-value",
    "domain": ".example.com",
    "path": "/",
    "expires": -1,
    "httpOnly": True,
    "secure": True,
    "sameSite": "Lax",
}
STATE = {
    "cookies": [SESSION_COOKIE],
    "origins": [
        {"origin": "https://example.com", "localStorage": [{"name": "t", "value": "토큰"}]}
    ],
}


@pytest.fixture(autouse=True)
def local_appdata(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Profiles go under tmp, never under the real %LOCALAPPDATA%."""
    path = tmp_path / "localappdata"
    monkeypatch.setenv("LOCALAPPDATA", str(path))
    return path


# --------------------------------------------------------------------------- paths


def test_sanitize_key() -> None:
    assert sanitize_key("naver") == "naver"
    assert sanitize_key(" Google Flights ") == "google_flights"
    assert sanitize_key("../../evil") == "evil"
    assert sanitize_key("CON") == "_con"
    assert len(sanitize_key("x" * 100)) == 40
    for bad in ("", "  ", "!!!", "../"):
        with pytest.raises(ValueError):
            sanitize_key(bad)


def test_profile_dir_under_localappdata(local_appdata: Path) -> None:
    path = profile_dir("Naver")
    assert path == local_appdata / "AiSquaredTravel" / "browser_profiles" / "naver"
    assert path.is_dir()


def test_profile_dir_falls_back_to_data_dir(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    monkeypatch.delenv("LOCALAPPDATA")
    assert profile_dir("naver") == settings.browser_profiles_dir / "naver"


# --------------------------------------------------------------------------- storage_state


def test_storage_state_roundtrip_is_encrypted(secretbox: SecretBox, settings: Settings) -> None:
    path = save_storage_state("naver", STATE)
    assert path == settings.sessions_dir / "naver.state.enc" == state_path("naver")
    raw = path.read_bytes()
    assert b"very-secret-cookie-value" not in raw and b"SID" not in raw
    assert secretbox.decrypt_bytes(raw).startswith(b"{")
    assert load_storage_state("naver") == STATE
    assert [p.name for p in path.parent.iterdir()] == ["naver.state.enc"]  # no temp leftovers


def test_save_replaces_previous_state() -> None:
    save_storage_state("naver", STATE)
    save_storage_state("naver", {"cookies": [], "origins": []})
    assert load_storage_state("naver") == {"cookies": [], "origins": []}
    assert len(list(state_path("naver").parent.iterdir())) == 1


def test_save_rejects_non_dict() -> None:
    with pytest.raises(TypeError):
        save_storage_state("naver", [1, 2])  # type: ignore[arg-type]


def test_load_missing_returns_none() -> None:
    assert load_storage_state("nobody") is None


def test_load_undecryptable_returns_none_and_logs(caplog: pytest.LogCaptureFixture) -> None:
    path = state_path("naver")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(SecretBox(key=Fernet.generate_key()).encrypt_bytes(b'{"cookies": []}'))
    with caplog.at_level(logging.WARNING, logger="app.browser.sessions"):
        assert load_storage_state("naver") is None
    assert "cannot be decrypted" in caplog.text


def test_load_corrupt_or_wrong_shape_returns_none(secretbox: SecretBox) -> None:
    path = state_path("naver")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(secretbox.encrypt_bytes(b"not json"))
    assert load_storage_state("naver") is None
    path.write_bytes(secretbox.encrypt_bytes(b"[1, 2]"))
    assert load_storage_state("naver") is None


# --------------------------------------------------------------------------- DB status


def _add_row(db: Session, status: str = "active") -> None:
    db.add(ProviderSession(provider="naver", status=status, storage_path=str(state_path("naver"))))
    db.commit()


def test_session_status(db: Session) -> None:
    assert session_status(db, "naver") == "none"
    _add_row(db)
    assert session_status(db, "naver") == "expired"  # active row but no state file
    save_storage_state("naver", STATE)
    assert session_status(db, "naver") == "active"
    mark_session_expired(db, "naver")
    assert session_status(db, "naver") == "expired"
    assert db.get(ProviderSession, "naver").status == "expired"  # type: ignore[union-attr]


def test_delete_session_removes_files_and_resets_row(db: Session) -> None:
    profile = profile_dir("naver")
    (profile / "Default").mkdir()
    (profile / "Default" / "Cookies").write_bytes(b"x")
    save_storage_state("naver", STATE)
    _add_row(db)
    assert delete_session(db, "naver") is True
    assert not profile.exists() and not state_path("naver").exists()
    row = db.get(ProviderSession, "naver")
    assert row is not None and row.status == "none" and row.storage_path is None
    assert session_status(db, "naver") == "none"
    assert delete_session(db, "unknown") is True  # nothing to delete is fine


# --------------------------------------------------------------------------- start_login fakes


class FakeDialog:
    def __init__(self) -> None:
        self.dismissed = False

    def dismiss(self) -> None:
        self.dismissed = True


class FakeLoginPage:
    def __init__(self, context: FakeLoginContext) -> None:
        self.context = context
        self.closed = False
        self.visited: list[str] = []

    def goto(self, url: str, **_kwargs: Any) -> None:
        self.visited.append(url)

    def is_closed(self) -> bool:
        return self.closed or self.context.closed


class FakeLoginContext:
    """Simulates the user: logs in on poll 2, then closes the window/page on a later poll."""

    def __init__(
        self,
        *,
        login_on_poll: int | None = 2,
        close_window_on_poll: int | None = None,
        close_pages_on_poll: int | None = None,
        dialog_on_poll: int | None = None,
    ) -> None:
        self.login_on_poll = login_on_poll
        self.close_window_on_poll = close_window_on_poll
        self.close_pages_on_poll = close_pages_on_poll
        self.dialog_on_poll = dialog_on_poll
        self.closed = False
        self.polls = 0
        self.jar: list[dict[str, Any]] = []
        self.handlers: dict[str, list[Callable[..., Any]]] = {}
        self.storage_state_calls = 0
        self.dialogs: list[FakeDialog] = []
        self.pages: list[FakeLoginPage] = [FakeLoginPage(self)]

    def on(self, event: str, callback: Callable[..., Any]) -> None:
        # Same constraint as Playwright's wrap_handler: the handler (or, for a bound method,
        # its instance) must accept new attributes.
        target = getattr(callback, "__self__", None) if inspect.ismethod(callback) else callback
        setattr(target, "_pw_impl_instance_" + getattr(callback, "__name__", ""), object())
        self.handlers.setdefault(event, []).append(callback)

    def new_page(self) -> FakeLoginPage:
        page = FakeLoginPage(self)
        self.pages.append(page)
        return page

    def cookies(self) -> list[dict[str, Any]]:
        if self.closed:
            raise PlaywrightError("BrowserContext.cookies: Target page, context or browser closed")
        return [dict(cookie) for cookie in self.jar]

    def wait_for_event(self, event: str, timeout: float) -> FakeLoginContext:
        assert event == "close"
        self.polls += 1
        if self.polls == self.login_on_poll:
            self.jar.append(dict(SESSION_COOKIE))
        if self.polls == self.dialog_on_poll:
            dialog = FakeDialog()
            self.dialogs.append(dialog)
            for callback in self.handlers.get("dialog", []):
                callback(dialog)
        if self.polls == self.close_pages_on_poll:
            for page in self.pages:
                page.closed = True
        if self.polls == self.close_window_on_poll:
            self.closed = True  # closing the last window ends the browser
            for callback in self.handlers.get("close", []):
                callback(self)
            return self
        time.sleep(timeout / 1000)
        raise PlaywrightTimeoutError(f"Timeout {timeout}ms exceeded waiting for event 'close'")

    def storage_state(self) -> dict[str, Any]:
        if self.closed:
            raise PlaywrightError("BrowserContext.storage_state: Target closed")
        self.storage_state_calls += 1
        return {"cookies": [dict(c) for c in self.jar], "origins": STATE["origins"]}

    def close(self) -> None:
        self.closed = True


class FakeLoginHandle:
    def __init__(self, context: FakeLoginContext, cancelled: bool = False) -> None:
        self.context = context
        self.cancelled = cancelled
        self.calls: list[tuple[str, bool | None, dict[str, Any]]] = []

    def persistent_context(
        self,
        profile: str,
        headless: bool | None = None,
        args: list[str] | None = None,
        **kwargs: Any,
    ) -> FakeLoginContext:
        self.calls.append((profile, headless, kwargs))
        return self.context


class FakeManager:
    def __init__(
        self, handle: FakeLoginHandle | None = None, exc: BaseException | None = None
    ) -> None:
        self.handle = handle
        self.exc = exc
        self.runs: list[tuple[float, str, float | None]] = []

    def run(
        self,
        fn: Callable[[Any], Any],
        timeout: float = 180.0,
        label: str = "",
        *,
        queue_timeout: float | None = None,
    ) -> Any:
        self.runs.append((timeout, label, queue_timeout))
        if self.exc is not None:
            raise self.exc
        return fn(self.handle)


def _login(
    context: FakeLoginContext, timeout_s: float = 600, **kwargs: Any
) -> tuple[dict[str, Any], FakeManager]:
    manager = FakeManager(FakeLoginHandle(context, **kwargs))
    url = "https://nid.example.com/login?next=x"
    result = start_login("naver", url, timeout_s, manager, poll_s=0.01)  # type: ignore[arg-type]
    return result, manager


# --------------------------------------------------------------------------- start_login


def test_login_window_closed_by_user_saves_snapshot(db: Session) -> None:
    context = FakeLoginContext(close_window_on_poll=4)
    result, manager = _login(context)
    assert result["status"] == "active" and result["cookies"] == 1
    assert "closed" in result["message"]
    assert manager.runs == [(600 + LOGIN_GRACE_S, "login:naver", LOGIN_QUEUE_TIMEOUT_S)]
    profile, headless, kwargs = manager.handle.calls[0]  # type: ignore[union-attr]
    assert (profile, headless, kwargs) == ("naver", False, {"no_viewport": True})
    assert context.pages[0].visited == ["https://nid.example.com/login?next=x"]
    assert "dialog" in context.handlers  # dialogs stay visible for the user
    assert context.storage_state_calls == 0  # browser already gone: cookie snapshot used
    assert load_storage_state("naver") == {"cookies": [SESSION_COOKIE], "origins": []}
    row = db.get(ProviderSession, "naver")
    assert row is not None and row.status == "active"
    assert row.storage_path == str(state_path("naver"))
    assert session_status(db, "naver") == "active"


def test_login_timeout_exports_full_state_and_dismisses_dialogs() -> None:
    context = FakeLoginContext(dialog_on_poll=3)
    started = time.monotonic()
    result, _manager = _login(context, timeout_s=0.3)
    assert time.monotonic() - started < 5
    assert result["status"] == "active" and "timed out" in result["message"]
    assert context.storage_state_calls == 1
    assert context.dialogs and all(d.dismissed for d in context.dialogs)
    assert load_storage_state("naver") == STATE


def test_login_all_pages_closed_but_browser_alive_exports_full_state() -> None:
    context = FakeLoginContext(close_pages_on_poll=3)
    result, _manager = _login(context)
    assert result["status"] == "active" and "closed" in result["message"]
    assert context.storage_state_calls == 1
    assert load_storage_state("naver") == STATE


def test_login_cancelled_handle_still_exports() -> None:
    context = FakeLoginContext(login_on_poll=None)
    context.jar.append(dict(SESSION_COOKIE))
    result, _manager = _login(context, cancelled=True)
    assert result["status"] == "active" and "cancelled" in result["message"]


def test_login_without_cookies_saves_nothing(db: Session) -> None:
    context = FakeLoginContext(login_on_poll=None, close_window_on_poll=2)
    result, _manager = _login(context)
    assert result == {
        "status": "none",
        "cookies": 0,
        "message": "No login cookies for example.com were captured; the login was probably "
        "not completed (nothing saved).",
    }
    assert not state_path("naver").exists()
    assert db.get(ProviderSession, "naver") is None


@pytest.mark.parametrize(
    "exc",
    [
        TimeoutError("browser job 'login:naver' did not start within 720s"),
        BrowserUnavailableError("No browser could be launched"),
        PlaywrightError("BrowserType.launch_persistent_context: crashed\n=== logs ==="),
    ],
)
def test_login_browser_failure_returns_error(exc: BaseException, db: Session) -> None:
    manager = FakeManager(exc=exc)
    url = "https://example.com/login"
    result = start_login("naver", url, 600, manager)  # type: ignore[arg-type]
    assert result["status"] == "error" and result["cookies"] == 0
    assert result["message"] == str(exc).splitlines()[0]
    assert db.get(ProviderSession, "naver") is None


@pytest.mark.parametrize("url", ["", "example.com/login", "javascript:alert(1)", "file:///C:/x"])
def test_login_rejects_non_http_urls(url: str) -> None:
    with pytest.raises(ValueError):
        start_login("naver", url, 600, FakeManager())  # type: ignore[arg-type]


def test_login_through_real_manager_uses_headed_window_sized_profile(
    local_appdata: Path,
) -> None:
    """Integration with BrowserManager/BrowserHandle (fake Playwright, no window)."""
    context = FakeLoginContext(close_window_on_poll=3)
    launches: list[dict[str, Any]] = []

    class Chromium:
        def launch_persistent_context(self, user_data_dir: str, **kwargs: Any) -> FakeLoginContext:
            launches.append({"user_data_dir": user_data_dir, **kwargs})
            return context

    class Playwright:
        chromium = Chromium()

    @contextmanager
    def factory() -> Iterator[Playwright]:
        yield Playwright()

    BrowserHandle.reset_channel_cache()
    manager = BrowserManager(playwright_factory=factory)
    try:
        result = start_login("naver", "https://example.com/login", 600, manager, poll_s=0.01)
    finally:
        manager.shutdown()
        BrowserHandle.reset_channel_cache()
    assert result["status"] == "active"
    (launch,) = launches
    assert launch["headless"] is False and launch["no_viewport"] is True
    assert "viewport" not in launch and launch["locale"] == "ko-KR"
    assert launch["channel"] == "msedge"
    profiles = local_appdata / "AiSquaredTravel" / "browser_profiles"
    assert Path(launch["user_data_dir"]) == profiles / "naver"
    assert sessions.load_storage_state("naver") == {"cookies": [SESSION_COOKIE], "origins": []}


# --------------------------------------------------------------------------- domain filter


def _cookie(name: str, domain: str) -> dict[str, Any]:
    return {**SESSION_COOKIE, "name": name, "domain": domain}


def test_filter_storage_state_keeps_related_domains_only() -> None:
    state = {
        "cookies": [
            _cookie("parent", ".naver.com"),
            _cookie("sub", "nid.naver.com"),
            _cookie("aad", ".login.microsoftonline.com"),
            _cookie("bing", ".bing.com"),
            _cookie("lookalike", "evilnaver.com"),
        ],
        "origins": [
            {"origin": "https://flight.naver.com", "localStorage": []},
            {"origin": "https://www.bing.com", "localStorage": []},
        ],
    }
    kept = filter_storage_state(state, ["flight.naver.com", " .NID.naver.com "])
    assert [c["name"] for c in kept["cookies"]] == ["parent", "sub"]
    assert [o["origin"] for o in kept["origins"]] == ["https://flight.naver.com"]
    assert len(state["cookies"]) == 5  # input untouched


def test_login_with_cookie_domains_saves_filtered_state() -> None:
    context = FakeLoginContext(close_window_on_poll=4)
    context.jar.append(_cookie("MUID", ".bing.com"))
    manager = FakeManager(FakeLoginHandle(context))
    result = start_login(
        "naver",
        "https://nid.example.com/login",
        600,
        manager,  # type: ignore[arg-type]
        cookie_domains=["example.com"],
        poll_s=0.01,
    )
    assert result["status"] == "active" and result["cookies"] == 1
    assert load_storage_state("naver") == {"cookies": [SESSION_COOKIE], "origins": []}


def test_site_domain() -> None:
    assert site_domain("nid.naver.com") == "naver.com"
    assert site_domain("NAVER.com.") == "naver.com"
    assert site_domain("www.example.co.kr") == "example.co.kr"
    assert site_domain("a.b.example.com.au") == "example.com.au"
    assert site_domain("accounts.google.com") == "google.com"
    assert site_domain("localhost") == "localhost"
    assert site_domain("127.0.0.1") == "127.0.0.1"
    assert site_domain("[::1]") == "::1"


# --------------------------------------------------------------------------- login: what counts

MS_COOKIES = [
    _cookie("MUID", ".bing.com"),
    _cookie("AAD_AT", ".login.microsoftonline.com"),
    _cookie("EdgeAuth", ".microsoft.com"),
]


def test_abandoned_login_with_only_microsoft_cookies_saves_nothing(db: Session) -> None:
    """Review repro: a fresh headed Edge profile holds the user's Microsoft-account cookies;
    closing the window without logging in must not mark the provider active."""
    context = FakeLoginContext(login_on_poll=None, close_window_on_poll=2)
    context.jar.extend(dict(cookie) for cookie in MS_COOKIES)
    result, _manager = _login(context)
    assert result["status"] == "none" and result["cookies"] == 0
    assert "example.com" in result["message"]
    assert not state_path("naver").exists()
    assert db.get(ProviderSession, "naver") is None


def test_login_saves_only_the_login_site_and_extra_domains() -> None:
    context = FakeLoginContext(close_window_on_poll=4)
    context.jar.extend(dict(cookie) for cookie in MS_COOKIES)
    context.jar.append(_cookie("KAKAO", ".kakao.com"))
    context.jar.append(_cookie("OTHER", ".unrelated.org"))
    manager = FakeManager(FakeLoginHandle(context))
    result = start_login(
        "naver",
        "https://nid.example.com/login",
        600,
        manager,  # type: ignore[arg-type]
        cookie_domains=["kakao.com", " "],
        poll_s=0.01,
    )
    assert result["status"] == "active" and result["cookies"] == 2
    saved = load_storage_state("naver")
    assert saved is not None
    assert sorted(c["name"] for c in saved["cookies"]) == ["KAKAO", "SID"]


def test_login_cookies_decide_whether_the_login_completed(db: Session) -> None:
    context = FakeLoginContext(close_window_on_poll=4)  # sets SID only
    manager = FakeManager(FakeLoginHandle(context))
    url = "https://nid.example.com/login"
    result = start_login(
        "naver", url, 600, manager, login_cookies=["NID_AUT"], poll_s=0.01  # type: ignore[arg-type]
    )
    assert result["status"] == "none" and not state_path("naver").exists()
    context = FakeLoginContext(close_window_on_poll=4)
    context.jar.append(_cookie("NID_AUT", ".example.com"))
    manager = FakeManager(FakeLoginHandle(context))
    result = start_login(
        "naver", url, 600, manager, login_cookies=["NID_AUT"], poll_s=0.01  # type: ignore[arg-type]
    )
    assert result["status"] == "active" and result["cookies"] == 2
    assert session_status(db, "naver") == "active"


def test_login_save_failure_returns_error(monkeypatch: pytest.MonkeyPatch, db: Session) -> None:
    def broken_save(_key: str, _state: dict[str, Any]) -> Path:
        raise OSError("disk full")

    monkeypatch.setattr(sessions, "save_storage_state", broken_save)
    result, _manager = _login(FakeLoginContext(close_window_on_poll=4))
    assert result == {
        "status": "error",
        "cookies": 0,
        "message": "Could not save the session (OSError).",
    }
    assert db.get(ProviderSession, "naver") is None


# --------------------------------------------------------------------------- login: queueing


def _manager_for(context: FakeLoginContext, launches: list[dict[str, Any]]) -> BrowserManager:
    """A real BrowserManager whose Playwright launches ``context`` (no window opens)."""

    class Chromium:
        def launch_persistent_context(self, user_data_dir: str, **kwargs: Any) -> FakeLoginContext:
            launches.append({"user_data_dir": user_data_dir, **kwargs})
            return context

    class Playwright:
        chromium = Chromium()

    @contextmanager
    def factory() -> Iterator[Playwright]:
        yield Playwright()

    return BrowserManager(playwright_factory=factory)


def test_login_queued_behind_another_job_gets_its_full_window(
    monkeypatch: pytest.MonkeyPatch, db: Session
) -> None:
    """Review repro: the login waited behind a 1 s job; the manager timeout used to count
    the queue time and threw the user's login away while the window was still open."""
    monkeypatch.setattr(sessions, "LOGIN_GRACE_S", 0.3)
    context = FakeLoginContext()  # logs in on poll 2 and keeps the window open
    launches: list[dict[str, Any]] = []
    manager = _manager_for(context, launches)
    try:
        manager.submit(lambda _h: time.sleep(1.0), label="google_flights")
        url = "https://example.com/login"
        result = start_login("naver", url, 0.5, manager, poll_s=0.05)
    finally:
        manager.shutdown(wait=True, timeout=5)
    assert result["status"] == "active" and "timed out" in result["message"]
    assert len(launches) == 1 and context.storage_state_calls == 1
    assert load_storage_state("naver") == STATE
    assert session_status(db, "naver") == "active"


def test_login_that_cannot_start_opens_no_window(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sessions, "LOGIN_QUEUE_TIMEOUT_S", 0.2)
    launches: list[dict[str, Any]] = []
    manager = _manager_for(FakeLoginContext(), launches)
    release = threading.Event()
    try:
        manager.submit(lambda _h: release.wait(10), label="naver")
        result = start_login("naver", "https://example.com/login", 600, manager, poll_s=0.01)
        release.set()
    finally:
        manager.shutdown(wait=True, timeout=5)
    assert result["status"] == "error"
    assert result["message"] == "browser job 'login:naver' did not start within 0.2s " \
        "(browser busy with 'naver')"
    assert launches == [] and not state_path("naver").exists()


class SlowExportContext(FakeLoginContext):
    """``storage_state()`` hangs for a while (e.g. a busy renderer)."""

    def storage_state(self) -> dict[str, Any]:
        time.sleep(0.8)
        return super().storage_state()


def test_login_captured_after_the_caller_gave_up_is_still_saved(
    monkeypatch: pytest.MonkeyPatch, db: Session
) -> None:
    monkeypatch.setattr(sessions, "LOGIN_GRACE_S", 0.2)
    context = SlowExportContext()
    manager = _manager_for(context, [])
    try:
        result = start_login("naver", "https://example.com/login", 0.2, manager, poll_s=0.02)
        assert result["status"] == "error" and "timed out after 0.4s" in result["message"]
        assert "still saved" in result["message"]
    finally:
        manager.shutdown(wait=True, timeout=5)  # the job finishes its export and save
    assert load_storage_state("naver") == STATE
    assert session_status(db, "naver") == "active"
