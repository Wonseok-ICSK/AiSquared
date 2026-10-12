"""Naver Flights provider — **opt-in** (DESIGN §13.1, unpublished research notes).

Naver's terms of service forbid automated collection (macros, bots, scrapers) without prior
permission, so this provider is off by default (``enabled_by_default = False``), describes the
risk in the UI and keeps the volume tiny: at most :attr:`NaverFlightsProvider.max_queries_per_run`
searches per check, 15–30 s apart (process-wide), each search URL asked at most once per Naver
15-minute cache window, and a 24-hour pause after Naver refuses a search (HTTP 403/429/503, a
non-SSE reply, or a request the browser rejects before any readable reply). No IP rotation,
captcha solving, stealth flags or user-agent spoofing — ever.

How a search runs (one :class:`~app.browser.manager.BrowserManager` job):

1. Launch real Edge **headed** (``headless=False``): ``flight-api.naver.com`` rejects the
   ``HeadlessChrome`` user agent and the page then retries every second forever. The window is
   placed off-screen with ``OFFSCREEN_ARGS`` unless ``provider_options["naver"]["show_window"]``.
2. Expose the binding ``__aisqNaverSse`` (messages accepted from the top frame only) and add
   ``naver_capture.js`` as an init script; open :func:`build_search_url`. The page itself fires
   ``POST flight-api.naver.com/flight/international/searchFlights`` (cross-origin, so with a
   CORS preflight; an SSE stream of cumulative snapshots); the script forwards only the final,
   unexpired snapshot (``status.isCompleted == true``) or an error signal.
3. Wait at most :data:`FINAL_EVENT_TIMEOUT_S`. A refusal (HTTP 403/429/503, 2xx without
   ``text/event-stream``, or a fetch the browser fails before any readable reply — an nginx
   refusal page carries no CORS headers, so the page only sees "Failed to fetch") takes the
   context offline at once (stops the page's endless 1-s retry loop), saves a screenshot and
   raises :class:`ProviderBlocked`. The real status of such CORS-hidden replies is read from a
   CDP ``Network`` session (``responseReceivedExtraInfo``) when available.
4. Optionally load partner (travel agency) names with the public ``bookingPartners`` GraphQL
   query from inside the page (cached in memory for a week; unknown codes keep the code).

Parsing is pure and lives in :mod:`app.providers.flights.naver_parse`; the captured event is
cached per search URL until ``status.expireAt`` and parsed per call (``max_stops``,
``korean_ota_only`` may differ between callers).

Options (``RuntimeSettings.provider_options["naver"]``): ``show_window`` (bool, default False)
— show the Edge window instead of placing it off-screen (debugging); ``korean_ota_only``
(bool, default False) — price itineraries only with fares from Naver's Korean OTA list
(``bookingPartners``), ignoring foreign/unlisted sellers such as BDC219 or SKP206.

**Never pass Naver data to the LLM**: flight.naver.com/robots.txt bans AI training and
retrieval-augmented generation. Offers carry ``raw["no_llm"] = True``; the itinerary planner
may use the chosen flight's times only.
"""

from __future__ import annotations

import json
import logging
import random
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

from playwright.sync_api import Error as PlaywrightError

from app.browser.manager import (
    OFFSCREEN_ARGS,
    BrowserError,
    BrowserHandle,
    BrowserUnavailableError,
)
from app.core.airports import is_metro
from app.providers.flights.base import (
    FlightOffer,
    FlightProvider,
    FlightQuery,
    ProviderBlocked,
    ProviderError,
    ProviderNotConfigured,
    ProviderStatus,
)
from app.providers.flights.naver_parse import (
    KOREAN_OTA_PARTNERS,
    parse_final_event,
    partner_names_from_graphql,
)
from app.providers.flights.registry import register
from app.timeutil import utc_to_local

if TYPE_CHECKING:
    from playwright.sync_api import Dialog, Page, Request, Response

logger = logging.getLogger(__name__)

SITE_URL = "https://flight.naver.com"
SEARCH_PATH = "/flights/international/"
SEARCH_API_MARKER = "/flight/international/searchFlights"
GRAPHQL_URL = "https://flight-api.naver.com/graphql"
FARE_TYPE_PARAM: dict[str, str] = {
    "economy": "Y",
    "premium_economy": "P",
    "business": "C",
    "first": "F",
}
BINDING_NAME = "__aisqNaverSse"
"""Must match ``BINDING`` in ``naver_capture.js``."""
CAPTURE_SCRIPT_PATH = Path(__file__).with_name("naver_capture.js")

