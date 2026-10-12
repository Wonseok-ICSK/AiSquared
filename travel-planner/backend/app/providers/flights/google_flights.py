"""Google Flights provider: key ``google_flights``, default source, no API key (DESIGN §13.1).

Pipeline: :mod:`~app.providers.flights.google_flights_tfs` builds the ``tfs`` search URL,
this module fetches the page politely, :mod:`~app.providers.flights.google_flights_parse`
turns it into offers (ds:1 JSON first, result-row aria-labels as fallback).

Fetch modes (``RuntimeSettings.provider_options["google_flights"]["fetch_mode"]``):

* ``"browser"`` (default): the installed Edge through the shared :class:`BrowserManager`, in a
  throwaway headless context; ``page.goto(url, wait_until="domcontentloaded")`` then
  ``page.content()``. No stealth flags, no user-agent spoofing. When no browser can be launched
  (or the context has no browser manager) the request falls back to ``http``.
* ``"http"``: one httpx GET with ``Accept-Language`` and a normal desktop Edge User-Agent.

Politeness: one Google request at a time per process and at least
:data:`MIN_REQUEST_INTERVAL_S` (+ up to :data:`REQUEST_JITTER_S` random) between consecutive
requests (:class:`RequestSpacer`). Blocks — HTTP 429/403, a ``/sorry/`` page, the
``consent.google.com`` wall, a captcha / JavaScript challenge, or a page with neither a ds:1
script nor result rows — raise :class:`ProviderBlocked` at once (never retried, never solved;
browser mode saves a screenshot under ``data/logs/screenshots``) and pause every Google request
of the process for ``block_cooldown_minutes`` (option, default
:data:`DEFAULT_BLOCK_COOLDOWN_MINUTES`; :class:`BlockCooldown`): meanwhile requests fail with
:class:`ProviderBlocked` without touching the network. A page that has data Google's format no
longer matches (ds:1 undecodable / unknown layout, unreadable rows) is a :class:`ProviderError`,
not a block.

Round trips: Google lists outbound flights priced at the cheapest round-trip total, so offers
have ``inbound=None`` + ``return_date``. :meth:`GoogleFlightsProvider.get_flight_details`
spends one extra request (outbound selected) to learn the return flight and the "Booking
options" link. Google's terms forbid automated access that ignores robots.txt; this provider is
for personal, low-volume monitoring only (it fetches the base ``/travel/flights`` path; the
``/search`` and ``/booking`` URLs are built only as links for the user).
"""

from __future__ import annotations

import logging
import math
import random
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import TYPE_CHECKING

import httpx

from app.providers.flights.base import (
    FlightOffer,
    FlightProvider,
    FlightQuery,
    ProviderBlocked,
    ProviderError,
    ProviderStatus,
    leg_key,
    query_for_offer,
)
from app.providers.flights.google_flights_parse import (
    DS1_MISSING,
    DS1_USABLE,
    ROW_SELECTOR,
    Ds1FormatError,
    ReturnOption,
    count_result_rows,
    detect_block,
    ds1_state,
    extract_ds1,
    has_ds1,
    is_no_results,
    parse_dom_rows,
    parse_offers,
    parse_return_options,
)
from app.providers.flights.google_flights_tfs import (
    SelectedFlight,
    booking_link,
    search_url,
    selected_from_leg,
    user_link,
)
from app.providers.flights.registry import register
from app.timeutil import now_local

if TYPE_CHECKING:
    from app.browser.manager import BrowserHandle

logger = logging.getLogger(__name__)

FETCH_BROWSER = "browser"
FETCH_HTTP = "http"
FETCH_MODES: tuple[str, ...] = (FETCH_BROWSER, FETCH_HTTP)

MIN_REQUEST_INTERVAL_S = 5.0
"""Minimum spacing between two Google requests of this process."""
REQUEST_JITTER_S = 5.0
"""Random extra spacing (uniform 0..this) on top of :data:`MIN_REQUEST_INTERVAL_S`."""
BROWSER_JOB_TIMEOUT_S = 120.0
NAVIGATION_TIMEOUT_MS = 45_000
ROWS_WAIT_MS = 15_000
"""How long the browser waits for result rows when the HTML has no usable ds:1 data."""
DEFAULT_BLOCK_COOLDOWN_MINUTES = 240.0
"""How long all Google requests pause after a block (option ``block_cooldown_minutes``; 0 = off)."""
BROWSER_JOB_LABEL = "google_flights"
SCREENSHOT_NAME = "google_flights-blocked"
UNRECOGNISED_SCREENSHOT_NAME = "google_flights-unrecognised"
"""Screenshot name for a page whose ds:1 data no longer matches the parser (no rows either)."""

