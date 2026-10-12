"""BrowserManager / BrowserHandle: worker thread, timeouts, channel fallback, cleanup.

Most tests inject a fake Playwright (no driver, no browser). Two tests use the real
Playwright driver (no browser window); the Edge tests launch real *headless* Edge on
``data:`` URLs and are skipped when Edge is not installed.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Error as PlaywrightError

from app.browser import manager as manager_module
from app.browser.manager import (
    LAUNCH_TIMEOUT_MS,
    OFFSCREEN_ARGS,
    BrowserError,
    BrowserHandle,
    BrowserJobCancelled,
    BrowserManager,
    BrowserProfileLockedError,
    BrowserUnavailableError,
    channel_candidates,
    context_options,
    get_browser_manager,
    normalize_channel,
    persistent_launch_options,
    shutdown_browser_manager,
)
from app.browser.sessions import read_profile_channel, write_profile_channel
from app.config import Settings, get_settings

MISSING = "BrowserType.launch: Chromium distribution '{}' is not found at C:\\x.exe"
LOCKED = (
    "BrowserType.launch_persistent_context: Target page, context or browser has been closed\n"
    "<process did exit: exitCode=21>"
)

_EDGE_CANDIDATES = [
    Path(os.environ.get(var, default)) / "Microsoft" / "Edge" / "Application" / "msedge.exe"
    for var, default in (
        ("ProgramFiles(x86)", r"C:\Program Files (x86)"),
        ("ProgramFiles", r"C:\Program Files"),
    )
]
EDGE_INSTALLED = any(path.is_file() for path in _EDGE_CANDIDATES)
needs_edge = pytest.mark.skipif(not EDGE_INSTALLED, reason="Microsoft Edge is not installed")


# --------------------------------------------------------------------------- fakes


class FakeContext:
    def __init__(self, options: dict[str, Any]) -> None:
        self.options = options
        self.closed = False
        self.added_cookies: list[dict[str, Any]] = []

    def add_cookies(self, cookies: list[dict[str, Any]]) -> None:
        self.added_cookies.extend(cookies)

    def close(self) -> None:
        self.closed = True


class FakeBrowser:
    def __init__(self, channel: str) -> None:
        self.channel = channel
        self.closed = False
        self.contexts: list[FakeContext] = []

    def is_connected(self) -> bool:
        return not self.closed

    def new_context(self, **options: Any) -> FakeContext:
        context = FakeContext(options)
        self.contexts.append(context)
        return context

    def close(self) -> None:
        self.closed = True


class FakeChromium:
    """Records launches; ``failures[channel]`` is raised as a Playwright error."""

    name = "chromium"

    def __init__(self, failures: dict[str, str] | None = None) -> None:
        self.failures = failures or {}
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.browsers: list[FakeBrowser] = []
        self.persistent: list[FakeContext] = []

    def launch(self, **kwargs: Any) -> FakeBrowser:
        self.calls.append(("launch", kwargs["channel"], kwargs))
        if kwargs["channel"] in self.failures:
            raise PlaywrightError(self.failures[kwargs["channel"]])
        browser = FakeBrowser(kwargs["channel"])
        self.browsers.append(browser)
        return browser

    def launch_persistent_context(self, user_data_dir: str, **kwargs: Any) -> FakeContext:
        record = {"user_data_dir": user_data_dir, **kwargs}
        self.calls.append(("persistent", kwargs["channel"], record))
        if kwargs["channel"] in self.failures:
            raise PlaywrightError(self.failures[kwargs["channel"]])
        context = FakeContext(kwargs)
        self.persistent.append(context)
        return context

    def channels_tried(self) -> list[str]:
        return [channel for _kind, channel, _kwargs in self.calls]


class FakePlaywright:
    def __init__(self, chromium: FakeChromium) -> None:
        self.chromium = chromium


def fake_factory(chromium: FakeChromium) -> Callable[[], Any]:
    @contextmanager
    def factory() -> Iterator[FakePlaywright]:
        yield FakePlaywright(chromium)

    return factory


class FakePage:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    def screenshot(self, path: str, timeout: float) -> None:
        if self.fail:
            raise PlaywrightError("Target page, context or browser has been closed")
        Path(path).write_bytes(b"\x89PNG fake")


# --------------------------------------------------------------------------- fixtures


@pytest.fixture(autouse=True)
def _isolated_browser_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Profiles under tmp (never the real %LOCALAPPDATA%) and a clean channel cache."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
    BrowserHandle.reset_channel_cache()
    yield
    BrowserHandle.reset_channel_cache()
    shutdown_browser_manager(wait=False)


@pytest.fixture
def chromium() -> FakeChromium:
    return FakeChromium()


@pytest.fixture
def manager(chromium: FakeChromium) -> Iterator[BrowserManager]:
    mgr = BrowserManager(playwright_factory=fake_factory(chromium))
    yield mgr
    mgr.shutdown(wait=True, timeout=5)


def _wait_until(predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


# --------------------------------------------------------------------------- pure helpers


def test_channel_candidates_order_and_dedupe() -> None:
    assert channel_candidates("msedge") == ["msedge", "chrome", "chromium"]
    assert channel_candidates("chrome") == ["chrome", "msedge", "chromium"]
    assert channel_candidates("msedge", "chromium") == ["chromium", "msedge", "chrome"]
    assert channel_candidates("msedge-beta", "msedge") == [
        "msedge",
        "msedge-beta",
        "chrome",
        "chromium",
    ]
    assert channel_candidates(None) == ["msedge", "chrome", "chromium"]


def test_normalize_channel() -> None:
    assert normalize_channel(" MSEdge ") == "msedge"
    assert normalize_channel("") is None and normalize_channel(None) is None
    with pytest.raises(ValueError):
        normalize_channel("msedge; rm -rf")


def test_context_options_defaults_and_overrides() -> None:
    options = context_options()
    assert options == {
        "locale": "ko-KR",
        "timezone_id": "Asia/Seoul",
        "viewport": {"width": 1366, "height": 900},
    }
    options["viewport"]["width"] = 1  # the module default must not be shared/mutated
    assert context_options()["viewport"]["width"] == 1366
    assert context_options({"locale": "en-US"})["locale"] == "en-US"
    assert "viewport" not in context_options({"no_viewport": True})
    assert "storage_state" not in context_options({"storage_state": None})
    state = {"cookies": [], "origins": []}
    assert context_options({"storage_state": state})["storage_state"] is state
    assert OFFSCREEN_ARGS == ["--window-position=-32000,-32000", "--window-size=1366,900"]


# --------------------------------------------------------------------------- jobs + cleanup


def test_run_executes_on_worker_and_closes_everything(
    manager: BrowserManager, chromium: FakeChromium, tmp_path: Path
) -> None:
    def job(handle: BrowserHandle) -> dict[str, Any]:
        browser = handle.launch()
        default_ctx = handle.new_context()  # reuses the launched browser
        custom_ctx = handle.new_context(browser, locale="en-US", storage_state={"cookies": []})
        profile_ctx = handle.persistent_context("demo", args=OFFSCREEN_ARGS)
        return {
            "thread": threading.current_thread().name,
            "objects": (browser, default_ctx, custom_ctx, profile_ctx),
            "channel": handle.channel_used,
        }

    result = manager.run(job, timeout=10, label="unit")
    assert result["thread"].startswith("playwright")
    assert result["channel"] == "msedge"
    browser, default_ctx, custom_ctx, profile_ctx = result["objects"]
    assert len(chromium.browsers) == 1 and browser.contexts == [default_ctx, custom_ctx]
    assert all(obj.closed for obj in result["objects"])
    assert default_ctx.options["locale"] == "ko-KR"
    assert default_ctx.options["timezone_id"] == "Asia/Seoul"
    assert custom_ctx.options["locale"] == "en-US"
    assert custom_ctx.options["storage_state"] == {"cookies": []}
    kind, channel, kwargs = chromium.calls[-1]
    assert (kind, channel) == ("persistent", "msedge")
    expected_dir = tmp_path / "localappdata" / "AiSquaredTravel" / "browser_profiles" / "demo"
    assert Path(kwargs["user_data_dir"]) == expected_dir and expected_dir.is_dir()
    assert kwargs["args"] == OFFSCREEN_ARGS and kwargs["viewport"] == {"width": 1366, "height": 900}
    assert kwargs["headless"] is True  # BROWSER_HEADLESS default


def test_everything_closed_even_when_job_raises(
    manager: BrowserManager, chromium: FakeChromium
) -> None:
    def job(handle: BrowserHandle) -> None:
        handle.new_context()
        handle.persistent_context("p1")
        raise ValueError("parse failed")

    with pytest.raises(ValueError, match="parse failed"):
        manager.run(job, timeout=10)
    assert chromium.browsers[0].closed and chromium.browsers[0].contexts[0].closed
    assert chromium.persistent[0].closed


def test_handle_is_unusable_after_job_and_from_other_threads(manager: BrowserManager) -> None:
    errors: list[BaseException] = []

    def job(handle: BrowserHandle) -> BrowserHandle:
        def other_thread() -> None:
            try:
                handle.launch()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        thread = threading.Thread(target=other_thread)
        thread.start()
        thread.join()
        return handle

    handle = manager.run(job, timeout=10)
    assert len(errors) == 1 and isinstance(errors[0], RuntimeError)
    with pytest.raises(BrowserJobCancelled):
        handle.launch()
    with pytest.raises(BrowserJobCancelled):
        _ = handle.playwright


def test_nested_run_from_a_job_is_refused(manager: BrowserManager) -> None:
    def job(_handle: BrowserHandle) -> None:
        manager.run(lambda _h: None, timeout=1)

    with pytest.raises(RuntimeError, match="deadlock"):
        manager.run(job, timeout=10)


def test_submit_returns_future_usable_from_async(manager: BrowserManager) -> None:
    future = manager.submit(lambda handle: handle.label, label="futured")
    assert future.result(timeout=10) == "futured"

    async def main() -> str:
        return await asyncio.wrap_future(manager.submit(lambda h: "async-ok", "async"))

    assert asyncio.run(main()) == "async-ok"


# --------------------------------------------------------------------------- timeouts


def test_timeout_raises_and_cancels_running_handle(manager: BrowserManager) -> None:
    release = threading.Event()
    seen: dict[str, Any] = {}

    def slow(handle: BrowserHandle) -> None:
        seen["handle"] = handle
        release.wait(10)
        try:
            handle.launch()
        except BrowserJobCancelled:
            seen["raised"] = True
            raise

    started = time.monotonic()
    with pytest.raises(TimeoutError, match="timed out after 0.3s"):
        manager.run(slow, timeout=0.3, label="slow")
    assert time.monotonic() - started < 3
    assert seen["handle"].cancelled
    assert manager.current_label == "slow"  # worker keeps running until fn returns
    release.set()
    assert manager.run(lambda _h: "next", timeout=10) == "next"
    assert seen["raised"] is True


def test_queued_job_that_never_started_is_dropped(manager: BrowserManager) -> None:
    release = threading.Event()
    ran: list[str] = []
    blocker = manager.submit(lambda _h: release.wait(10), label="blocker")
    assert _wait_until(lambda: manager.current_label == "blocker")
    with pytest.raises(TimeoutError, match="did not start.*blocker"):
        manager.run(lambda _h: ran.append("queued"), timeout=0.2, label="queued")
    release.set()
    blocker.result(timeout=10)
    assert manager.run(lambda _h: "after", timeout=10) == "after"
    assert ran == []


def test_timeout_error_raised_by_job_propagates_unchanged(manager: BrowserManager) -> None:
    def job(_handle: BrowserHandle) -> None:
        raise TimeoutError("selector wait expired")

    with pytest.raises(TimeoutError, match="selector wait expired"):
        manager.run(job, timeout=10)


def test_queue_timeout_makes_timeout_count_from_job_start(manager: BrowserManager) -> None:
    release = threading.Event()
    manager.submit(lambda _h: release.wait(10), label="blocker")
    assert _wait_until(lambda: manager.current_label == "blocker")
    threading.Timer(0.4, release.set).start()

    def job(handle: BrowserHandle) -> str:
        time.sleep(0.2)
        return "cancelled" if handle.cancelled else "ran"

    started = time.monotonic()
    # 0.4 s queued + 0.2 s running > timeout, but the timeout only counts from the start.
    assert manager.run(job, timeout=0.35, label="own-deadline", queue_timeout=5) == "ran"
    assert time.monotonic() - started >= 0.55


def test_queue_timeout_drops_a_job_that_never_starts(manager: BrowserManager) -> None:
    release = threading.Event()
    ran: list[str] = []
    blocker = manager.submit(lambda _h: release.wait(10), label="blocker")
    assert _wait_until(lambda: manager.current_label == "blocker")
    started = time.monotonic()
    with pytest.raises(TimeoutError, match="did not start within 0.2s.*blocker"):
        manager.run(lambda _h: ran.append("x"), timeout=60, label="late", queue_timeout=0.2)
    assert time.monotonic() - started < 3
    release.set()
    blocker.result(timeout=10)
    assert manager.run(lambda _h: "after", timeout=10) == "after"
    assert ran == []


def test_queue_timeout_still_cancels_a_running_job_after_timeout(manager: BrowserManager) -> None:
    release = threading.Event()
    handles: list[BrowserHandle] = []

    def slow(handle: BrowserHandle) -> None:
        handles.append(handle)
        release.wait(10)

    with pytest.raises(TimeoutError, match="timed out after 0.2s"):
        manager.run(slow, timeout=0.2, label="slow", queue_timeout=5)
    assert handles[0].cancelled
    release.set()


def test_shutdown_wakes_a_run_waiting_for_its_start(manager: BrowserManager) -> None:
    release = threading.Event()
    manager.submit(lambda _h: release.wait(10), label="blocker")
    assert _wait_until(lambda: manager.current_label == "blocker")
    errors: list[BaseException] = []

    def waiter() -> None:
        try:
            manager.run(lambda _h: None, timeout=60, label="queued", queue_timeout=60)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    thread = threading.Thread(target=waiter)
    thread.start()
    time.sleep(0.1)
    manager.shutdown(wait=False)
    thread.join(5)
    release.set()
    assert not thread.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], BrowserJobCancelled)


# --------------------------------------------------------------------------- channels


def test_falls_back_to_next_channel_and_caches_it(chromium: FakeChromium) -> None:
    chromium.failures["msedge"] = MISSING.format("msedge")
    mgr = BrowserManager(playwright_factory=fake_factory(chromium))
    try:
        assert mgr.run(lambda h: (h.launch(), h.channel_used)[1], timeout=10) == "chrome"
        assert chromium.channels_tried() == ["msedge", "chrome"]
        assert BrowserHandle.preferred_channel() == "chrome"
        chromium.calls.clear()
        mgr.run(lambda h: h.launch(), timeout=10)
        assert chromium.channels_tried() == ["chrome"]  # cached channel tried first
    finally:
        mgr.shutdown()


def test_no_channel_available_gives_install_hint(chromium: FakeChromium) -> None:
    for channel in ("msedge", "chrome", "chromium"):
        chromium.failures[channel] = MISSING.format(channel)
    mgr = BrowserManager(playwright_factory=fake_factory(chromium))
    try:
        with pytest.raises(BrowserUnavailableError) as info:
            mgr.run(lambda h: h.launch(), timeout=10)
    finally:
        mgr.shutdown()
    message = str(info.value)
    assert "python -m playwright install chromium" in message
    assert all(channel in message for channel in ("msedge", "chrome", "chromium"))
    assert BrowserHandle.preferred_channel() is None


def test_configure_changes_defaults_and_resets_cache(
    manager: BrowserManager, chromium: FakeChromium
) -> None:
    chromium.failures["msedge"] = "BrowserType.launch: crashed"
    manager.run(lambda h: h.launch(), timeout=10)
    assert BrowserHandle.preferred_channel() == "chrome"
    chromium.failures.clear()
    chromium.calls.clear()
    manager.configure(channel="chromium", headless=False)
    assert BrowserHandle.preferred_channel() is None
    manager.run(lambda h: h.launch(), timeout=10)
    _kind, channel, kwargs = chromium.calls[0]
    assert channel == "chromium" and kwargs["headless"] is False
    manager.configure(None, None)  # no change
    assert manager.channel == "chromium" and manager.headless is False


def test_defaults_come_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BROWSER_CHANNEL", "Chrome")
    monkeypatch.setenv("BROWSER_HEADLESS", "false")
    get_settings.cache_clear()
    mgr = BrowserManager()
    try:
        assert mgr.channel == "chrome" and mgr.headless is False
    finally:
        mgr.shutdown()


def test_persistent_context_falls_back_only_when_not_installed(chromium: FakeChromium) -> None:
    chromium.failures["msedge"] = MISSING.format("msedge")
    mgr = BrowserManager(playwright_factory=fake_factory(chromium))
    try:
        used = mgr.run(lambda h: (h.persistent_context("p"), h.channel_used)[1], timeout=10)
        assert used == "chrome"
        assert read_profile_channel("p") == "chrome"
        assert BrowserHandle.preferred_channel() is None  # profiles never feed the cache
        chromium.failures["msedge"] = LOCKED
        chromium.calls.clear()
        with pytest.raises(BrowserProfileLockedError, match="in use"):
            mgr.run(lambda h: h.persistent_context("q"), timeout=10)
        assert chromium.channels_tried() == ["msedge"]
        assert read_profile_channel("q") is None  # nothing launched, nothing pinned
    finally:
        mgr.shutdown()


def test_profile_stays_on_its_channel_after_a_transient_launch_failure(
    manager: BrowserManager, chromium: FakeChromium
) -> None:
    """Review repro: a one-off msedge launch() timeout caches chrome for throwaway browsers,
    but the Edge-created login profile must still be reopened with Edge."""
    manager.run(lambda h: h.persistent_context("naver"), timeout=10)
    assert read_profile_channel("naver") == "msedge"
    chromium.failures["msedge"] = "BrowserType.launch: Timeout 60000ms exceeded."
    assert manager.run(lambda h: (h.launch(), h.channel_used)[1], timeout=10) == "chrome"
    assert BrowserHandle.preferred_channel() == "chrome"
    del chromium.failures["msedge"]
    chromium.calls.clear()
    used = manager.run(lambda h: (h.persistent_context("naver"), h.channel_used)[1], timeout=10)
    assert used == "msedge" and chromium.channels_tried() == ["msedge"]
    assert BrowserHandle.preferred_channel() == "chrome"  # launch() cache left alone


def test_profile_recorded_channel_wins_over_configured_channel(
    manager: BrowserManager, chromium: FakeChromium
) -> None:
    manager.run(lambda h: h.persistent_context("naver"), timeout=10)
    manager.configure(channel="chrome")
    chromium.calls.clear()
    manager.run(lambda h: (h.persistent_context("naver"), h.launch()), timeout=10)
    assert chromium.calls[0][:2] == ("persistent", "msedge")
    assert chromium.calls[1][:2] == ("launch", "chrome")  # throwaway browsers follow config
    manager.run(lambda h: h.persistent_context("fresh"), timeout=10)
    assert read_profile_channel("fresh") == "chrome"  # new profiles follow config


def test_profile_moves_to_another_channel_only_when_its_browser_is_missing(
    manager: BrowserManager, chromium: FakeChromium, caplog: pytest.LogCaptureFixture
) -> None:
    write_profile_channel("naver", "chrome")
    chromium.failures["chrome"] = MISSING.format("chrome")
    with caplog.at_level("WARNING", logger="app.browser.manager"):
        used = manager.run(
            lambda h: (h.persistent_context("naver"), h.channel_used)[1], timeout=10
        )
    assert chromium.channels_tried() == ["chrome", "msedge"]
    assert used == "msedge" and read_profile_channel("naver") == "msedge"
    assert "created by 'chrome'" in caplog.text
    chromium.failures["chrome"] = "BrowserType.launch_persistent_context: crashed"
    write_profile_channel("other", "chrome")
    chromium.calls.clear()
    with pytest.raises(PlaywrightError, match="crashed"):
        manager.run(lambda h: h.persistent_context("other"), timeout=10)
    assert chromium.channels_tried() == ["chrome"]  # transient failure: no fallback


def test_invalid_profile_marker_is_ignored(
    manager: BrowserManager, chromium: FakeChromium
) -> None:
    write_profile_channel("naver", "bad channel!")
    manager.run(lambda h: h.persistent_context("naver"), timeout=10)
    assert chromium.channels_tried() == ["msedge"]
    assert read_profile_channel("naver") == "msedge"


def test_persistent_launch_options() -> None:
    options, state = persistent_launch_options({"storage_state": {"cookies": []}})
    assert state == {"cookies": []} and "storage_state" not in options
    assert options["timeout"] == LAUNCH_TIMEOUT_MS and options["locale"] == "ko-KR"
    options, state = persistent_launch_options({"timeout": 90_000, "no_viewport": True})
    assert options["timeout"] == 90_000 and "viewport" not in options and state is None
    for reserved in ("channel", "user_data_dir", "executable_path"):
        with pytest.raises(ValueError, match=reserved):
            persistent_launch_options({reserved: "x"})


def test_persistent_context_accepts_timeout_and_rejects_reserved_keys(
    manager: BrowserManager, chromium: FakeChromium
) -> None:
    manager.run(lambda h: h.persistent_context("p", timeout=90_000), timeout=10)
    manager.run(lambda h: h.new_context(profile="p", timeout=1_000), timeout=10)
    manager.run(lambda h: h.persistent_context("p"), timeout=10)
    timeouts = [kwargs["timeout"] for _kind, _channel, kwargs in chromium.calls]
    assert timeouts == [90_000, 1_000, LAUNCH_TIMEOUT_MS]
    with pytest.raises(ValueError, match="channel"):
        manager.run(lambda h: h.persistent_context("p", channel="chrome"), timeout=10)
    with pytest.raises(ValueError, match="user_data_dir"):
        manager.run(lambda h: h.new_context(profile="p", user_data_dir="C:/x"), timeout=10)


def test_new_context_reuses_a_browser_only_in_the_requested_mode(
    manager: BrowserManager, chromium: FakeChromium
) -> None:
    def job(handle: BrowserHandle) -> list[FakeBrowser]:
        contexts = [
            handle.new_context(),  # default mode (headless)
            handle.new_context(headless=False),  # needs a headed browser
            handle.new_context(headless=True),  # reuses the headless one
            handle.new_context(headless=False),  # reuses the headed one
            handle.new_context(),  # None = any connected browser
        ]
        return [_owner(chromium, context) for context in contexts]

    owners = manager.run(job, timeout=10)
    assert [call[2]["headless"] for call in chromium.calls] == [True, False]
    headless, headed = chromium.browsers
    assert owners == [headless, headed, headless, headed, headless]


def _owner(chromium: FakeChromium, context: FakeContext) -> FakeBrowser:
    return next(browser for browser in chromium.browsers if context in browser.contexts)


def test_persistent_context_restores_live_cookies(
    manager: BrowserManager, chromium: FakeChromium
) -> None:
    future = time.time() + 3600
    state = {
        "cookies": [
            {"name": "session", "value": "s", "domain": ".x.com", "path": "/", "expires": -1},
            {"name": "remember", "value": "r", "domain": ".x.com", "path": "/", "expires": future},
            {"name": "old", "value": "o", "domain": ".x.com", "path": "/", "expires": 1000.0},
        ],
        "origins": [],
    }
    manager.run(lambda h: h.new_context(profile="p", storage_state=state), timeout=10)
    context = chromium.persistent[0]
    assert [c["name"] for c in context.added_cookies] == ["session", "remember"]
    assert "storage_state" not in context.options


# --------------------------------------------------------------------------- screenshots


def test_screenshot_saves_under_logs_and_never_raises(
    manager: BrowserManager, settings: Settings
) -> None:
    def job(handle: BrowserHandle) -> tuple[Path | None, Path | None]:
        good = handle.screenshot(FakePage(), "블록 page/1")
        return good, handle.screenshot(FakePage(fail=True), "x")

    good, bad = manager.run(job, timeout=10)
    assert bad is None
    assert good is not None and good.is_file()
    assert good.parent == settings.logs_dir / "screenshots"
    assert good.name.endswith("Z-page-1.png")


def test_prune_screenshots_keeps_newest(tmp_path: Path) -> None:
    for stamp in ("20261001", "20261002", "20261003"):
        (tmp_path / f"{stamp}T000000000000Z-x.png").write_bytes(b"x")
    manager_module._prune_screenshots(tmp_path, keep=2)
    assert sorted(p.name[:8] for p in tmp_path.glob("*.png")) == ["20261002", "20261003"]


# --------------------------------------------------------------------------- shutdown + singleton


def test_shutdown_cancels_active_and_pending_jobs(manager: BrowserManager) -> None:
    def cooperative(handle: BrowserHandle) -> str:
        deadline = time.monotonic() + 10
        while not handle.cancelled and time.monotonic() < deadline:
            time.sleep(0.01)
        return "stopped" if handle.cancelled else "not cancelled"

    active = manager.submit(cooperative, "active")
    assert _wait_until(lambda: manager.current_label == "active")
    pending = manager.submit(lambda _h: "never", "pending")
    manager.shutdown(wait=True, timeout=5)
    assert active.result(timeout=5) == "stopped"
    assert pending.cancelled()
    with pytest.raises(BrowserError, match="shut down"):
        manager.run(lambda _h: None, timeout=1)


def test_singleton_lifecycle() -> None:
    first = get_browser_manager()
    assert get_browser_manager() is first
    shutdown_browser_manager()
    assert first.is_shut_down
    second = get_browser_manager()
    assert second is not first and not second.is_shut_down
    shutdown_browser_manager()


# --------------------------------------------------------------------------- real Playwright


def test_run_from_thread_while_selector_loop_runs_in_main_thread() -> None:
    """uvicorn --reload on Windows runs a SelectorEventLoop; sync Playwright on the worker
    thread must still start its driver subprocess (it gets its own Proactor loop)."""
    mgr = BrowserManager()

    def job(handle: BrowserHandle) -> str:
        return handle.playwright.chromium.name  # starts the real driver; no browser

    async def server() -> tuple[bool, str, str]:
        is_selector = isinstance(asyncio.get_running_loop(), asyncio.SelectorEventLoop)
        via_thread = await asyncio.to_thread(mgr.run, job, 60, "selector-thread")
        via_future = await asyncio.wrap_future(mgr.submit(job, "selector-future"))
        return is_selector, via_thread, via_future

    loop = asyncio.SelectorEventLoop()
    try:
        result = loop.run_until_complete(server())
    finally:
        loop.close()
        mgr.shutdown()
    assert result == (True, "chromium", "chromium")


@needs_edge
def test_real_headless_edge_job_on_data_url(settings: Settings) -> None:
    mgr = BrowserManager(channel="msedge", headless=True)

    def job(handle: BrowserHandle) -> dict[str, Any]:
        page = handle.new_context().new_page()
        page.goto("data:text/html,<title>AiSq</title><p>hello</p>")
        info = page.evaluate(
            "() => ({lang: navigator.language, w: innerWidth, h: innerHeight,"
            " tz: Intl.DateTimeFormat().resolvedOptions().timeZone, ua: navigator.userAgent})"
        )
        return {
            "title": page.title(),
            "info": info,
            "channel": handle.channel_used,
            "shot": handle.screenshot(page, "edge-test"),
            "browser": handle._browsers[0],
        }

    try:
        result = mgr.run(job, timeout=120, label="edge-test")
    finally:
        mgr.shutdown()
    assert result["title"] == "AiSq" and result["channel"] == "msedge"
    info = result["info"]
    assert info["lang"] == "ko-KR" and info["tz"] == "Asia/Seoul"
    assert (info["w"], info["h"]) == (1366, 900)
    assert "Edg/" in info["ua"]
    assert result["shot"] is not None and result["shot"].is_file()
    assert result["shot"].parent == settings.logs_dir / "screenshots"
    assert not result["browser"].is_connected()  # closed when the job returned


@needs_edge
def test_real_edge_persistent_profile_with_restored_session_cookie(tmp_path: Path) -> None:
    mgr = BrowserManager(channel="msedge", headless=True)
    state = {
        "cookies": [
            {
                "name": "sid",
                "value": "v1",
                "domain": "example.com",
                "path": "/",
                "expires": -1,
                "httpOnly": True,
                "secure": True,
                "sameSite": "Lax",
            }
        ],
        "origins": [],
    }

    def job(handle: BrowserHandle) -> list[str]:
        context = handle.persistent_context("edge-test", storage_state=state)
        return [cookie["name"] for cookie in context.cookies("https://example.com")]

    try:
        names = mgr.run(job, timeout=120, label="edge-profile-test")
    finally:
        mgr.shutdown()
    assert names == ["sid"]
    profile = tmp_path / "localappdata" / "AiSquaredTravel" / "browser_profiles" / "edge-test"
    assert profile.is_dir() and any(profile.iterdir())


@pytest.mark.live
@needs_edge
def test_live_edge_fetches_example_com() -> None:
    mgr = BrowserManager(channel="msedge", headless=True)

    def job(handle: BrowserHandle) -> str:
        page = handle.new_context().new_page()
        page.goto("https://example.com/", wait_until="domcontentloaded", timeout=30_000)
        return page.title()

    try:
        assert "Example Domain" in mgr.run(job, timeout=90, label="live-example")
    finally:
        mgr.shutdown()
