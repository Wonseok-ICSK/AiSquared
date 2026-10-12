"""Flight providers: readiness, background test searches and login sessions (DESIGN §7, §9).

* ``GET /providers`` — every registered provider as :class:`ProviderInfo`. Providers are
  instantiated with a :class:`ProviderContext` like :mod:`app.services.search` does and only
  ``status()`` is called: no browser is ever launched here.
* ``POST /providers/{key}/test`` — one small live search in a background thread (ICN → NRT,
  30 days ahead, 4 nights, 1 adult); ``GET /providers/{key}/test`` returns the latest state
  (the UI polls it every 2 s while ``status == "running"``). Results live in memory only.
* ``POST /providers/{key}/login`` — opens a headed login window (``sessions.start_login``)
  in the background for providers with ``supports_login``; ``DELETE
  /providers/{key}/session`` forgets the saved session.

Login-capable providers declare optional class attributes read here: ``login_url`` (required
to log in), ``login_cookie_domains`` and ``login_cookies`` (passed to ``start_login``).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Sequence
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app import settings_store
from app.browser import sessions
from app.config import get_settings
from app.db import get_db
from app.providers.flights import registry
from app.providers.flights.base import (
    FlightOffer,
    FlightProvider,
    FlightQuery,
    ProviderBlocked,
    ProviderContext,
    ProviderError,
    ProviderNotConfigured,
)
from app.schemas import MessageOut, ProviderInfo, ProviderTestOut
from app.services.search import safe_error
from app.settings_store import RuntimeSettings, load_runtime_settings
from app.timeutil import now_local, utcnow

logger = logging.getLogger(__name__)

router = APIRouter(tags=["providers"])

TEST_ORIGIN = "ICN"
TEST_DESTINATION = "NRT"
TEST_DAYS_AHEAD = 30
TEST_NIGHTS = 4
TEST_STALE_AFTER = timedelta(minutes=20)
"""A test still ``running`` after this long is reported as timed out (its thread is abandoned).
Longer than any provider's own limits (Naver: up to 10 min queued behind other browser jobs
+ 150 s per job)."""

_lock = threading.Lock()
_tests: dict[str, ProviderTestOut] = {}
_test_threads: dict[str, threading.Thread] = {}
_login_threads: dict[str, threading.Thread] = {}
_login_results: dict[str, dict[str, Any]] = {}


# --------------------------------------------------------------------------- helpers


def provider_class_or_404(key: str) -> type[FlightProvider]:
    """The registered provider class for ``key`` (HTTP 404 when unknown)."""
    try:
        return registry.get_provider_class(key)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"unknown flight provider {key!r}") from None


def _browser_manager() -> Any | None:
    """The shared BrowserManager (creating it starts no browser), or ``None`` if unavailable."""
    try:
        from app.browser.manager import get_browser_manager

        return get_browser_manager()
    except Exception as exc:  # noqa: BLE001 - providers report "not configured" instead
        logger.warning("Browser automation is not available: %s", safe_error(exc))
        return None


def provider_context(
    cls: type[FlightProvider],
    runtime: RuntimeSettings,
    get_secret: Callable[[str], str | None] = settings_store.get_secret,
) -> ProviderContext:
    """Context like ``services.search`` builds it: secrets, shared browser, per-provider options."""
    return ProviderContext(
        get_secret=get_secret,
        browser=_browser_manager() if cls.uses_browser else None,
        options=dict(runtime.provider_options.get(cls.key) or {}),
    )


def _login_alive(key: str) -> bool:
    thread = _login_threads.get(key)
    return thread is not None and thread.is_alive()


def _login_note(key: str, session_status: str | None) -> str:
    """Extra status text about an open login window or a failed last login attempt."""
    with _lock:
        if _login_alive(key):
            return "Login window open: log in there, then close the window."
        last = _login_results.get(key)
    if last and last.get("status") != "active" and session_status != "active":
        return f"Last login attempt: {last.get('message') or last.get('status')}"
    return ""


def _join(*parts: str) -> str:
    return " · ".join(part for part in parts if part)


def provider_info(
    db: Session, cls: type[FlightProvider], runtime: RuntimeSettings, enabled_keys: set[str]
) -> ProviderInfo:
    """:class:`ProviderInfo` for one provider class (status only, never a search)."""
    try:
        ctx = provider_context(cls, runtime, lambda name: settings_store.get_secret(name, db))
        status = cls(ctx).status()
        configured, message = bool(status.configured), status.message or ""
    except Exception as exc:  # noqa: BLE001 - one broken provider must not break the list
        logger.warning("Flight provider %s status failed: %s", cls.key, safe_error(exc))
        configured, message = False, f"could not be initialised: {safe_error(exc)}"
    session_status = sessions.session_status(db, cls.key) if cls.supports_login else None
    if cls.supports_login:
        message = _join(message, _login_note(cls.key, session_status))
    return ProviderInfo(
        key=cls.key,
        display_name=cls.display_name,
        description=cls.description or "",
        requires_api_key=bool(cls.requires_api_key),
        configured=configured,
        enabled_by_default=bool(cls.enabled_by_default),
        enabled=cls.key in enabled_keys,
        uses_browser=bool(cls.uses_browser),
        supports_login=bool(cls.supports_login),
        session_status=session_status,
        message=message,
    )


def build_provider_infos(db: Session, runtime: RuntimeSettings | None = None) -> list[ProviderInfo]:
    """Every registered provider in registration order (also used by ``GET /meta``)."""
    runtime = runtime or load_runtime_settings(db)
    enabled = set(registry.default_provider_keys(runtime.default_providers))
    return [provider_info(db, cls, runtime, enabled) for cls in registry.list_provider_classes()]


# --------------------------------------------------------------------------- list


@router.get("/providers", response_model=list[ProviderInfo])
def list_providers(db: Session = Depends(get_db)) -> list[ProviderInfo]:
    """All flight providers with readiness, default-set membership and login session state."""
    return build_provider_infos(db)


# --------------------------------------------------------------------------- test search


def default_test_query() -> FlightQuery:
    """ICN → NRT, departing 30 days from today (local), 4-night round trip, 1 adult, economy."""
    depart = now_local().date() + timedelta(days=TEST_DAYS_AHEAD)
    return FlightQuery(
        origin=TEST_ORIGIN,
        destination=TEST_DESTINATION,
        depart_date=depart,
        return_date=depart + timedelta(days=TEST_NIGHTS),
        adults=1,
        cabin="economy",
        currency=get_settings().default_currency,
    )


def _describe(query: FlightQuery) -> str:
    dates = query.depart_date.isoformat()
    if query.return_date:
        dates += f" – {query.return_date.isoformat()}"
    return f"{query.origin} → {query.destination}, {dates}, {query.adults} adult"


def _is_stale(test: ProviderTestOut) -> bool:
    return (
        test.status == "running"
        and test.started_at is not None
        and utcnow() - test.started_at > TEST_STALE_AFTER
    )


def _expire_if_stale(key: str) -> ProviderTestOut | None:
    """The stored test of ``key``; a stale ``running`` one is turned into a timeout error."""
    with _lock:
        test = _tests.get(key)
        if test is not None and _is_stale(test):
            minutes = int(TEST_STALE_AFTER.total_seconds() // 60)
            test = test.model_copy(
                update={
                    "status": "error",
                    "finished_at": utcnow(),
                    "message": f"Test search did not finish within {minutes} minutes",
                }
            )
            _tests[key] = test
        return test


def _finished(key: str, started_at: Any, **fields: Any) -> ProviderTestOut:
    return ProviderTestOut(key=key, started_at=started_at, finished_at=utcnow(), **fields)


def _success(key: str, query: FlightQuery, offers: Sequence[FlightOffer], started_at: Any) -> ProviderTestOut:
    if not offers:
        return _finished(
            key,
            started_at,
            status="ok",
            offers_found=0,
            message=f"The search worked but returned no offers ({_describe(query)})",
        )
    cheapest = min(offers, key=lambda offer: offer.price_total)
    return _finished(
        key,
        started_at,
        status="ok",
        offers_found=len(offers),
        min_price=float(cheapest.price_total),
        currency=cheapest.currency or query.currency,
        message=_describe(query),
    )


def _failure_message(exc: Exception) -> str:
    if isinstance(exc, ProviderNotConfigured):
        return f"Not configured: {safe_error(exc)}"
    if isinstance(exc, ProviderBlocked):
        return f"Blocked by the site: {safe_error(exc)}"
    if isinstance(exc, ProviderError):
        return safe_error(exc)
    return f"{type(exc).__name__}: {safe_error(exc)}"


def _run_test(key: str, provider: FlightProvider, query: FlightQuery, started_at: Any) -> None:
    """Thread body: run the search and store the outcome (never raises)."""
    t0 = time.monotonic()
    try:
        result = _success(key, query, provider.search_flights(query), started_at)
    except Exception as exc:  # noqa: BLE001 - reported to the UI
        if not isinstance(exc, ProviderError):
            logger.exception("Provider test search %s crashed", key)
        result = _finished(key, started_at, status="error", message=_failure_message(exc))
    logger.info(
        "Provider test %s finished: %s in %.1fs (%s)",
        key,
        result.status,
        time.monotonic() - t0,
        result.message,
    )
    with _lock:
        current = _tests.get(key)
        if current is not None and current.started_at == started_at:  # not superseded
            _tests[key] = result


def _store(test: ProviderTestOut) -> ProviderTestOut:
    with _lock:
        _tests[test.key] = test
    return test


@router.post("/providers/{key}/test", response_model=ProviderTestOut, status_code=202)
def start_provider_test(key: str, db: Session = Depends(get_db)) -> ProviderTestOut:
    """Start a small live search in the background; poll ``GET`` while ``status == 'running'``.

    A test already running for ``key`` is returned instead of starting another. An
    unconfigured provider yields an immediate ``error`` result.
    """
    cls = provider_class_or_404(key)
    current = _expire_if_stale(key)
    if current is not None and current.status == "running":
        return current
    query = default_test_query()
    started_at = utcnow()
    try:
        provider = cls(provider_context(cls, load_runtime_settings(db)))
        status = provider.status()
    except Exception as exc:  # noqa: BLE001
        return _store(
            _finished(
                key, started_at, status="error", message=f"could not be initialised: {safe_error(exc)}"
            )
        )
    if not status.configured:
        return _store(
            _finished(
                key,
                started_at,
                status="error",
                message=f"Not configured: {status.message or 'missing configuration'}",
            )
        )
    running = ProviderTestOut(
        key=key, status="running", started_at=started_at, message=_describe(query)
    )
    with _lock:
        current = _tests.get(key)
        if current is not None and current.status == "running" and not _is_stale(current):
            return current  # another request started one meanwhile
        _tests[key] = running
        thread = threading.Thread(
            target=_run_test,
            args=(key, provider, query, started_at),
            name=f"provider-test-{key}",
            daemon=True,
        )
        _test_threads[key] = thread
        thread.start()
    logger.info("Provider test %s started (%s)", key, _describe(query))
    return running


@router.get("/providers/{key}/test", response_model=ProviderTestOut)
def get_provider_test(key: str) -> ProviderTestOut:
    """Latest test state of ``key`` (404 when no test ran since the server started)."""
    provider_class_or_404(key)
    test = _expire_if_stale(key)
    if test is None:
        raise HTTPException(status_code=404, detail=f"no test search has been run for {key!r} yet")
    return test


# --------------------------------------------------------------------------- login sessions


def _run_login(key: str, url: str, cls: type[FlightProvider]) -> None:
    """Thread body: interactive login (blocks until the window closes or times out)."""
    try:
        result = sessions.start_login(
            key,
            url,
            cookie_domains=getattr(cls, "login_cookie_domains", None),
            login_cookies=getattr(cls, "login_cookies", None),
        )
    except Exception as exc:  # noqa: BLE001 - reported via GET /providers
        logger.exception("Interactive login for %s failed", key)
        result = {"status": "error", "cookies": 0, "message": f"{type(exc).__name__}: {safe_error(exc)}"}
    logger.info("Interactive login for %s ended: %s", key, result.get("status"))
    with _lock:
        _login_results[key] = dict(result)


@router.post("/providers/{key}/login", response_model=MessageOut, status_code=202)
def start_provider_login(key: str) -> MessageOut:
    """Open a headed browser window for the user to log in (runs in the background)."""
    cls = provider_class_or_404(key)
    if not cls.supports_login:
        raise HTTPException(
            status_code=400, detail=f"{cls.display_name} does not use a login session"
        )
    url = getattr(cls, "login_url", None)
    if not url:
        raise HTTPException(
            status_code=400, detail=f"{cls.display_name} has no login page configured"
        )
    minutes = int(sessions.DEFAULT_LOGIN_TIMEOUT_S // 60)
    with _lock:
        if _login_alive(key):
            return MessageOut(
                ok=True,
                message=(
                    f"A login window for {cls.display_name} is already open. "
                    "Log in there, then close the window."
                ),
            )
        _login_results.pop(key, None)
        thread = threading.Thread(
            target=_run_login, args=(key, url, cls), name=f"provider-login-{key}", daemon=True
        )
        _login_threads[key] = thread
        thread.start()
    return MessageOut(
        ok=True,
        message=(
            f"A browser window is opening for {cls.display_name}. Log in there, then close the "
            f"window; the session is saved automatically (the window closes after {minutes} minutes)."
        ),
    )


@router.delete("/providers/{key}/session", response_model=MessageOut)
def delete_provider_session(key: str, db: Session = Depends(get_db)) -> MessageOut:
    """Forget the saved login (encrypted state file + dedicated profile)."""
    cls = provider_class_or_404(key)
    with _lock:
        if _login_alive(key):
            raise HTTPException(
                status_code=409,
                detail=f"A login window for {cls.display_name} is still open; close it first",
            )
        _login_results.pop(key, None)
    removed = sessions.delete_session(db, key)
    if removed:
        return MessageOut(ok=True, message=f"Saved {cls.display_name} login session removed.")
    return MessageOut(
        ok=False,
        message=(
            "Session forgotten, but some browser profile files could not be deleted "
            "(close any open browser window and try again)."
        ),
    )