FINAL_EVENT_TIMEOUT_S = 60.0
"""Hard limit from navigation start to the final snapshot (cold searches take 10–17 s)."""
NAVIGATION_TIMEOUT_MS = 45_000
POLL_INTERVAL_MS = 250
NETWORK_ERROR_GRACE_S = 3.0
"""After a local/transport failure (offline, DNS, broken stream), wait this long for the
page's own retry (1 s later) to open a healthy stream before giving up."""
FETCH_DETAIL_GRACE_S = 0.8
"""After the page saw "Failed to fetch", wait this long (less than the page's 1-s retry) for
the browser's reason; without one the failure counts as a refusal (see :class:`CaptureState`)."""
INCOMPLETE_GRACE_S = 5.0
"""After a stream ended without a final snapshot, wait this long for a re-search to start."""
JOB_TIMEOUT_S = 150.0
QUEUE_TIMEOUT_S = 600.0
"""The single browser worker may be busy with another provider's job."""
SPACING_RANGE_S = (15.0, 30.0)
"""Jittered pause between two Naver searches (process-wide)."""
RESULT_CACHE_TTL_S = 15 * 60.0
"""Naver caches a search for 15 minutes (``status.expireAt``); re-asking inside it is pointless."""
RESULT_CACHE_MAX = 8
"""Captured events kept (a 200-mapping event is ~8 MB decoded)."""
PARTNER_CACHE_TTL_S = 7 * 24 * 3600.0
PARTNER_FETCH_TIMEOUT_MS = 10_000
BLOCK_PAUSE = timedelta(hours=24)
BLOCK_STATUSES = frozenset({403, 429, 503})
LOCAL_NETWORK_ERRORS = (
    "ERR_INTERNET_DISCONNECTED",
    "ERR_NETWORK_CHANGED",
    "ERR_NETWORK_IO_SUSPENDED",
    "ERR_NAME_NOT_RESOLVED",
    "ERR_NAME_RESOLUTION_FAILED",
    "ERR_ADDRESS_UNREACHABLE",
    "ERR_CONNECTION_TIMED_OUT",
    "ERR_TIMED_OUT",
    "ERR_PROXY_",
    "ERR_TUNNEL_CONNECTION_FAILED",
    "ERR_CERT_",
    "ERR_SSL_",
)
"""Chromium errors (substrings) that point at this machine's connection, not at Naver."""
CDP_PENDING_MAX = 512
"""Statuses kept for requests whose URL CDP has not reported yet (events may arrive early)."""

TOS_WARNING = (
    "Naver's terms of service forbid automated collection (macros, bots, scrapers) without "
    "permission. Personal, low-volume use only — use at your own risk."
)

BOOKING_PARTNERS_QUERY = (
    "query bookingPartners {\n  bookingPartners {\n    otaId\n    expName\n  }\n}\n"
)
"""The exact GraphQL document the site itself sends (public, no auth)."""

_PARTNERS_JS = """
async ([url, query, timeoutMs]) => {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(url, {
      method: "POST",
      credentials: "include",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        operationName: "bookingPartners",
        variables: {},
        query: query,
      }),
      signal: controller.signal,
    });
    if (!response.ok) return {error: "HTTP " + response.status};
    const body = await response.json();
    return {partners: (body && body.data && body.data.bookingPartners) || []};
  } catch (error) {
    return {error: String((error && error.message) || error).slice(0, 200)};
  } finally {
    clearTimeout(timer);
  }
}
"""

_SEARCH_KEY_LEG_RE = re.compile(r"(\d{8})([A-Z]{3})[A-Z]([A-Z]{3})[A-Z]")
"""One leg of ``status.searchKey`` (e.g. ``20261120SELCTYOC``: date, origin, type, dest, type)."""


# --------------------------------------------------------------------------- search URL


def route_token(code: str) -> str:
    """``"SEL"`` -> ``"SEL:city"`` (multi-airport metro), ``"ICN"`` -> ``"ICN:airport"``."""
    normalized = code.strip().upper()
    return f"{normalized}:{'city' if is_metro(normalized) else 'airport'}"


def build_search_url(query: FlightQuery) -> str:
    """Naver search page for ``query`` (also the user-facing booking link).

    Round trip: ``/flights/international/{O}-{D}-{YYYYMMDD}/{D}-{O}-{YYYYMMDD}``, one-way:
    ``/flights/international/{O}-{D}-{YYYYMMDD}``; each place is ``CODE:city`` or
    ``CODE:airport`` (the form the site's own route builder emits), then
    ``?adult=N&isDirect=true|false&fareType=Y|P|C|F``.
    """
    origin, destination = route_token(query.origin), route_token(query.destination)
    path = f"{origin}-{destination}-{query.depart_date:%Y%m%d}"
    if query.return_date is not None:
        path += f"/{destination}-{origin}-{query.return_date:%Y%m%d}"
    params = {
        "adult": max(1, query.adults),
        "isDirect": "true" if query.direct_only else "false",
        "fareType": FARE_TYPE_PARAM[query.cabin],
    }
    return f"{SITE_URL}{SEARCH_PATH}{path}?{urlencode(params)}"