HTTP_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/154.0.0.0 Safari/537.36 Edg/154.0.0.0"
)
HTTP_HEADERS: dict[str, str] = {
    "User-Agent": HTTP_USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,ko;q=0.8",
}
PRICE_MATCH_TOLERANCE = 0.5
"""Prices are whole currency units; a return option "matches" the listed total within this."""


# --------------------------------------------------------------------------- politeness


class RequestSpacer:
    """One Google request at a time per process, spaced ``min_interval + U(0, jitter)`` apart.

    The spacing counts from the *end* of the previous request (failed ones included). ``clock``
    / ``sleep`` / ``rng`` are injectable for tests (defaults resolved at call time, so
    monkeypatching ``time.sleep`` works too).
    """

    def __init__(
        self,
        min_interval: float = MIN_REQUEST_INTERVAL_S,
        jitter: float = REQUEST_JITTER_S,
        *,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
        rng: Callable[[], float] | None = None,
    ) -> None:
        self.min_interval = min_interval
        self.jitter = jitter
        self._clock = clock
        self._sleep = sleep
        self._rng = rng
        self._lock = threading.Lock()
        self._next_at: float | None = None

    def _now(self) -> float:
        return (self._clock or time.monotonic)()

    @contextmanager
    def slot(self) -> Iterator[float]:
        """Wait for the next free slot and hold it during one request; yields seconds waited."""
        with self._lock:
            waited = 0.0
            if self._next_at is not None:
                waited = max(0.0, self._next_at - self._now())
                if waited > 0:
                    logger.debug("Google Flights: waiting %.1f s before the next request", waited)
                    (self._sleep or time.sleep)(waited)
            try:
                yield waited
            finally:
                extra = (self._rng or random.random)() * self.jitter
                self._next_at = self._now() + self.min_interval + extra

    def reset(self) -> None:
        """Forget the last request (tests)."""
        with self._lock:
            self._next_at = None


REQUEST_SPACER = RequestSpacer()
"""Process-wide spacer shared by every provider instance (and both fetch modes)."""


class BlockCooldown:
    """Process-wide pause after Google blocked a request: no Google request until it ends.

    Research guardrail: back off on any block instead of retrying (another trip's scheduled
    check or a manual check would otherwise hit the captcha page again within seconds).
    ``clock`` is injectable for tests (default ``time.monotonic``, resolved at call time).
    """

    def __init__(self, *, clock: Callable[[], float] | None = None) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._until: float | None = None
        self._reason = ""

    def _now(self) -> float:
        return (self._clock or time.monotonic)()

    def start(self, seconds: float, reason: str) -> None:
        """Pause for ``seconds`` because of ``reason``; extends (never shortens) a running pause."""
        if seconds <= 0:
            return
        with self._lock:
            until = self._now() + seconds
            if self._until is None or until > self._until:
                self._until = until
                self._reason = reason

    def remaining(self) -> tuple[float, str]:
        """Seconds left in the pause (``0.0`` when none) and the block that started it."""
        with self._lock:
            if self._until is None:
                return 0.0, ""
            left = self._until - self._now()
            if left <= 0:
                self._until, self._reason = None, ""
                return 0.0, ""
            return left, self._reason

    def reset(self) -> None:
        """End the pause now (tests; a future "retry Google now" action)."""
        with self._lock:
            self._until, self._reason = None, ""


BLOCK_COOLDOWN = BlockCooldown()
"""Process-wide block pause shared by every provider instance (and both fetch modes)."""


def local_clock_after(seconds: float) -> str:
    """Local wall-clock time ``seconds`` from now: ``"HH:MM"``, with the date if not today."""
    now = now_local()
    at = now + timedelta(seconds=seconds)
    return f"{at:%H:%M}" if at.date() == now.date() else f"{at:%Y-%m-%d %H:%M}"


