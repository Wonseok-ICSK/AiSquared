"""BrowserManager: every Playwright call runs on one dedicated worker thread (DESIGN §9, §13.2).

Playwright's sync API is thread-bound and starts a Node driver subprocess, which needs a
Proactor event loop on Windows. uvicorn uses a *Selector* loop under ``--reload``, so the
API is never used on the server's loop thread: jobs are submitted to a single
``ThreadPoolExecutor(max_workers=1, thread_name_prefix="playwright")`` and each job runs
inside a fresh ``with sync_playwright() as p:`` (fresh driver per job, nothing leaks, access
to the persistent profiles is serialized).

Usage::

    def job(handle: BrowserHandle) -> str:
        context = handle.new_context()          # launches the default browser on demand
        page = context.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        return page.content()

    html = get_browser_manager().run(job, timeout=120, label="google_flights")
    # from async code: await asyncio.wrap_future(get_browser_manager().submit(job, "x"))

No stealth/evasion flags and no user-agent spoofing are ever added here.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sys
import threading
import time
import warnings
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, TypeVar

from playwright.sync_api import Browser, BrowserContext, Page, Playwright, sync_playwright
from playwright.sync_api import Error as PlaywrightError

from app.browser.sessions import profile_dir, read_profile_channel, write_profile_channel
from app.config import get_settings
from app.timeutil import utcnow

logger = logging.getLogger(__name__)

T = TypeVar("T")

WORKER_THREAD_PREFIX = "playwright"
DEFAULT_JOB_TIMEOUT_S = 180.0
DEFAULT_SHUTDOWN_WAIT_S = 30.0
LAUNCH_TIMEOUT_MS = 60_000
SCREENSHOT_TIMEOUT_MS = 15_000
MAX_SCREENSHOTS = 50
"""Older screenshots are pruned (they may show logged-in pages)."""

BUNDLED_CHANNEL = "chromium"
"""Playwright's own Chromium build (needs ``python -m playwright install chromium``)."""
FALLBACK_CHANNELS: tuple[str, ...] = ("msedge", "chrome", BUNDLED_CHANNEL)
OFFSCREEN_ARGS: list[str] = ["--window-position=-32000,-32000", "--window-size=1366,900"]
"""Launch args for a *headed* window the user should not see (e.g. sites rejecting headless)."""
CONTEXT_DEFAULTS: dict[str, Any] = {
    "locale": "ko-KR",
    "timezone_id": "Asia/Seoul",
    "viewport": {"width": 1366, "height": 900},
}

PERSISTENT_RESERVED_OPTIONS = frozenset({"channel", "user_data_dir", "executable_path"})
"""``persistent_context`` keywords the handle controls itself (rejected with ValueError)."""

_CHANNEL_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
_SLUG_RE = re.compile(r"[^A-Za-z0-9_-]+")
_MISSING_BROWSER_MARKERS = ("is not found at", "executable doesn't exist")
_PROFILE_LOCK_MARKERS = ("exitcode=21", "target page, context or browser has been closed")
_INSTALL_HINT = (
    "Install Microsoft Edge (preferred) or run `python -m playwright install chromium` "
    "for the bundled fallback (never `playwright install msedge`)."
)

PlaywrightFactory = Callable[[], Any]
"""Returns a context manager yielding a sync ``Playwright`` (default: ``sync_playwright``)."""


# --------------------------------------------------------------------------- errors


class BrowserError(RuntimeError):
    """Base class for browser-layer failures."""


class BrowserUnavailableError(BrowserError):
    """No browser channel could be launched (or Playwright cannot start in this process)."""


class BrowserProfileLockedError(BrowserError):
    """A persistent profile is already used by another browser process."""


class BrowserJobCancelled(BrowserError):
    """The job was cancelled (timeout or shutdown); its handle may no longer be used."""


# --------------------------------------------------------------------------- pure helpers