# --------------------------------------------------------------------------- capture signals


@dataclass(frozen=True)
class CaptureFailure:
    """Why a capture failed; ``blocked`` failures pause the provider (see :data:`BLOCK_PAUSE`)."""

    blocked: bool
    message: str


def classify_http(status: int, content_type: str) -> CaptureFailure | None:
    """``None`` for a healthy ``searchFlights`` reply (2xx ``text/event-stream``), else why not.

    HTTP 403/429/503 and 2xx replies that are not an event stream count as a refusal
    (``blocked``); other HTTP errors are ordinary failures.
    """
    is_stream = "text/event-stream" in (content_type or "").lower()
    if 200 <= status < 300 and is_stream:
        return None
    if status in BLOCK_STATUSES:
        return CaptureFailure(True, f"Naver refused the search (HTTP {status})")
    if 200 <= status < 300:
        kind = (content_type or "no content type").split(";")[0].strip()
        message = f"Naver answered the search with {kind} instead of an event stream"
        return CaptureFailure(True, f"{message} (HTTP {status})")
    return CaptureFailure(False, f"Naver search request failed (HTTP {status})")


def is_local_network_error(failure: str) -> bool:
    """True when a Chromium network error points at this machine's connection (see
    :data:`LOCAL_NETWORK_ERRORS`), not at a server that refused the request."""
    return any(marker in failure for marker in LOCAL_NETWORK_ERRORS)