# --------------------------------------------------------------------------- fetching


@dataclass(frozen=True)
class FetchedPage:
    """A fetched Google Flights page."""

    html: str
    url: str  # final URL (after redirects)
    status: int | None
    mode: str  # FETCH_BROWSER | FETCH_HTTP
    screenshot: str | None = None


def fetch_page_with_browser(handle: BrowserHandle, url: str) -> FetchedPage:
    """Browser-job body: open ``url`` in a throwaway headless context and return its HTML.

    Runs on the Playwright worker thread. Waits for result rows only when the server HTML has
    no usable ds:1 data (missing, undecodable or unknown layout); saves a screenshot when the
    page looks blocked or has neither usable ds:1 data nor result rows.
    """
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    context = handle.new_context(headless=True)
    page = context.new_page()
    response = page.goto(url, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)
    status = response.status if response is not None else None
    html, final_url = page.content(), page.url
    blocked = detect_block(html, final_url, status)
    state = ds1_state(html)
    if blocked is None and state not in DS1_USABLE:
        try:
            page.locator(ROW_SELECTOR).first.wait_for(state="attached", timeout=ROWS_WAIT_MS)
        except PlaywrightTimeoutError:
            logger.debug("Google Flights: no result rows appeared within %d ms", ROWS_WAIT_MS)
        html, final_url = page.content(), page.url
        blocked = detect_block(html, final_url, status)
        state = ds1_state(html)
    screenshot = None
    if blocked is not None or (state not in DS1_USABLE and count_result_rows(html) == 0):
        # No ds:1 script at all = treated as a block; a ds:1 Google's format no longer matches
        # is a format problem (named differently so the two are easy to tell apart).
        is_block = blocked is not None or state == DS1_MISSING
        name = SCREENSHOT_NAME if is_block else UNRECOGNISED_SCREENSHOT_NAME
        path = handle.screenshot(page, name)
        screenshot = str(path) if path is not None else None
    return FetchedPage(html, final_url, status, FETCH_BROWSER, screenshot)


def fetch_page_with_http(url: str, timeout: float) -> FetchedPage:
    """One plain GET (redirects followed, so ``/sorry/`` and consent redirects are visible)."""
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True, headers=HTTP_HEADERS) as client:
            response = client.get(url)
    except httpx.TimeoutException:
        raise ProviderError(f"Google Flights request timed out after {timeout:g} s") from None
    except httpx.HTTPError as exc:
        raise ProviderError(f"Google Flights request failed: {_first_line(exc)}") from None
    return FetchedPage(response.text, str(response.url), response.status_code, FETCH_HTTP)


def _finite_number(value: object) -> float | None:
    """``value`` as a finite float (numbers and numeric strings; not bools), else ``None``."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def _first_line(exc: BaseException) -> str:
    text = str(exc).strip()
    return text.splitlines()[0] if text else type(exc).__name__


# --------------------------------------------------------------------------- offer helpers


def outbound_selection(offer: FlightOffer) -> tuple[SelectedFlight, ...] | None:
    """The offer's outbound flights as ``tfs`` selections (segments, else ``raw["selected"]``)."""
    selected = selected_from_leg(offer.outbound)
    if selected:
        return selected
    stored = offer.raw.get("selected")
    if not isinstance(stored, list) or not stored:
        return None
    try:
        return tuple(SelectedFlight.from_list(item) for item in stored)
    except (TypeError, ValueError):
        return None


def search_query_for_offer(offer: FlightOffer) -> FlightQuery:
    """The search that produced ``offer``: its dates/passengers/cabin plus the original
    origin/destination (e.g. metro codes) and stop filter from ``raw["search"]``."""
    base = query_for_offer(offer)
    search = offer.raw.get("search")
    if not isinstance(search, dict):
        return base
    max_stops = search.get("max_stops")
    return replace(
        base,
        origin=str(search.get("origin") or base.origin),
        destination=str(search.get("destination") or base.destination),
        direct_only=bool(search.get("direct_only")),
        max_stops=max_stops if type(max_stops) is int else None,
    )