def normalize_channel(channel: str | None) -> str | None:
    """Lower-case/strip a channel name; ``None``/blank -> ``None``; invalid -> ``ValueError``."""
    if channel is None or not channel.strip():
        return None
    value = channel.strip().lower()
    if not _CHANNEL_RE.match(value):
        raise ValueError(f"invalid browser channel {channel!r}")
    return value


def channel_candidates(configured: str | None, preferred: str | None = None) -> list[str]:
    """Channels to try, in order: ``preferred`` (last one that worked), ``configured``,
    then ``msedge`` -> ``chrome`` -> bundled ``chromium``; de-duplicated."""
    order = [preferred, configured, *FALLBACK_CHANNELS]
    return list(dict.fromkeys(channel for channel in order if channel))


def persistent_launch_options(
    overrides: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], Any]:
    """Split ``persistent_context`` kwargs into launch options and a ``storage_state``.

    Applies :func:`context_options` defaults and ``timeout`` = :data:`LAUNCH_TIMEOUT_MS`
    unless given. Keys the handle controls (:data:`PERSISTENT_RESERVED_OPTIONS`) raise
    ``ValueError``; ``headless``/``args`` are explicit parameters of ``persistent_context``.
    """
    options = dict(overrides or {})
    reserved = sorted(PERSISTENT_RESERVED_OPTIONS.intersection(options))
    if reserved:
        raise ValueError(
            f"persistent_context() sets {', '.join(reserved)} itself "
            "(the channel comes from BrowserManager.configure / the profile)"
        )
    storage_state = options.pop("storage_state", None)
    options = context_options(options)
    options.setdefault("timeout", LAUNCH_TIMEOUT_MS)
    return options, storage_state