class CaptureState:
    """Collects signals from ``naver_capture.js``, Playwright and CDP network events.

    :meth:`outcome` returns the final snapshot text, a :class:`CaptureFailure`, or ``None``
    (keep waiting). Precedence:

    1. the final snapshot;
    2. an HTTP failure with a known status (page script, Playwright or CDP);
    3. a refusal: the ``searchFlights`` request failed before any reply could be read and
       not because of this machine's connection — what a CORS-less 403/429/503 looks like
       to the page (``net::ERR_FAILED``);
    4. a local/transport failure or broken stream, after :data:`NETWORK_ERROR_GRACE_S`;
    5. a bare "Failed to fetch" from the page without the browser's reason, after
       :data:`FETCH_DETAIL_GRACE_S` — a refusal unless a healthy stream opened before;
    6. a stream that ended without a final snapshot, after :data:`INCOMPLETE_GRACE_S`.

    A healthy stream (2xx ``text/event-stream``) clears the pending failures of 4–6: the
    page searched again and :data:`FINAL_EVENT_TIMEOUT_S` now governs the new stream.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self.final_text: str | None = None
        self.http_failure: CaptureFailure | None = None
        self.refusal: CaptureFailure | None = None
        self.network_error: str | None = None
        self.network_error_at: float | None = None
        self.fetch_failed_at: float | None = None
        self.incomplete: str | None = None
        self.incomplete_at: float | None = None
        self.streams_ok = 0

    def on_message(self, message: Any) -> None:
        """Binding callback payload from ``naver_capture.js`` (top frame only)."""
        if not isinstance(message, Mapping):
            return
        kind, reason = message.get("kind"), message.get("reason")
        if kind == "final" and isinstance(message.get("data"), str):
            self.final_text = self.final_text or message["data"]
        elif kind == "error" and reason == "http":
            self.on_http(_int(message.get("status")), str(message.get("contentType") or ""))
        elif kind == "error" and reason == "network":
            if self.fetch_failed_at is None:
                self.fetch_failed_at = self._clock()
        elif kind == "error" and reason == "stream":
            self._pending_error(f"stream error: {message.get('message') or 'unknown'}")
        elif kind == "error" and reason == "incomplete":
            self._incomplete(message)

    def on_http(self, status: int, content_type: str) -> None:
        """A ``searchFlights`` response status (from the page script, Playwright or CDP)."""
        failure = classify_http(status, content_type)
        if failure is None:
            self.streams_ok += 1
            self._clear_pending()
        elif self.http_failure is None or (failure.blocked and not self.http_failure.blocked):
            self.http_failure = failure

    def on_request_failed(self, failure: str, *, had_response: bool) -> None:
        """Playwright ``requestfailed`` for a ``searchFlights`` request (``failure`` text).

        ``had_response``: a reply (headers) had arrived, so a stream broke mid-way.
        """
        if "ERR_ABORTED" in failure:  # the page cancelled its own request
            return
        if had_response:
            self._pending_error(f"stream error: {failure}")
        elif is_local_network_error(failure):
            self._pending_error(f"network error: {failure}")
        elif self.refusal is None:
            self.refusal = CaptureFailure(
                True,
                "Naver refused the search: the request failed before any readable reply "
                f"({failure}; a refusal without CORS headers, e.g. HTTP 403/429/503)",
            )

    def outcome(self) -> str | CaptureFailure | None:
        """Final snapshot text, a failure, or ``None`` while still waiting."""
        if self.final_text is not None:
            return self.final_text
        if self.http_failure is not None:
            return self.http_failure
        if self.refusal is not None:
            return self.refusal
        now = self._clock()
        if self.network_error is not None:
            if _elapsed(self.network_error_at, now) < NETWORK_ERROR_GRACE_S:
                return None
            return CaptureFailure(False, f"Naver search request failed ({self.network_error})")
        if _elapsed(self.fetch_failed_at, now) >= FETCH_DETAIL_GRACE_S:
            if self.streams_ok == 0:
                return CaptureFailure(
                    True, "Naver refused the search: the request failed before any reply"
                )
            return CaptureFailure(False, "Naver search request failed (Failed to fetch)")
        if _elapsed(self.incomplete_at, now) >= INCOMPLETE_GRACE_S:
            return CaptureFailure(False, f"Naver: {self.incomplete}")
        return None

    def _pending_error(self, text: str) -> None:
        if self.network_error is None:
            self.network_error, self.network_error_at = text, self._clock()

    def _incomplete(self, message: Mapping[str, Any]) -> None:
        if message.get("expired"):
            self.incomplete = "the search result had expired and no fresh one followed"
        else:
            done, total = message.get("completed"), message.get("requested")
            progress = f" ({done}/{total} partners)" if done is not None else ""
            self.incomplete = f"the search stream ended before completion{progress}"
        self.incomplete_at = self._clock()

    def _clear_pending(self) -> None:
        self.network_error = self.network_error_at = None
        self.fetch_failed_at = None
        self.incomplete = self.incomplete_at = None


class SearchStatusWatcher:
    """Feeds ``searchFlights`` statuses seen by a CDP ``Network`` session into a state.

    CDP reports a reply's status (``responseReceivedExtraInfo``) even when CORS hides it from
    the page and from Playwright, including a refused preflight (``OPTIONS``). Only failures
    are forwarded (Playwright already reports healthy streams); redirects are ignored.
    """

    def __init__(self, state: CaptureState) -> None:
        self._state = state
        self._methods: dict[str, str] = {}
        self._early: OrderedDict[str, tuple[int, str]] = OrderedDict()

    def on_request(self, params: Mapping[str, Any]) -> None:
        """``Network.requestWillBeSent``: remember ``searchFlights`` request ids."""
        request = params.get("request")
        if not isinstance(request, Mapping) or SEARCH_API_MARKER not in str(request.get("url")):
            return
        request_id = str(params.get("requestId") or "")
        method = str(request.get("method") or "").upper()
        self._methods[request_id] = method
        early = self._early.pop(request_id, None)
        if early is not None:
            self._report(method, *early)

    def on_response(self, params: Mapping[str, Any]) -> None:
        """``Network.responseReceivedExtraInfo``: status + raw headers of any request."""
        request_id = str(params.get("requestId") or "")
        status = _int(params.get("statusCode"))
        content_type = _header(params.get("headers"), "content-type")
        method = self._methods.get(request_id)
        if method is not None:
            self._report(method, status, content_type)
            return
        self._early[request_id] = (status, content_type)  # its URL may still be on the way
        while len(self._early) > CDP_PENDING_MAX:
            self._early.popitem(last=False)

    def _report(self, method: str, status: int, content_type: str) -> None:
        if 300 <= status < 400 or (method == "OPTIONS" and status < 400):
            return
        if method in ("POST", "OPTIONS") and classify_http(status, content_type) is not None:
            self._state.on_http(status, content_type)


def _header(headers: Any, name: str) -> str:
    """Case-insensitive header lookup in a CDP ``Headers`` object."""
    if not isinstance(headers, Mapping):
        return ""
    return next((str(v) for k, v in headers.items() if str(k).lower() == name), "")


def _elapsed(since: float | None, now: float) -> float:
    """Seconds since ``since`` (``-inf`` when it never happened)."""
    return float("-inf") if since is None else now - since


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _first_line(exc: BaseException) -> str:
    text = str(exc).strip()
    return text.splitlines()[0][:300] if text else type(exc).__name__


# --------------------------------------------------------------------------- browser job


@dataclass
class CaptureResult:
    """What one browser job returns."""

    event: dict[str, Any]
    partners: dict[str, str] | None
    elapsed_s: float


@cache
def capture_script() -> str:
    """Source of ``naver_capture.js`` (read once)."""
    return CAPTURE_SCRIPT_PATH.read_text(encoding="utf-8")


def _dismiss_dialog(dialog: Dialog) -> None:
    """The page alerts e.g. when a cached result expired; log and dismiss (never block)."""
    logger.info("Naver page opened a %s dialog (dismissed): %s", dialog.type, dialog.message[:200])
    try:
        dialog.dismiss()
    except PlaywrightError as exc:
        logger.debug("Dismissing the Naver dialog failed: %s", _first_line(exc))


class PageWatcher:
    """Routes the page's binding calls and network events into a :class:`CaptureState`.

    Only the top frame counts: ``expose_binding`` makes the binding callable from every frame
    (ad iframes included), and an iframe could also fetch a URL containing the API path.
    """

    def __init__(self, page: Page, state: CaptureState) -> None:
        self._page = page
        self._state = state
        self._answered: set[Request] = set()
        self.ignored_calls = 0

    def on_binding(self, source: Mapping[str, Any], message: Any) -> None:
        """``expose_binding`` callback: forward top-frame messages, drop the rest."""
        if source.get("frame") is not self._page.main_frame:
            self.ignored_calls += 1
            if self.ignored_calls == 1:
                logger.warning("Naver capture: ignored a binding call from a sub-frame")
            return
        self._state.on_message(message)

    def on_response(self, response: Response) -> None:
        """Playwright ``response`` (only fires for replies CORS lets the page read)."""
        try:
            if self._is_search(response.request):
                self._answered.add(response.request)
                self._state.on_http(response.status, response.headers.get("content-type", ""))
        except Exception as exc:  # noqa: BLE001 - an event handler must never raise
            logger.debug("Naver response watcher failed: %s", _first_line(exc))

    def on_request_failed(self, request: Request) -> None:
        """Playwright ``requestfailed``: CORS-blocked refusals end up here as ERR_FAILED."""
        try:
            if self._is_search(request):
                self._state.on_request_failed(
                    request.failure or "request failed", had_response=request in self._answered
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug("Naver request watcher failed: %s", _first_line(exc))

    def _is_search(self, request: Request) -> bool:
        if SEARCH_API_MARKER not in request.url or request.method != "POST":
            return False
        try:
            return request.frame is self._page.main_frame
        except PlaywrightError:  # e.g. a service-worker request has no frame
            return False


def attach_status_watcher(page: Page, state: CaptureState) -> bool:
    """Open a CDP ``Network`` session on ``page`` feeding a :class:`SearchStatusWatcher`.

    Returns ``False`` (refusals are then recognised from fetch errors alone) if CDP is not
    available. The session buffers no bodies (it only needs statuses and headers).
    """
    watcher = SearchStatusWatcher(state)
    try:
        session = page.context.new_cdp_session(page)
        session.on("Network.requestWillBeSent", lambda p: _guarded(watcher.on_request, p))
        session.on(
            "Network.responseReceivedExtraInfo", lambda p: _guarded(watcher.on_response, p)
        )
        session.send(
            "Network.enable",
            {"maxTotalBufferSize": 0, "maxResourceBufferSize": 0, "maxPostDataSize": 0},
        )
    except PlaywrightError as exc:
        logger.info("Naver: no CDP status watcher (%s); using fetch errors", _first_line(exc))
        return False
    return True


def _guarded(handler: Callable[[Mapping[str, Any]], None], params: Any) -> None:
    """Call a CDP event handler; never let it raise into Playwright's dispatcher."""
    try:
        if isinstance(params, Mapping):
            handler(params)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Naver CDP watcher failed: %s", _first_line(exc))