def round_trip_match_key(offer: FlightOffer) -> str:
    """``offer_key`` with the inbound reduced to ``RT:<return date>`` (how search results look)."""
    if offer.inbound is None:
        return offer.offer_key()
    return_date = offer.return_date or offer.inbound.departure_time[:10]
    return replace(offer, inbound=None, return_date=return_date).offer_key()


def pick_return_option(
    options: Sequence[ReturnOption], round_trip_total: float
) -> ReturnOption | None:
    """The first return priced at the offer's listed total (Google's own pairing), else the
    cheapest one; ``None`` when there are no options."""
    for option in options:
        if abs(option.price - round_trip_total) < PRICE_MATCH_TOLERANCE:
            return option
    return min(options, key=lambda option: option.price, default=None)


def attach_inbound(
    offer: FlightOffer,
    query: FlightQuery,
    outbound: Sequence[SelectedFlight],
    option: ReturnOption,
) -> FlightOffer:
    """``offer`` with ``option`` as inbound leg and the "Booking options" link (hl=ko).

    When the return flight's round-trip total differs from the offer's listed price (fares
    moved since the search), it is recorded as ``raw["inbound_round_trip_total"]``.
    """
    inbound = selected_from_leg(option.leg)
    if inbound:
        link = booking_link(query, outbound, inbound)
    else:
        link = user_link(query, outbound=outbound)
    raw = {**offer.raw, "inbound_source": "outbound_selected_search"}
    if abs(option.price - offer.price_total) >= PRICE_MATCH_TOLERANCE:
        raw["inbound_round_trip_total"] = option.price
    return replace(offer, inbound=option.leg, booking_url=link, raw=raw)


# --------------------------------------------------------------------------- provider