def context_options(overrides: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Context kwargs: :data:`CONTEXT_DEFAULTS` overlaid with ``overrides``.

    ``no_viewport=True`` drops the default viewport (window-sized pages, e.g. login windows);
    ``storage_state=None`` is dropped.
    """
    options = {**CONTEXT_DEFAULTS, "viewport": dict(CONTEXT_DEFAULTS["viewport"])}
    options.update(overrides or {})
    if options.get("no_viewport"):
        options.pop("viewport", None)
    if options.get("storage_state", ...) is None:
        options.pop("storage_state")
    return options


def _first_line(exc: BaseException) -> str:
    text = str(exc).strip()
    return text.splitlines()[0] if text else type(exc).__name__


def _is_missing_browser(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in _MISSING_BROWSER_MARKERS)


def _is_profile_locked(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in _PROFILE_LOCK_MARKERS)


def _no_browser_message(errors: Iterable[tuple[str, str]]) -> str:
    tried = "; ".join(f"{channel}: {reason}" for channel, reason in errors)
    return f"No browser could be launched (tried {tried}). {_INSTALL_HINT}"


def _cookie_expired(cookie: Mapping[str, Any], now: float) -> bool:
    try:
        expires = float(cookie.get("expires", -1))
    except (TypeError, ValueError):
        return False
    return 0 < expires < now  # -1 = session cookie


def _live_cookies(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Cookies of a ``storage_state`` dict that have not expired (session cookies kept)."""
    now = time.time()
    cookies = state.get("cookies") or []
    return [
        dict(cookie)
        for cookie in cookies
        if isinstance(cookie, Mapping) and not _cookie_expired(cookie, now)
    ]


def _slug(name: str) -> str:
    return _SLUG_RE.sub("-", name).strip("-")[:60] or "page"


def _prune_screenshots(directory: Path, keep: int | None = None) -> None:
    """Delete all but the newest ``keep`` (default :data:`MAX_SCREENSHOTS`) screenshots."""
    limit = MAX_SCREENSHOTS if keep is None else keep
    shots = sorted(directory.glob("*.png"), key=lambda path: path.name)  # names start with UTC ts
    for old in shots[:-limit] if limit > 0 else shots:
        old.unlink(missing_ok=True)


def _check_event_loop_policy() -> None:
    """Fail clearly if a global Selector policy would break Playwright's driver subprocess."""
    if sys.platform != "win32":
        return
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        policy = asyncio.get_event_loop_policy()
    selector_policy = getattr(asyncio, "WindowsSelectorEventLoopPolicy", None)
    if selector_policy is not None and isinstance(policy, selector_policy):
        raise BrowserUnavailableError(
            "The global asyncio policy is WindowsSelectorEventLoopPolicy (uvicorn <= 0.35?); "
            "Playwright cannot start its driver. Use uvicorn >= 0.36."
        )


# --------------------------------------------------------------------------- handle


class BrowserHandle:
    """What a job function gets: launch browsers/contexts on the worker thread.

    Valid only inside the job function and only on the worker thread. Everything it opens
    is closed when the job returns. After the job is cancelled (timeout/shutdown) every
    method raises :class:`BrowserJobCancelled`; long-running jobs should poll
    :attr:`cancelled` (or call :meth:`check_cancelled`) between steps.
    """

    _preferred_channel: ClassVar[str | None] = None
    _channel_lock: ClassVar[threading.Lock] = threading.Lock()

    def __init__(
        self,
        playwright: Playwright,
        *,
        channel: str | None,
        headless: bool,
        cancel_event: threading.Event,
        label: str = "",
    ) -> None:
        self._playwright = playwright
        self._channel = channel
        self._headless = headless
        self._cancel_event = cancel_event
        self.label = label
        self._thread_id = threading.get_ident()
        self._browsers: list[Browser] = []
        self._browser_headless: dict[int, bool] = {}  # id(browser) -> launched headless?
        self._contexts: list[BrowserContext] = []
        self._channel_used: str | None = None
        self._closed = False

    # ---------------------------------------------------------------- state

    @classmethod
    def preferred_channel(cls) -> str | None:
        """The channel cached as working by an earlier job (tried first), if any."""
        return cls._preferred_channel

    @classmethod
    def reset_channel_cache(cls) -> None:
        """Forget the cached working channel (e.g. after the configured channel changed)."""
        with cls._channel_lock:
            cls._preferred_channel = None

    @classmethod
    def _remember_channel(cls, channel: str) -> None:
        with cls._channel_lock:
            if cls._preferred_channel != channel:
                logger.info("Browser channel %r works; trying it first from now on", channel)
            cls._preferred_channel = channel

    @property
    def cancelled(self) -> bool:
        """True once the job was cancelled (timeout or manager shutdown)."""
        return self._cancel_event.is_set()

    @property
    def playwright(self) -> Playwright:
        """The job's ``Playwright`` instance (for anything the helpers do not cover)."""
        self._ensure_usable()
        return self._playwright

    @property
    def channel_used(self) -> str | None:
        """Channel of the last browser/context this handle launched (``None`` before)."""
        return self._channel_used

    def check_cancelled(self) -> None:
        """Raise :class:`BrowserJobCancelled` if the job was cancelled."""
        if self._cancel_event.is_set():
            raise BrowserJobCancelled(f"browser job {self.label!r} was cancelled")

    def _ensure_usable(self) -> None:
        if self._closed:
            raise BrowserJobCancelled("BrowserHandle used after its job finished")
        self.check_cancelled()
        if threading.get_ident() != self._thread_id:
            raise RuntimeError("BrowserHandle may only be used on the Playwright worker thread")

    def _candidates(self) -> list[str]:
        return channel_candidates(self._channel, type(self)._preferred_channel)

    # ---------------------------------------------------------------- launching

    def launch(self, headless: bool | None = None, args: list[str] | None = None) -> Browser:
        """Launch a browser, trying channels in order (see :func:`channel_candidates`).

        Any launch failure moves on to the next channel; the first channel that works is
        cached for later jobs. Raises :class:`BrowserUnavailableError` if none works.
        """
        self._ensure_usable()
        use_headless = self._headless if headless is None else headless
        errors: list[tuple[str, str]] = []
        for channel in self._candidates():
            self.check_cancelled()
            try:
                browser = self._playwright.chromium.launch(
                    channel=channel,
                    headless=use_headless,
                    args=list(args or []),
                    timeout=LAUNCH_TIMEOUT_MS,
                )
            except PlaywrightError as exc:
                errors.append((channel, _first_line(exc)))
                logger.warning("Browser channel %r failed to launch: %s", channel, _first_line(exc))
                continue
            self._browsers.append(browser)
            self._browser_headless[id(browser)] = use_headless
            self._on_launched(channel, use_headless, "browser")
            self._remember_channel(channel)
            return browser
        raise BrowserUnavailableError(_no_browser_message(errors))

    def new_context(
        self,
        browser: Browser | None = None,
        *,
        profile: str | None = None,
        headless: bool | None = None,
        **kwargs: Any,
    ) -> BrowserContext:
        """A throwaway context (defaults: ``ko-KR``, ``Asia/Seoul``, 1366x900 viewport).

        ``browser`` defaults to a browser this handle already launched in the requested
        ``headless`` mode (``None`` = any mode, else the default mode); a new one is
        launched if none matches. ``storage_state`` may be a dict (decrypted session). With
        ``profile`` this delegates to :meth:`persistent_context` (DESIGN §9 signature).
        """
        if profile is not None:
            return self.persistent_context(profile, headless=headless, **kwargs)
        self._ensure_usable()
        target = browser or self._default_browser(headless)
        context = target.new_context(**context_options(kwargs))
        self._contexts.append(context)
        return context

    def persistent_context(
        self,
        profile: str,
        headless: bool | None = None,
        args: list[str] | None = None,
        **kwargs: Any,
    ) -> BrowserContext:
        """Launch a persistent context on the dedicated profile ``sessions.profile_dir(profile)``.

        A profile is pinned to the channel that created it (recorded in the profile
        folder): it is reopened with that channel even if the configured channel or the
        cross-job cache of :meth:`launch` changed. A new profile tries the configured
        channel, then ``msedge`` -> ``chrome`` -> ``chromium``. The next channel is tried
        only if a browser is *not installed*; any other failure (e.g. the profile is locked
        by a running browser) raises at once, so a transient error never moves a profile
        to another browser. ``timeout`` defaults to :data:`LAUNCH_TIMEOUT_MS`; ``channel``
        and ``user_data_dir`` are rejected (``ValueError``). A ``storage_state`` dict is
        applied by adding its non-expired cookies (session cookies do not survive
        persistent-profile restarts; localStorage does).
        """
        self._ensure_usable()
        options, storage_state = persistent_launch_options(kwargs)
        user_data_dir = profile_dir(profile)
        recorded = self._recorded_channel(profile)
        use_headless = self._headless if headless is None else headless
        errors: list[tuple[str, str]] = []
        for channel in channel_candidates(self._channel, recorded):
            self.check_cancelled()
            try:
                context = self._playwright.chromium.launch_persistent_context(
                    str(user_data_dir),
                    channel=channel,
                    headless=use_headless,
                    args=list(args or []),
                    **options,
                )
            except PlaywrightError as exc:
                if _is_missing_browser(exc):
                    errors.append((channel, _first_line(exc)))
                    logger.warning("Browser channel %r is not installed", channel)
                    continue
                if _is_profile_locked(exc):
                    raise BrowserProfileLockedError(
                        f"Browser profile {profile!r} is in use by another browser process; "
                        "close that window (or wait for the running job) and retry."
                    ) from exc
                raise
            self._contexts.append(context)
            self._on_launched(channel, use_headless, f"profile {profile!r}")
            if channel != recorded:
                self._pin_profile(profile, channel, recorded)
            if storage_state:
                self._restore_cookies(context, storage_state)
            return context
        raise BrowserUnavailableError(_no_browser_message(errors))

    @staticmethod
    def _recorded_channel(profile: str) -> str | None:
        raw = read_profile_channel(profile)
        try:
            return normalize_channel(raw)
        except ValueError:
            logger.warning("Ignoring invalid channel marker in browser profile %r", profile)
            return None

    @staticmethod
    def _pin_profile(profile: str, channel: str, previous: str | None) -> None:
        if previous is not None:
            logger.warning(
                "Browser profile %r was created by %r, which is not installed; "
                "it is now opened with %r",
                profile,
                previous,
                channel,
            )
        write_profile_channel(profile, channel)

    def _default_browser(self, headless: bool | None) -> Browser:
        """First connected browser of this handle in the requested mode, else launch one."""
        for browser in self._browsers:
            if not browser.is_connected():
                continue
            if headless is None or self._browser_headless.get(id(browser)) == headless:
                return browser
        return self.launch(headless=headless)

    def _on_launched(self, channel: str, headless: bool, what: str) -> None:
        self._channel_used = channel
        logger.info(
            "Browser job %r: launched %s on channel %r (%s)",
            self.label,
            what,
            channel,
            "headless" if headless else "headed",
        )

    @staticmethod
    def _restore_cookies(context: BrowserContext, state: Mapping[str, Any]) -> None:
        cookies = _live_cookies(state)
        if not cookies:
            return
        try:
            context.add_cookies(cookies)  # type: ignore[arg-type]
        except PlaywrightError as exc:
            logger.warning("Could not restore saved cookies: %s", _first_line(exc))
            return
        logger.info("Restored %d saved cookies into the persistent context", len(cookies))

    # ---------------------------------------------------------------- utilities

    def screenshot(self, page: Page, name: str) -> Path | None:
        """Save a PNG to ``<logs_dir>/screenshots/<utc-ts>-<name>.png``; never raises.

        Returns the path, or ``None`` if the screenshot could not be taken.
        """
        try:
            directory = get_settings().logs_dir / "screenshots"
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f"{utcnow():%Y%m%dT%H%M%S%f}Z-{_slug(name)}.png"
            page.screenshot(path=str(path), timeout=SCREENSHOT_TIMEOUT_MS)
            _prune_screenshots(directory)
        except Exception as exc:  # noqa: BLE001 - diagnostics must never break a job
            logger.warning("Screenshot %r failed: %s", name, _first_line(exc))
            return None
        logger.info("Saved screenshot %s", path)
        return path

    def _close_all(self) -> None:
        """Close every context and browser this handle opened (errors logged, not raised)."""
        for context in reversed(self._contexts):
            try:
                context.close()
            except Exception as exc:  # noqa: BLE001
                logger.debug("Closing context failed: %s", _first_line(exc))
        for browser in reversed(self._browsers):
            try:
                browser.close()
            except Exception as exc:  # noqa: BLE001
                logger.debug("Closing browser failed: %s", _first_line(exc))
        self._contexts.clear()
        self._browsers.clear()
        self._browser_headless.clear()
        self._closed = True


# --------------------------------------------------------------------------- manager


@dataclass
class _Job:
    label: str
    cancel: threading.Event = field(default_factory=threading.Event)
    started: threading.Event = field(default_factory=threading.Event)
    """Set when the job starts running, or when its future completes (e.g. dropped)."""
    finished: threading.Event = field(default_factory=threading.Event)


class BrowserManager:
    """Owns the single Playwright worker thread (see the module docstring).

    ``channel``/``headless`` default to ``BROWSER_CHANNEL``/``BROWSER_HEADLESS``
    (``config.get_settings()``, read at each job start) unless set via the constructor or
    :meth:`configure`. ``playwright_factory`` is injectable for tests.
    """

    def __init__(
        self,
        channel: str | None = None,
        headless: bool | None = None,
        *,
        playwright_factory: PlaywrightFactory = sync_playwright,
    ) -> None:
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=WORKER_THREAD_PREFIX)
        self._factory = playwright_factory
        self._channel = normalize_channel(channel)
        self._headless = headless
        self._lock = threading.Lock()
        self._active: _Job | None = None
        self._worker_ident: int | None = None
        self._closed = False

    # ---------------------------------------------------------------- configuration

    @property
    def channel(self) -> str:
        """Effective preferred channel (configured, else ``BROWSER_CHANNEL``, else msedge)."""
        if self._channel:
            return self._channel
        configured = get_settings().browser_channel
        try:
            return normalize_channel(configured) or FALLBACK_CHANNELS[0]
        except ValueError:
            logger.warning("Ignoring invalid BROWSER_CHANNEL %r", configured)
            return FALLBACK_CHANNELS[0]

    @property
    def headless(self) -> bool:
        """Effective default headless mode (configured, else ``BROWSER_HEADLESS``)."""
        return get_settings().browser_headless if self._headless is None else self._headless

    @property
    def current_label(self) -> str | None:
        """Label of the job running right now (``None`` when idle)."""
        active = self._active
        return active.label if active else None

    @property
    def is_shut_down(self) -> bool:
        """True after :meth:`shutdown`."""
        return self._closed

    def configure(self, channel: str | None = None, headless: bool | None = None) -> None:
        """Update the defaults for later jobs; ``None`` leaves a value unchanged.

        Changing the channel also forgets the cached working channel so the new
        preference is tried first.
        """
        new_channel = normalize_channel(channel)
        with self._lock:
            if new_channel is not None and new_channel != self._channel:
                self._channel = new_channel
                BrowserHandle.reset_channel_cache()
                logger.info("Browser channel set to %r", new_channel)
            if headless is not None:
                self._headless = headless

    # ---------------------------------------------------------------- jobs

    def submit(self, fn: Callable[[BrowserHandle], T], label: str = "") -> Future[T]:
        """Queue ``fn`` on the worker thread; returns its ``Future`` (no timeout).

        From async code: ``await asyncio.wrap_future(manager.submit(fn, "label"))``.
        """
        future, _job = self._submit(fn, label)
        return future

    def run(
        self,
        fn: Callable[[BrowserHandle], T],
        timeout: float = DEFAULT_JOB_TIMEOUT_S,
        label: str = "",
        *,
        queue_timeout: float | None = None,
    ) -> T:
        """Run ``fn(handle)`` on the worker thread and return its result.

        ``timeout`` covers queueing + execution. With ``queue_timeout`` the job must
        *start* within ``queue_timeout`` seconds and ``timeout`` then counts from its
        start (for jobs that enforce their own deadline, e.g. an interactive login).
        On timeout a ``TimeoutError`` is raised: a job that never started is dropped; a
        running job is marked cancelled so its next handle call raises
        :class:`BrowserJobCancelled`. Python cannot interrupt the worker, so it keeps
        running until ``fn`` returns (its browsers are closed then) and later jobs wait
        behind it. Blocks the calling thread: never call it on an event-loop thread (use
        :meth:`submit`).
        """
        _warn_if_on_event_loop(label)
        future, job = self._submit(fn, label)
        if queue_timeout is not None:
            self._await_start(future, job, queue_timeout)
        try:
            return future.result(timeout=timeout)
        except CancelledError:
            raise BrowserJobCancelled(f"browser job {label!r} was cancelled (shutdown)") from None
        except TimeoutError:
            if future.done():
                raise  # fn itself raised TimeoutError
        return self._expire(future, job, timeout)

    def _await_start(self, future: Future[T], job: _Job, queue_timeout: float) -> None:
        """Wait until ``job`` starts (or completes); drop it if it is still queued."""
        if job.started.wait(queue_timeout):
            return
        busy_with = self.current_label
        if future.cancel():
            raise TimeoutError(
                f"browser job {job.label!r} did not start within {queue_timeout:g}s "
                f"(browser busy with {busy_with!r})"
            )
        # cancel() failed: it started just now and gets the full ``timeout``.

    def _expire(self, future: Future[T], job: _Job, timeout: float) -> T:
        """``run`` deadline passed: drop a queued job or cancel the running one."""
        label = job.label
        busy_with = self.current_label
        job.cancel.set()
        if future.cancel():
            raise TimeoutError(
                f"browser job {label!r} did not start within {timeout:g}s "
                f"(browser busy with {busy_with!r})"
            )
        if future.done():  # finished between the deadline and the cancel request
            return future.result()
        logger.warning(
            "Browser job %r exceeded %gs; cancelled (worker continues until it returns)",
            label,
            timeout,
        )
        raise TimeoutError(f"browser job {label!r} timed out after {timeout:g}s")

    def _submit(self, fn: Callable[[BrowserHandle], T], label: str) -> tuple[Future[T], _Job]:
        if threading.get_ident() == self._worker_ident:
            raise RuntimeError(
                "BrowserManager.run/submit called from inside a browser job would deadlock; "
                "use the BrowserHandle you were given"
            )
        job = _Job(label=label)
        with self._lock:
            if self._closed:
                raise BrowserError("BrowserManager is shut down")
            future = self._executor.submit(self._execute, fn, job)
        future.add_done_callback(lambda _future: job.started.set())  # wakes _await_start
        return future, job

    def _execute(self, fn: Callable[[BrowserHandle], T], job: _Job) -> T:
        """Worker-thread body: fresh Playwright per job; always closes what was opened."""
        self._worker_ident = threading.get_ident()
        with self._lock:
            self._active = job
        job.started.set()
        started = time.monotonic()
        logger.debug("Browser job %r started", job.label)
        try:
            if job.cancel.is_set():
                raise BrowserJobCancelled(f"browser job {job.label!r} was cancelled")
            _check_event_loop_policy()
            with self._factory() as playwright:
                handle = BrowserHandle(
                    playwright,
                    channel=self.channel,
                    headless=self.headless,
                    cancel_event=job.cancel,
                    label=job.label,
                )
                try:
                    return fn(handle)
                finally:
                    handle._close_all()
        finally:
            with self._lock:
                self._active = None
            job.finished.set()
            logger.info(
                "Browser job %r finished in %.1fs%s",
                job.label,
                time.monotonic() - started,
                " (cancelled)" if job.cancel.is_set() else "",
            )

    def shutdown(self, wait: bool = True, timeout: float = DEFAULT_SHUTDOWN_WAIT_S) -> None:
        """Stop accepting jobs, drop queued ones and cancel the running one.

        With ``wait``, block up to ``timeout`` seconds for the running job to return.
        """
        with self._lock:
            if self._closed:
                return
            self._closed = True
            active = self._active
        if active is not None:
            active.cancel.set()
        self._executor.shutdown(wait=False, cancel_futures=True)
        if wait and active is not None and not active.finished.wait(timeout):
            logger.warning("Browser job %r still running after shutdown", active.label)
        logger.info("BrowserManager shut down")


def _warn_if_on_event_loop(label: str) -> None:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    logger.warning(
        "BrowserManager.run(%r) called on an event-loop thread; it blocks the loop "
        "(use submit() + asyncio.wrap_future)",
        label,
    )


# --------------------------------------------------------------------------- singleton

_manager: BrowserManager | None = None
_manager_lock = threading.Lock()


def get_browser_manager() -> BrowserManager:
    """Process-wide manager (created on first use; recreated after a shutdown)."""
    global _manager
    with _manager_lock:
        if _manager is None or _manager.is_shut_down:
            _manager = BrowserManager()
        return _manager


def shutdown_browser_manager(wait: bool = True, timeout: float = DEFAULT_SHUTDOWN_WAIT_S) -> None:
    """Shut the singleton down (FastAPI lifespan shutdown) and forget it."""
    global _manager
    with _manager_lock:
        manager, _manager = _manager, None
    if manager is not None:
        manager.shutdown(wait=wait, timeout=timeout)