def _stop_page(handle: BrowserHandle, page: Page, name: str) -> None:
    """Stop the page's 1-s retry loop at once, keeping a screenshot.

    Going offline stops ordinary requests immediately; CORS preflights bypass the offline
    emulation, so the page is closed right after the screenshot as well.
    """
    try:
        page.context.set_offline(True)
    except PlaywrightError as exc:
        logger.debug("Could not take the Naver context offline: %s", _first_line(exc))
    handle.screenshot(page, name)
    try:
        page.close()
    except PlaywrightError as exc:
        logger.debug("Could not close the Naver page: %s", _first_line(exc))


def prepare_page(page: Page, state: CaptureState) -> None:
    """Install the binding, the capture script and the event watchers on a fresh page."""
    watcher = PageWatcher(page, state)
    page.expose_binding(BINDING_NAME, watcher.on_binding)
    page.add_init_script(script=capture_script())
    page.on("dialog", _dismiss_dialog)
    page.on("response", watcher.on_response)
    page.on("requestfailed", watcher.on_request_failed)
    attach_status_watcher(page, state)


def await_final_event(
    handle: BrowserHandle,
    page: Page,
    url: str,
    state: CaptureState,
    timeout_s: float = FINAL_EVENT_TIMEOUT_S,
) -> str:
    """Open ``url`` and wait for the final snapshot text (raises on refusal/failure/timeout)."""
    deadline = time.monotonic() + timeout_s
    try:
        response = page.goto(url, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)
    except PlaywrightError as exc:
        if state.outcome() is None:
            raise ProviderError(f"Naver Flights page did not load: {_first_line(exc)}") from exc
        response = None
    if response is not None and response.status in BLOCK_STATUSES:
        state.on_http(response.status, "text/html")
    while True:
        handle.check_cancelled()
        outcome = state.outcome()
        if isinstance(outcome, str):
            return outcome
        if isinstance(outcome, CaptureFailure):
            _stop_page(handle, page, "naver-blocked" if outcome.blocked else "naver-error")
            raise (ProviderBlocked if outcome.blocked else ProviderError)(outcome.message)
        if time.monotonic() >= deadline:
            _stop_page(handle, page, "naver-timeout")
            raise ProviderError(
                f"Naver Flights returned no complete result within {timeout_s:.0f} s "
                f"({state.streams_ok} search stream(s) opened)"
            )
        page.wait_for_timeout(POLL_INTERVAL_MS)


