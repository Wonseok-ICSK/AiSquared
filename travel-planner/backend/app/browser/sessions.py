"""Provider login sessions: dedicated profiles + encrypted ``storage_state`` (DESIGN §9, §13.2).

* Profiles live outside the project/Desktop/OneDrive folder, under
  ``%LOCALAPPDATA%\\AiSquaredTravel\\browser_profiles\\<key>`` (fallback when LOCALAPPDATA is
  unset: ``settings.browser_profiles_dir``). The real Edge profile is never touched. Treat a
  profile folder as a secret (its cookies are only DPAPI-protected, localStorage is plain).
  A marker file in the folder pins the profile to the browser channel that created it.
* ``storage_state`` exports are JSON -> ``SecretBox.encrypt_bytes`` ->
  ``<data>/sessions/<key>.state.enc`` (atomic replace); plaintext never touches the disk.
  They matter because *session* cookies do not survive a persistent-profile restart.
* :func:`start_login` opens a **headed** window on the profile; the user logs in and closes
  the window. Closing the last window ends the browser at once, so cookies are snapshotted
  while the window is open and the full ``storage_state()`` is exported whenever the
  context is still alive at the end (timeout/cancel). Only the login site's cookies are
  kept (a fresh Edge profile also holds the user's Microsoft-account cookies), and the
  browser job saves them itself so a finished login is never discarded.

Cookie values are never logged (only counts).
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import shutil
import stat
import tempfile
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import session_scope
from app.models import ProviderSession
from app.security.secrets import SecretBoxError, get_secretbox
from app.timeutil import utcnow

if TYPE_CHECKING:
    from playwright.sync_api import BrowserContext, Dialog

    from app.browser.manager import BrowserHandle, BrowserManager

logger = logging.getLogger(__name__)

APP_DIR_NAME = "AiSquaredTravel"
STATE_SUFFIX = ".state.enc"
PROFILE_CHANNEL_FILE = "aisquared-channel.txt"
"""Marker in a profile folder naming the browser channel that created it (never changes)."""
DEFAULT_LOGIN_TIMEOUT_S = 600.0
LOGIN_GRACE_S = 120.0
"""Extra manager timeout on top of the login wait (browser start, navigation, export, save)."""
LOGIN_QUEUE_TIMEOUT_S = 300.0
"""How long a login waits for earlier browser jobs before giving up (no window opens)."""
LOGIN_POLL_S = 0.5
"""How often the cookie snapshot is refreshed while the login window is open."""
LOGIN_NAV_TIMEOUT_MS = 60_000

SessionStatus = Literal["none", "active", "expired"]
LoginEnd = Literal["closed", "timeout", "cancelled"]
LoginStatus = Literal["active", "none", "error"]

_KEY_RE = re.compile(r"[^a-z0-9_-]+")
_MAX_KEY_LEN = 40  # ProviderSession.provider is String(40)
_GENERIC_SECOND_LEVEL = frozenset(
    {"ac", "co", "com", "edu", "go", "gov", "ne", "net", "or", "org", "pe", "re"}
)  # e.g. example.co.kr, example.or.kr, example.com.au
_WINDOWS_RESERVED = {"con", "prn", "aux", "nul"} | {
    f"{prefix}{n}" for prefix in ("com", "lpt") for n in range(10)
}


# --------------------------------------------------------------------------- paths


def sanitize_key(key: str) -> str:
    """Normalise a provider/session key for file names and the DB (``[a-z0-9_-]``, ≤ 40)."""
    cleaned = _KEY_RE.sub("_", (key or "").strip().lower()).strip("_-")[:_MAX_KEY_LEN]
    if not cleaned:
        raise ValueError(f"invalid session key {key!r}")
    return f"_{cleaned}"[:_MAX_KEY_LEN] if cleaned in _WINDOWS_RESERVED else cleaned


def profiles_root() -> Path:
    """Root folder of the dedicated browser profiles (not created here)."""
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / APP_DIR_NAME / "browser_profiles"
    return get_settings().browser_profiles_dir


def _profile_path(key: str) -> Path:
    return profiles_root() / sanitize_key(key)


def profile_dir(key: str) -> Path:
    """Dedicated profile folder for ``key`` (created on demand)."""
    path = _profile_path(key)
    path.mkdir(parents=True, exist_ok=True)
    return path


def state_path(key: str) -> Path:
    """Where the encrypted ``storage_state`` of ``key`` is stored (may not exist)."""
    return get_settings().sessions_dir / f"{sanitize_key(key)}{STATE_SUFFIX}"


def read_profile_channel(key: str) -> str | None:
    """Browser channel that created the profile ``key`` (``None`` if unknown/new)."""
    marker = _profile_path(key) / PROFILE_CHANNEL_FILE
    try:
        text = marker.read_text(encoding="ascii").strip()
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        logger.warning("Cannot read the channel marker of browser profile %r: %s", key, exc)
        return None
    return text or None


def write_profile_channel(key: str, channel: str) -> None:
    """Record that profile ``key`` belongs to ``channel`` (never raises; logged on failure)."""
    try:
        _atomic_write(profile_dir(key) / PROFILE_CHANNEL_FILE, channel.encode("ascii"))
    except (OSError, ValueError) as exc:
        logger.warning("Cannot record the channel of browser profile %r: %s", key, exc)


# --------------------------------------------------------------------------- storage_state


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def save_storage_state(key: str, state: dict[str, Any]) -> Path:
    """Encrypt ``state`` (a Playwright ``storage_state`` dict) to ``sessions/<key>.state.enc``."""
    if not isinstance(state, dict):
        raise TypeError("storage_state must be a dict")
    payload = json.dumps(state, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    path = state_path(key)
    _atomic_write(path, get_secretbox().encrypt_bytes(payload))
    logger.info(
        "Saved encrypted browser session %r (%d cookies, %d origins)",
        sanitize_key(key),
        len(state.get("cookies") or []),
        len(state.get("origins") or []),
    )
    return path


def load_storage_state(key: str) -> dict[str, Any] | None:
    """Decrypt the saved ``storage_state`` of ``key``; ``None`` if missing or unreadable."""
    path = state_path(key)
    try:
        token = path.read_bytes()
    except FileNotFoundError:
        logger.debug("No saved browser session for %r", sanitize_key(key))
        return None
    except OSError as exc:
        logger.warning("Cannot read browser session file %s: %s", path, exc)
        return None
    try:
        state = json.loads(get_secretbox().decrypt_bytes(token).decode("utf-8"))
    except SecretBoxError:
        logger.warning("Browser session %s cannot be decrypted (key changed?); ignored", path)
        return None
    except ValueError:  # JSONDecodeError / UnicodeDecodeError
        logger.warning("Browser session %s is corrupt; ignoring it", path)
        return None
    if not isinstance(state, dict):
        logger.warning("Browser session %s does not hold a storage_state object", path)
        return None
    return state


# --------------------------------------------------------------------------- DB status


def session_status(db: Session, key: str) -> SessionStatus:
    """``none``, ``active`` or ``expired`` (``active`` without a state file counts as expired)."""
    row = db.get(ProviderSession, sanitize_key(key))
    if row is None or row.status not in ("active", "expired"):
        return "none"
    if row.status == "active" and not state_path(key).is_file():
        return "expired"
    return "active" if row.status == "active" else "expired"


def mark_session_expired(db: Session, key: str) -> None:
    """Flag an ``active`` session as ``expired`` (e.g. a provider hit a login wall); commits."""
    row = db.get(ProviderSession, sanitize_key(key))
    if row is None or row.status != "active":
        return
    row.status = "expired"
    row.updated_at = utcnow()
    db.commit()
    logger.info("Browser session %r marked expired", row.provider)


def _rmtree_onexc(func: Callable[..., Any], path: str, _exc: BaseException) -> None:
    os.chmod(path, stat.S_IWRITE)  # read-only files (Windows)
    func(path)


def _remove_profile(key: str) -> bool:
    path = _profile_path(key)
    if not path.exists():
        return True
    try:
        shutil.rmtree(path, onexc=_rmtree_onexc)
    except OSError as exc:
        logger.warning("Could not delete browser profile %s (browser still open?): %s", path, exc)
        return False
    return True


def delete_session(db: Session, key: str) -> bool:
    """Forget a login: delete the state file and the profile folder, set the row to ``none``.

    Commits. Returns False if some file could not be removed (e.g. the profile is in use
    by an open browser window); the DB row is reset regardless.
    """
    key = sanitize_key(key)
    removed = True
    try:
        state_path(key).unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("Could not delete browser session file for %r: %s", key, exc)
        removed = False
    removed = _remove_profile(key) and removed
    row = db.get(ProviderSession, key)
    if row is not None:
        row.status = "none"
        row.storage_path = None
        row.account_hint = None
        row.updated_at = utcnow()
        db.commit()
    logger.info("Browser session %r deleted%s", key, "" if removed else " (partially)")
    return removed


def _mark_active(key: str, path: Path) -> None:
    with session_scope() as db:
        row = db.get(ProviderSession, key)
        if row is None:
            row = ProviderSession(provider=key)
            db.add(row)
        row.status = "active"
        row.storage_path = str(path)
        row.updated_at = utcnow()


# --------------------------------------------------------------------------- interactive login


@dataclass
class _LoginWatch:
    """State observed while the headed login window is open."""

    cookies: list[dict[str, Any]] = field(default_factory=list)
    dialogs: list[Dialog] = field(default_factory=list)
    closed: bool = False

    # Handlers are bound methods: Playwright's ``on()`` tags handlers with ``setattr``, which
    # fails for builtins such as ``list.append``.
    def mark_closed(self, *_args: Any) -> None:
        """``close`` event handler."""
        self.closed = True

    def add_dialog(self, dialog: Dialog) -> None:
        """``dialog`` event handler: keep the dialog open for the user, remember it."""
        self.dialogs.append(dialog)

    def snapshot(self, context: BrowserContext) -> None:
        """Remember the current cookies (cheap; never opens pages)."""
        try:
            self.cookies = [dict(cookie) for cookie in context.cookies()]
        except PlaywrightError:
            self.closed = True


@dataclass(frozen=True)
class _LoginCapture:
    """What a login must capture to count: cookies of ``domains``, incl. a ``login_cookies``."""

    domains: tuple[str, ...]
    login_cookies: frozenset[str] = frozenset()


@dataclass
class _LoginResult:
    status: LoginStatus
    cookies: int
    end: LoginEnd
    error: str = ""


def _open_pages(context: BrowserContext) -> list[Any]:
    return [page for page in context.pages if not page.is_closed()]


def _pump_until_closed(context: BrowserContext, watch: _LoginWatch, seconds: float) -> bool:
    """Let Playwright dispatch events for up to ``seconds``; True once the context closed."""
    if watch.closed:
        return True
    try:
        context.wait_for_event("close", timeout=max(seconds, 0.01) * 1000)
    except PlaywrightTimeoutError:
        return watch.closed
    except PlaywrightError:
        watch.closed = True
        return True
    watch.closed = True
    return True


def _wait_for_user(
    handle: BrowserHandle,
    context: BrowserContext,
    watch: _LoginWatch,
    deadline: float,
    poll_s: float,
) -> LoginEnd:
    """Block until the user closed every window, the deadline passed or the job was cancelled."""
    while True:
        if handle.cancelled:
            return "cancelled"
        if not watch.closed and not _open_pages(context):
            return "closed"
        watch.snapshot(context)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return "timeout"
        if _pump_until_closed(context, watch, min(poll_s, remaining)):
            return "closed"


def _dismiss_dialogs(dialogs: list[Dialog]) -> None:
    for dialog in dialogs:
        try:
            dialog.dismiss()
        except PlaywrightError:
            pass  # already handled by the user


def _final_state(context: BrowserContext, watch: _LoginWatch) -> dict[str, Any]:
    """Full ``storage_state()`` if the context is still alive, else the last cookie snapshot."""
    if not watch.closed:
        _dismiss_dialogs(watch.dialogs)  # an open dialog would block the export
        try:
            return dict(context.storage_state())
        except PlaywrightError as exc:
            logger.info("storage_state export failed (%s); using snapshot", type(exc).__name__)
    return {"cookies": list(watch.cookies), "origins": []}


def _domain_related(host: str, domain: str) -> bool:
    """True if one is ``domain`` or a sub-domain of the other (cookie scoping both ways)."""
    return host == domain or host.endswith(f".{domain}") or domain.endswith(f".{host}")


def filter_storage_state(state: dict[str, Any], domains: Sequence[str]) -> dict[str, Any]:
    """Keep only cookies/origins related to ``domains`` (parents and sub-domains included).

    E.g. ``["naver.com"]`` keeps ``.naver.com`` and ``nid.naver.com`` cookies but drops the
    Microsoft-account cookies Edge's implicit sign-in adds to a fresh headed profile.
    """
    wanted = [d.strip().lstrip(".").lower() for d in domains if d and d.strip()]

    def related(host: str) -> bool:
        host = host.strip().lstrip(".").lower()
        return bool(host) and any(_domain_related(host, domain) for domain in wanted)

    cookies = [c for c in state.get("cookies") or [] if related(str(c.get("domain", "")))]
    origins = [
        o
        for o in state.get("origins") or []
        if related(urlsplit(str(o.get("origin", ""))).hostname or "")
    ]
    return {**state, "cookies": cookies, "origins": origins}


def site_domain(host: str) -> str:
    """Approximate registrable domain of ``host`` (no public-suffix list).

    ``nid.naver.com`` -> ``naver.com``; ``www.example.co.kr`` -> ``example.co.kr`` (generic
    second-level labels such as ``co``/``or``/``go`` under a two-letter TLD are kept). IP
    addresses and hosts with at most two labels are returned unchanged.
    """
    host = host.strip().strip("[]").rstrip(".").lower()
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return host
    labels = host.split(".")
    if len(labels) <= 2:
        return host
    generic_sld = len(labels[-1]) == 2 and labels[-2] in _GENERIC_SECOND_LEVEL
    return ".".join(labels[-3:] if generic_sld else labels[-2:])


def _validate_login_url(url: str) -> str:
    """The lower-case host of an absolute http(s) ``url`` (``ValueError`` otherwise)."""
    parts = urlsplit(url or "")
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"login URL must be an absolute http(s) URL, got {url!r}")
    return parts.hostname


def _login_capture(
    host: str, cookie_domains: Sequence[str] | None, login_cookies: Sequence[str] | None
) -> _LoginCapture:
    domains = [site_domain(host), *(d.strip() for d in cookie_domains or [] if d and d.strip())]
    return _LoginCapture(
        domains=tuple(dict.fromkeys(domains)),
        login_cookies=frozenset(name for name in login_cookies or [] if name),
    )


def _persist_login(
    key: str, state: dict[str, Any], end: LoginEnd, capture: _LoginCapture
) -> _LoginResult:
    """Filter ``state`` to the provider's site and save it if it holds a login.

    Runs inside the browser job, so a captured login is saved even when the caller of
    ``run()`` already gave up waiting.
    """
    kept = filter_storage_state(state, capture.domains)
    cookies = kept["cookies"]
    names = {str(cookie.get("name", "")) for cookie in cookies}
    if not cookies or (capture.login_cookies and not capture.login_cookies & names):
        logger.info(
            "Interactive login for %r captured no login cookies for %s (%d related cookies); "
            "nothing saved",
            key,
            ", ".join(capture.domains),
            len(cookies),
        )
        return _LoginResult(status="none", cookies=0, end=end)
    try:
        path = save_storage_state(key, kept)
        _mark_active(key, path)
    except (OSError, ValueError, SecretBoxError, SQLAlchemyError) as exc:
        logger.warning("Could not save the login session for %r: %s", key, type(exc).__name__)
        return _LoginResult(status="error", cookies=0, end=end, error=type(exc).__name__)
    dropped = len(state.get("cookies") or []) - len(cookies)
    if dropped:
        logger.info("Login %r: ignored %d cookies of unrelated sites", key, dropped)
    return _LoginResult(status="active", cookies=len(cookies), end=end)


def _login_job(
    handle: BrowserHandle,
    key: str,
    url: str,
    timeout_s: float,
    poll_s: float,
    capture: _LoginCapture,
) -> _LoginResult:
    """Worker-thread body of :func:`start_login`; the login deadline counts from here."""
    deadline = time.monotonic() + timeout_s
    context = handle.persistent_context(key, headless=False, no_viewport=True)
    watch = _LoginWatch()
    context.on("close", watch.mark_closed)
    # A listener keeps JS dialogs open for the user (Playwright auto-dismisses them otherwise).
    context.on("dialog", watch.add_dialog)
    page = context.pages[0] if context.pages else context.new_page()
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=LOGIN_NAV_TIMEOUT_MS)
    except PlaywrightError as exc:  # leave the window open: the user can retry/navigate
        logger.warning("Login page for %r did not load cleanly: %s", key, type(exc).__name__)
    end = _wait_for_user(handle, context, watch, deadline, poll_s)
    return _persist_login(key, _final_state(context, watch), end, capture)


def _login_message(result: _LoginResult, timeout_s: float, site: str) -> str:
    if result.status == "error":
        return f"Could not save the session ({result.error})."
    if result.status == "none":
        return (
            f"No login cookies for {site} were captured; the login was probably not "
            "completed (nothing saved)."
        )
    cookies = result.cookies
    if result.end == "closed":
        return f"Login window closed; saved the session ({cookies} cookies)."
    if result.end == "timeout":
        return (
            f"Login window timed out after {timeout_s:g}s; "
            f"saved the current session ({cookies} cookies)."
        )
    return f"Login was cancelled; saved the current session ({cookies} cookies)."


def start_login(
    provider_key: str,
    url: str,
    timeout_s: float = DEFAULT_LOGIN_TIMEOUT_S,
    manager: BrowserManager | None = None,
    *,
    cookie_domains: Sequence[str] | None = None,
    login_cookies: Sequence[str] | None = None,
    poll_s: float = LOGIN_POLL_S,
) -> dict[str, Any]:
    """Open a headed window on the provider's profile at ``url`` and capture the login.

    Blocks until the user closes the window or ``timeout_s`` passes (counted from when the
    window opens; waiting for earlier browser jobs is capped at
    :data:`LOGIN_QUEUE_TIMEOUT_S`); call it from a background thread. Only cookies and
    localStorage of the login URL's site (see :func:`site_domain`) plus the optional extra
    ``cookie_domains`` are saved. ``login_cookies`` (e.g. ``["NID_AUT"]``) names cookies
    that prove a completed login; without it any cookie of those sites counts. The capture
    is saved by the browser job itself, so it is never lost when this call stops waiting.

    Returns ``{"status": "active" | "none" | "error", "cookies": n, "message": str}``:
    ``active`` = encrypted state saved and ``ProviderSession`` marked active, ``none`` = no
    login captured (nothing changed), ``error`` = the browser or the save failed.
    """
    from app.browser.manager import BrowserError, _first_line, get_browser_manager

    key = sanitize_key(provider_key)
    host = _validate_login_url(url)
    capture = _login_capture(host, cookie_domains, login_cookies)
    browser = manager or get_browser_manager()
    window_opening = threading.Event()

    def job(handle: BrowserHandle) -> _LoginResult:
        window_opening.set()
        return _login_job(handle, key, url, timeout_s, poll_s, capture)

    logger.info("Starting interactive login for %r at %s (timeout %gs)", key, host, timeout_s)
    try:
        result = browser.run(
            job,
            timeout=timeout_s + LOGIN_GRACE_S,
            label=f"login:{key}",
            queue_timeout=LOGIN_QUEUE_TIMEOUT_S,
        )
    except (BrowserError, PlaywrightError, TimeoutError) as exc:
        message = _first_line(exc)
        logger.warning("Interactive login for %r failed: %s", key, message)
        if isinstance(exc, TimeoutError) and window_opening.is_set():
            message += "; a login completed in that window is still saved when the job ends."
        return {"status": "error", "cookies": 0, "message": message}
    return {
        "status": result.status,
        "cookies": result.cookies,
        "message": _login_message(result, timeout_s, capture.domains[0]),
    }