@register
class GoogleFlightsProvider(FlightProvider):
    """Google Flights result pages, parsed locally (no key; personal low-volume use)."""

    key = "google_flights"
    display_name = "Google Flights"
    description = (
        "Google Flights result pages, no API key. Uses the installed Edge (headless) by "
        "default, or plain HTTP (setting). Prices are Google's displayed fares: round-trip "
        "total for all passengers. Personal low-volume use only; stops on any block or captcha."
    )
    uses_browser = True
    supports_metro_codes = True
    max_queries_per_run = 8
    enabled_by_default = True

    @property
    def fetch_mode(self) -> str:
        """``"browser"`` (default) or ``"http"`` from ``ctx.options["fetch_mode"]``."""
        mode = str(self.ctx.options.get("fetch_mode") or FETCH_BROWSER).strip().lower()
        if mode not in FETCH_MODES:
            logger.warning("Unknown google_flights fetch_mode %r; using %r", mode, FETCH_BROWSER)
            return FETCH_BROWSER
        return mode

    @property
    def block_cooldown_s(self) -> float:
        """Pause after a block, from ``ctx.options["block_cooldown_minutes"]`` (0 = no pause)."""
        value = self.ctx.options.get("block_cooldown_minutes")
        if value is None:
            return DEFAULT_BLOCK_COOLDOWN_MINUTES * 60
        minutes = _finite_number(value)
        if minutes is None or minutes < 0:
            logger.warning(
                "Invalid google_flights block_cooldown_minutes %r; using %g",
                value,
                DEFAULT_BLOCK_COOLDOWN_MINUTES,
            )
            minutes = DEFAULT_BLOCK_COOLDOWN_MINUTES
        return minutes * 60

    def status(self) -> ProviderStatus:
        """Always configured (no key); the message names the fetch mode and a running pause.

        A pause after a block keeps the provider "configured" on purpose: its searches raise
        :class:`ProviderBlocked`, which the search service reports and falls back from.
        """
        if self.fetch_mode == FETCH_HTTP:
            message = "No API key needed; fetch mode: plain HTTP"
        else:
            message = "No API key needed; fetch mode: browser (headless Edge)"
        left, _reason = BLOCK_COOLDOWN.remaining()
        if left > 0:
            message += f"; paused after a Google block until {local_clock_after(left)}"
        return ProviderStatus(True, message)

    def search_flights(self, query: FlightQuery) -> list[FlightOffer]:
        """One Google request for ``query``; outbound options priced at the round-trip total."""
        page = self._fetch(search_url(query))
        offers = self._offers_from_page(page, query)
        cheapest = min((offer.price_total for offer in offers), default=None)
        logger.info(
            "Google Flights %s->%s %s%s (%s): %d offers%s",
            query.origin,
            query.destination,
            query.depart_date.isoformat(),
            f"/{query.return_date.isoformat()}" if query.return_date else "",
            page.mode,
            len(offers),
            f", cheapest {query.currency} {cheapest:,.0f}" if cheapest is not None else "",
        )
        return offers

    def get_flight_details(self, offer: FlightOffer) -> FlightOffer:
        """Fill the unknown inbound leg of a round-trip offer (one extra request).

        Fetches the search with the offer's outbound selected, takes the return flight priced
        at the offer's listed total (else the cheapest return) and sets the "Booking options"
        link. Best effort: returns ``offer`` unchanged when it already has an inbound leg, is
        one-way, lacks flight numbers, or the request fails (logged). Never mutates ``offer``.
        """
        if offer.inbound is not None or not offer.return_date:
            return offer
        outbound = outbound_selection(offer)
        if not outbound:
            logger.info("Google Flights: no flight numbers to select for %s", offer.offer_key())
            return offer
        query = search_query_for_offer(offer)
        try:
            options = self._return_options(query, outbound)
        except ProviderError as exc:
            logger.warning("Google Flights return-flight lookup failed: %s", exc)
            return offer
        picked = pick_return_option(options, offer.price_total)
        if picked is None:
            logger.info("Google Flights listed no priced return for %s", offer.offer_key())
            return offer
        return attach_inbound(offer, query, outbound, picked)

    def get_price(self, offer: FlightOffer) -> float | None:
        """Current per-person price of ``offer``'s itinerary, ``None`` if no longer listed.

        Repeats the original search and matches the outbound (+ return date); when the inbound
        leg is known, one more request prices that exact return flight.
        """
        query = search_query_for_offer(offer)
        target = round_trip_match_key(offer)
        match = next(
            (c for c in self.search_flights(query) if round_trip_match_key(c) == target), None
        )
        if match is None or offer.inbound is None:
            return match.price_per_person if match is not None else None
        outbound = outbound_selection(match)
        if not outbound:
            return None
        wanted = leg_key(offer.inbound)
        for option in self._return_options(query, outbound):
            if leg_key(option.leg) == wanted:
                return round(option.price / max(offer.adults, 1), 2)
        return None

    def get_booking_url(self, offer: FlightOffer) -> str | None:
        """The hl=ko user link: booking options / return list when known, else the search."""
        return offer.booking_url or offer.source_url or user_link(search_query_for_offer(offer))

    # ------------------------------------------------------------------ internals

    def _fetch(self, url: str) -> FetchedPage:
        """Fetch ``url`` in the configured mode (spaced); raises on blocks and HTTP errors.

        Sends nothing while a block pause is running (checked again after waiting for the
        request slot, since another thread may have been blocked meanwhile).
        """
        self._raise_if_paused()
        with REQUEST_SPACER.slot():
            self._raise_if_paused()
            page = self._fetch_browser(url) if self.fetch_mode == FETCH_BROWSER else None
            if page is None:
                page = fetch_page_with_http(url, self.ctx.http_timeout)
        self._check_page(page)
        return page

    def _raise_if_paused(self) -> None:
        left, reason = BLOCK_COOLDOWN.remaining()
        if left > 0:
            raise ProviderBlocked(
                f"Google Flights is paused until {local_clock_after(left)} after a block "
                f"({reason}); no request sent. Use another provider (e.g. SerpApi) meanwhile."
            )

    def _blocked(self, reason: str, page: FetchedPage) -> ProviderBlocked:
        """The :class:`ProviderBlocked` for ``page``; starts the process-wide block pause."""
        seconds = self.block_cooldown_s
        BLOCK_COOLDOWN.start(seconds, reason)
        if seconds > 0:
            logger.warning(
                "Google Flights blocked (%s); pausing Google requests for %.0f min",
                reason,
                seconds / 60,
            )
            advice = f"Google requests are paused until {local_clock_after(seconds)}"
        else:
            advice = "wait a few hours"
        return ProviderBlocked(
            f"Google Flights blocked the request ({reason}; {page.mode} mode{_shot(page)}). "
            f"Not retried: {advice}; use another provider (e.g. SerpApi) meanwhile."
        )

    def _fetch_browser(self, url: str) -> FetchedPage | None:
        """Fetch with the browser manager; ``None`` = no browser available (use HTTP)."""
        manager = self.ctx.browser
        if manager is None:
            logger.warning("Google Flights: no browser manager available; using plain HTTP")
            return None
        from playwright.sync_api import Error as PlaywrightError

        from app.browser.manager import BrowserError, BrowserUnavailableError

        def job(handle: BrowserHandle) -> FetchedPage:
            return fetch_page_with_browser(handle, url)

        try:
            return manager.run(job, timeout=BROWSER_JOB_TIMEOUT_S, label=BROWSER_JOB_LABEL)
        except BrowserUnavailableError as exc:
            logger.warning("Google Flights: %s Using plain HTTP instead.", _first_line(exc))
            return None
        except TimeoutError:
            raise ProviderError(
                f"Google Flights page did not load within {BROWSER_JOB_TIMEOUT_S:g} s (browser)"
            ) from None
        except (BrowserError, PlaywrightError) as exc:
            message = f"Google Flights browser fetch failed: {_first_line(exc)}"
            raise ProviderError(message) from None

    def _check_page(self, page: FetchedPage) -> None:
        reason = detect_block(page.html, page.url, page.status)
        if reason is not None:
            raise self._blocked(reason, page)
        if page.status is not None and page.status >= 400:
            raise ProviderError(f"Google Flights returned HTTP {page.status}")

    def _offers_from_page(self, page: FetchedPage, query: FlightQuery) -> list[FlightOffer]:
        """ds:1 offers, else the aria-label fallback.

        :class:`ProviderBlocked` only when the page has neither a ds:1 script nor result rows.
        :class:`ProviderError` (a format change, not a block) when ds:1 is present but
        undecodable / of unknown layout and there are no rows, or when rows exist but none
        produced an offer and some could not be read. ``[]`` when Google lists no flights or
        every row is legitimately unpriced / off-route.
        """
        payload = extract_ds1(page.html)
        if payload is not None:
            if is_no_results(payload):
                return []
            try:
                return parse_offers(payload, query, provider_key=self.key)
            except Ds1FormatError as exc:
                logger.warning(
                    "Google Flights ds:1 layout not recognised (%s); using row labels", exc
                )
                ds1 = "unknown layout"
        elif has_ds1(page.html):
            logger.warning("Google Flights ds:1 data could not be decoded; using row labels")
            ds1 = "not decodable"
        else:
            ds1 = "missing"
        dom = parse_dom_rows(page.html, query, provider_key=self.key)
        if dom.offers:
            return dom.offers
        if dom.rows == 0:
            if ds1 == "missing":
                raise self._blocked("page has no flight data (no ds:1 and no result rows)", page)
            raise ProviderError(
                f"Google Flights page layout not recognised (ds:1 {ds1}, no result rows"
                f"{_shot(page)})"
            )
        if dom.unparsable:
            raise ProviderError(
                f"Google Flights result rows not recognised ({dom.unparsable} of {dom.rows} "
                f"row(s) unreadable; ds:1 {ds1})"
            )
        return []

    def _return_options(
        self, query: FlightQuery, outbound: Sequence[SelectedFlight]
    ) -> list[ReturnOption]:
        """Return flights listed for ``outbound`` (one request, outbound selected).

        Same classification as the search page: no ds:1 script and no result rows is a block;
        ds:1 present but undecodable / of unknown layout (or rows without ds:1) is an error.
        """
        page = self._fetch(search_url(query, outbound=outbound))
        payload = extract_ds1(page.html)
        if payload is None:
            if has_ds1(page.html):
                state = "not decodable"
            elif count_result_rows(page.html) > 0:
                state = "missing (result rows only)"
            else:
                reason = "return-flight page has no flight data (no ds:1 and no result rows)"
                raise self._blocked(reason, page)
            raise ProviderError(f"Google Flights return-flight page: ds:1 {state}{_shot(page)}")
        try:
            return parse_return_options(payload, query)
        except Ds1FormatError as exc:
            message = f"Google Flights return-flight page not recognised: {exc}"
            raise ProviderError(message) from None


def _shot(page: FetchedPage) -> str:
    return f"; screenshot: {page.screenshot}" if page.screenshot else ""