def decode_event(text: str) -> dict[str, Any]:
    """Final snapshot text -> dict (``ProviderError`` if it is not a JSON object)."""
    try:
        event = json.loads(text)
    except ValueError as exc:
        raise ProviderError(f"Naver search result is not valid JSON: {exc}") from exc
    if not isinstance(event, dict):
        raise ProviderError("Naver search result is not a JSON object")
    return event


def search_key_legs(search_key: Any) -> list[tuple[str, str, str]]:
    """``status.searchKey`` -> ``[(YYYYMMDD, origin, destination), ...]`` per leg.

    ``"RTY100PN20261120SELCTYOC20261127TYOCSELC"`` -> ``[("20261120", "SEL", "TYO"),
    ("20261127", "TYO", "SEL")]``; ``[]`` when the key is missing or has another shape.
    """
    if not isinstance(search_key, str):
        return []
    return [(m[1], m[2], m[3]) for m in _SEARCH_KEY_LEG_RE.finditer(search_key)]


def expected_legs(query: FlightQuery) -> list[tuple[str, str, str]]:
    """The ``(date, origin, destination)`` legs a search for ``query`` must report."""
    origin, destination = query.origin.strip().upper(), query.destination.strip().upper()
    legs = [(f"{query.depart_date:%Y%m%d}", origin, destination)]
    if query.return_date is not None:
        legs.append((f"{query.return_date:%Y%m%d}", destination, origin))
    return legs


def validate_event(event: Mapping[str, Any], query: FlightQuery) -> None:
    """Reject an expired event or one for another route/dates (``ProviderError``).

    An unrecognisable ``status.searchKey`` is accepted (Naver may change its format).
    """
    if event.get("isExpired") is True:
        raise ProviderError("Naver returned an expired search result")
    status = event.get("status")
    legs = search_key_legs(status.get("searchKey") if isinstance(status, Mapping) else None)
    if legs and legs != expected_legs(query):
        raise ProviderError(
            f"Naver returned a result for another search ({legs}, expected "
            f"{expected_legs(query)})"
        )
    if not legs:
        logger.debug("Naver: searchKey not recognised; route check skipped")


def fetch_partner_names(page: Page) -> dict[str, str] | None:
    """Partner code -> name via the public ``bookingPartners`` query (``None`` on failure)."""
    try:
        result = page.evaluate(
            _PARTNERS_JS, [GRAPHQL_URL, BOOKING_PARTNERS_QUERY, PARTNER_FETCH_TIMEOUT_MS]
        )
    except PlaywrightError as exc:
        logger.info("Naver partner names unavailable: %s", _first_line(exc))
        return None
    if not isinstance(result, Mapping) or result.get("error"):
        reason = result.get("error") if isinstance(result, Mapping) else "unexpected reply"
        logger.info("Naver partner names unavailable: %s", reason)
        return None
    return partner_names_from_graphql(result.get("partners")) or None


def capture_search(
    handle: BrowserHandle, url: str, *, show_window: bool = False, want_partners: bool = True
) -> CaptureResult:
    """Browser job: headed Edge (off-screen unless ``show_window``), capture the final event."""
    started = time.monotonic()
    browser = handle.launch(headless=False, args=[] if show_window else list(OFFSCREEN_ARGS))
    context = handle.new_context(browser)
    page = context.new_page()
    state = CaptureState()
    prepare_page(page, state)
    event = decode_event(await_final_event(handle, page, url, state))
    partners = fetch_partner_names(page) if want_partners else None
    return CaptureResult(event=event, partners=partners, elapsed_s=time.monotonic() - started)


# --------------------------------------------------------------------------- process-wide state


@dataclass(frozen=True)
class BlockPause:
    """Naver refused a search; the provider rests until ``until`` (aware UTC)."""

    until: datetime
    reason: str

    def describe(self) -> str:
        """Human-readable status line (local time)."""
        resume = utc_to_local(self.until).strftime("%Y-%m-%d %H:%M")
        return f"Naver Flights paused until {resume} (or an app restart): {self.reason}"


_state_lock = threading.Lock()
_block_pause: BlockPause | None = None
_partners: tuple[float, dict[str, str]] | None = None
_results: OrderedDict[str, tuple[float, CaptureResult]] = OrderedDict()
"""Search URL -> (monotonic expiry, captured result). Events are only read (parsing is pure)."""

_query_lock = threading.Lock()
_next_query_at = 0.0
_sleep: Callable[[float], None] = time.sleep
_monotonic: Callable[[], float] = time.monotonic


def active_block_pause(now: datetime | None = None) -> BlockPause | None:
    """The current pause after a refusal, if any (an expired one is dropped)."""
    global _block_pause
    now = now or datetime.now(UTC)
    with _state_lock:
        if _block_pause is not None and _block_pause.until <= now:
            _block_pause = None
        return _block_pause


def _set_block_pause(reason: str) -> BlockPause:
    global _block_pause
    pause = BlockPause(datetime.now(UTC) + BLOCK_PAUSE, reason)
    with _state_lock:
        _block_pause = pause
    logger.warning("Naver Flights refused a search; pausing the provider: %s", pause.describe())
    return pause


def cached_partner_names() -> dict[str, str] | None:
    """The live ``bookingPartners`` map if loaded within :data:`PARTNER_CACHE_TTL_S`."""
    with _state_lock:
        if _partners is None or _monotonic() - _partners[0] > PARTNER_CACHE_TTL_S:
            return None
        return dict(_partners[1])


def _store_partner_names(names: Mapping[str, str]) -> None:
    global _partners
    with _state_lock:
        _partners = (_monotonic(), dict(names))


def cached_result(url: str) -> CaptureResult | None:
    """The event captured for ``url`` while Naver still serves it from its cache, if any."""
    with _state_lock:
        entry = _results.get(url)
        if entry is None or entry[0] <= _monotonic():
            _results.pop(url, None)
            return None
        return entry[1]


def _store_result(url: str, result: CaptureResult, ttl_s: float) -> None:
    if ttl_s <= 0:
        return
    with _state_lock:
        _results[url] = (_monotonic() + ttl_s, result)
        _results.move_to_end(url)
        while len(_results) > RESULT_CACHE_MAX:
            _results.popitem(last=False)


def result_ttl_s(event: Mapping[str, Any], now: datetime | None = None) -> float:
    """Seconds the result stays valid: until ``status.expireAt``, at most 15 minutes."""
    now = now or datetime.now(UTC)
    status = event.get("status")
    raw = status.get("expireAt") if isinstance(status, Mapping) else None
    try:
        expires = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return RESULT_CACHE_TTL_S
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    return max(0.0, min(RESULT_CACHE_TTL_S, (expires - now).total_seconds()))


def reset_state() -> None:
    """Forget pauses, cached results/partners and the spacing clock (tests; restart does too)."""
    global _block_pause, _partners, _next_query_at
    with _state_lock:
        _block_pause = None
        _partners = None
        _results.clear()
    _next_query_at = 0.0


def _wait_for_turn() -> None:
    """Sleep until the jittered gap after the previous Naver search has passed."""
    wait = _next_query_at - _monotonic()
    if wait > 0:
        logger.info("Naver Flights: waiting %.0f s before the next search", wait)
        _sleep(wait)


def _schedule_next() -> None:
    global _next_query_at
    _next_query_at = _monotonic() + random.uniform(*SPACING_RANGE_S)


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


# --------------------------------------------------------------------------- provider


@register
class NaverFlightsProvider(FlightProvider):
    """Naver Flights (KRW fares from Korean travel agencies and airline-direct channels)."""

    key = "naver"
    display_name = "Naver Flights (opt-in)"
    description = (
        "Naver Flights search results in KRW (Korean travel agencies and airline-direct "
        "channels), read from a real Microsoft Edge window placed off-screen. Off by default. "
        f"WARNING: {TOS_WARNING} At most 4 searches per check, 15–30 s apart; a refused "
        "search pauses the source for 24 h. Prices are 'all payment methods' (A01) fares per "
        "adult incl. taxes and fuel surcharge; card-conditional discounts are ignored. "
        "Naver data is flagged never to be sent to the AI planner (Naver bans AI/RAG use)."
    )
    uses_browser = True
    supports_metro_codes = True
    max_queries_per_run = 4
    enabled_by_default = False  # opt-in only (terms of service)

    def status(self) -> ProviderStatus:
        """Usable when browser automation is available and no refusal pause is active."""
        if self.ctx.browser is None:
            return ProviderStatus(
                configured=False,
                message="Browser automation is not available (needs Microsoft Edge + Playwright)",
            )
        pause = active_block_pause()
        if pause is not None:
            return ProviderStatus(configured=False, message=pause.describe())
        return ProviderStatus(configured=True, message=f"Opt-in source. {TOS_WARNING}")

    def search_flights(self, query: FlightQuery) -> list[FlightOffer]:
        """At most 30 cheapest A01 offers; one Naver search per URL per 15-minute window."""
        url = build_search_url(query)
        result = cached_result(url)
        if result is not None:
            logger.info("Naver Flights: reusing the result fetched in the last 15 min for %s", url)
        else:
            result = self._search(url, query)
        korean_only = _truthy(self.ctx.options.get("korean_ota_only", False))
        return self._offers(query, url, result, korean_only=korean_only)

    # ------------------------------------------------------------------ internals

    def _search(self, url: str, query: FlightQuery) -> CaptureResult:
        """One spaced Naver search for ``url``, cached before the lock is released."""
        with _query_lock:
            result = cached_result(url)  # another trip may have searched it meanwhile
            if result is not None:
                logger.info("Naver Flights: reusing the result another check just fetched")
                return result
            self._ensure_usable()
            _wait_for_turn()
            try:
                result = self._capture(url)
            finally:
                _schedule_next()
            if result.partners:
                _store_partner_names(result.partners)
            validate_event(result.event, query)
            _store_result(url, result, result_ttl_s(result.event))
            return result

    def _ensure_usable(self) -> None:
        if self.ctx.browser is None:
            raise ProviderNotConfigured("Browser automation is not available for Naver Flights")
        pause = active_block_pause()
        if pause is not None:
            raise ProviderBlocked(pause.describe())

    def _capture(self, url: str) -> CaptureResult:
        """Run the browser job; refusals start the block pause."""
        browser = self.ctx.browser
        if browser is None:  # checked by _ensure_usable; keeps type checkers happy
            raise ProviderNotConfigured("Browser automation is not available for Naver Flights")
        show_window = _truthy(self.ctx.options.get("show_window", False))
        want_partners = cached_partner_names() is None
        logger.info("Naver Flights: searching %s", url)
        try:
            return browser.run(
                lambda handle: capture_search(
                    handle, url, show_window=show_window, want_partners=want_partners
                ),
                timeout=JOB_TIMEOUT_S,
                label=self.key,
                queue_timeout=QUEUE_TIMEOUT_S,
            )
        except ProviderBlocked as exc:
            _set_block_pause(str(exc))
            raise
        except ProviderError:
            raise
        except BrowserUnavailableError as exc:
            raise ProviderError(
                f"Edge could not be started for Naver Flights (a headed window needs a "
                f"logged-in desktop session): {_first_line(exc)}"
            ) from exc
        except (BrowserError, TimeoutError, PlaywrightError) as exc:
            raise ProviderError(f"Naver Flights browser job failed: {_first_line(exc)}") from exc

    def _offers(
        self, query: FlightQuery, url: str, result: CaptureResult, *, korean_only: bool
    ) -> list[FlightOffer]:
        """Parse the captured event with the freshest partner names (fresh offer objects)."""
        live = cached_partner_names()
        names = {**KOREAN_OTA_PARTNERS, **live} if live else dict(KOREAN_OTA_PARTNERS)
        korean = set(live) if live else set(KOREAN_OTA_PARTNERS)
        offers = parse_final_event(
            result.event,
            query,
            search_url=url,
            partner_names=names,
            korean_partners=korean,
            korean_ota_only=korean_only,
        )
        cheapest = f"{offers[0].price_per_person:,.0f} KRW/person" if offers else "none"
        logger.info(
            "Naver Flights: %d offers (search took %.1f s; cheapest A01 %s)",
            len(offers),
            result.elapsed_s,
            cheapest,
        )
        return offers
