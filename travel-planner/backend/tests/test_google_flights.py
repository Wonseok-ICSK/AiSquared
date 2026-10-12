"""GoogleFlightsProvider: fetch modes, block handling, spacing, details, registry, live checks.

Pages come from ``fixtures/google_flights`` (see test_google_flights_parse.py). The browser is a
fake ``BrowserManager``; HTTP is mocked with respx. Live tests (``RUN_LIVE=1``) hit Google with
at most 4 requests spaced >= 10 s.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from app.browser.manager import BrowserUnavailableError
from app.providers.flights import default_provider_keys, get_provider_class
from app.providers.flights import google_flights as gf
from app.providers.flights.base import (
    FlightOffer,
    FlightQuery,
    ProviderBlocked,
    ProviderContext,
    ProviderError,
)
from app.providers.flights.google_flights import (
    DEFAULT_BLOCK_COOLDOWN_MINUTES,
    FETCH_BROWSER,
    FETCH_HTTP,
    HTTP_USER_AGENT,
    BlockCooldown,
    GoogleFlightsProvider,
    RequestSpacer,
    outbound_selection,
    pick_return_option,
    round_trip_match_key,
    search_query_for_offer,
)
from app.providers.flights.google_flights_parse import ReturnOption, extract_ds1, parse_offers
from app.providers.flights.google_flights_tfs import (
    USER_BOOKING_URL,
    SelectedFlight,
    query_tfs,
    search_url,
)

FIXTURES = Path(__file__).parent / "fixtures" / "google_flights"
ICN_NRT_2A = FlightQuery("ICN", "NRT", date(2026, 11, 20), date(2026, 11, 27), adults=2)
SEARCH_TFS = query_tfs(ICN_NRT_2A)
YP735 = SelectedFlight("ICN", "2026-11-20", "NRT", "YP", "735")
RETURN_TFS = query_tfs(ICN_NRT_2A, outbound=[YP735])
BOTH_SELECTED_TFS = (  # Google UI's own URL after picking YP735 + YP732 (research, 2026-10-06)
    "CBwQAho_EgoyMDI2LTExLTIwIh8KA0lDThIKMjAyNi0xMS0yMBoDTlJUKgJZUDIDNzM1agcIARIDSUNOcgcIARIDTlJU"
    "Gj8SCjIwMjYtMTEtMjciHwoDTlJUEgoyMDI2LTExLTI3GgNJQ04qAllQMgM3MzJqBwgBEgNOUlRyBwgBEgNJQ05AAUAB"
    "SAFwAYIBCwj___________8BmAEB"
)
GOOGLE = "https://www.google.com/travel/flights"
SORRY_HTML = (
    "<html><body>Our systems have detected unusual traffic from your computer network."
    '<form id="captcha-form" action="index" method="post"></form></body></html>'
)


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def no_wait_spacer(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[float]]:
    """Unit tests never sleep: spacing is recorded instead (fake clock advanced by sleeps)."""
    now = [1000.0]
    sleeps: list[float] = []

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    spacer = RequestSpacer(clock=lambda: now[0], sleep=fake_sleep, rng=lambda: 0.5)
    monkeypatch.setattr(gf, "REQUEST_SPACER", spacer)
    yield sleeps


@dataclass
class CooldownClock:
    """A fresh process-wide block pause per test, on a clock tests can move forward."""

    now: float = 0.0
    cooldown: BlockCooldown = field(init=False)

    def __post_init__(self) -> None:
        self.cooldown = BlockCooldown(clock=lambda: self.now)


@pytest.fixture(autouse=True)
def cooldown_clock(monkeypatch: pytest.MonkeyPatch) -> CooldownClock:
    """No test inherits another test's block pause."""
    clock = CooldownClock()
    monkeypatch.setattr(gf, "BLOCK_COOLDOWN", clock.cooldown)
    return clock


def provider(
    mode: str | None = None, browser: Any = None, **options: Any
) -> GoogleFlightsProvider:
    if mode:
        options["fetch_mode"] = mode
    ctx = ProviderContext(get_secret=lambda _name: None, browser=browser, options=options)
    return GoogleFlightsProvider(ctx)


# --------------------------------------------------------------------------- fake browser


@dataclass
class FakeResponse:
    status: int


class FakeLocator:
    def __init__(self, page: FakePage) -> None:
        self._page = page

    @property
    def first(self) -> FakeLocator:
        return self

    def wait_for(self, state: str = "visible", timeout: float | None = None) -> None:
        self._page.waits.append((state, timeout))
        if not self._page.rows_appear:
            raise PlaywrightTimeoutError("Timeout 15000ms exceeded.")


@dataclass
class FakePage:
    html: str
    final_url: str | None = None
    status: int = 200
    rows_appear: bool = True
    visits: list[tuple[str, str, float]] = field(default_factory=list)
    waits: list[tuple[str, float | None]] = field(default_factory=list)
    goto_error: Exception | None = None

    @property
    def url(self) -> str:
        return self.final_url or (self.visits[-1][0] if self.visits else "about:blank")

    def goto(self, url: str, wait_until: str = "load", timeout: float = 0) -> FakeResponse:
        self.visits.append((url, wait_until, timeout))
        if self.goto_error is not None:
            raise self.goto_error
        return FakeResponse(self.status)

    def content(self) -> str:
        return self.html

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(self)


@dataclass
class FakeContext:
    page: FakePage

    def new_page(self) -> FakePage:
        return self.page


@dataclass
class FakeHandle:
    page: FakePage
    contexts: list[dict[str, Any]] = field(default_factory=list)
    screenshots: list[str] = field(default_factory=list)

    def new_context(self, browser: Any = None, **kwargs: Any) -> FakeContext:
        self.contexts.append(kwargs)
        return FakeContext(self.page)

    def screenshot(self, page: FakePage, name: str) -> Path:
        self.screenshots.append(name)
        return Path("C:/logs/screenshots") / f"{name}.png"


@dataclass
class FakeBrowserManager:
    pages: list[FakePage]
    error: Exception | None = None
    handles: list[FakeHandle] = field(default_factory=list)
    runs: list[tuple[float, str]] = field(default_factory=list)

    def run(self, fn: Callable[[Any], Any], timeout: float = 180.0, label: str = "") -> Any:
        self.runs.append((timeout, label))
        if self.error is not None:
            raise self.error
        handle = FakeHandle(self.pages.pop(0))
        self.handles.append(handle)
        return fn(handle)


# --------------------------------------------------------------------------- registry / status


def test_registered_as_default_provider() -> None:
    cls = get_provider_class("google_flights")
    assert cls is GoogleFlightsProvider
    assert "google_flights" in default_provider_keys()
    assert cls.uses_browser and cls.supports_metro_codes and cls.enabled_by_default
    assert cls.max_queries_per_run == 8
    assert not cls.requires_api_key


def test_status_and_fetch_mode() -> None:
    assert provider().fetch_mode == FETCH_BROWSER
    assert provider("HTTP").fetch_mode == FETCH_HTTP
    assert provider("weird").fetch_mode == FETCH_BROWSER
    assert provider().status().configured
    assert "HTTP" in provider("http").status().message


# --------------------------------------------------------------------------- browser mode


def test_browser_mode_parses_ds1_without_waiting_for_rows() -> None:
    page = FakePage(fixture("rt_icn_nrt_2adults.html"))
    manager = FakeBrowserManager([page])
    offers = provider(browser=manager).search_flights(ICN_NRT_2A)
    assert len(offers) == 48
    assert min(o.price_total for o in offers) == 631400
    url, wait_until, _ = page.visits[0]
    assert wait_until == "domcontentloaded"
    assert url == search_url(ICN_NRT_2A)
    assert page.waits == []  # ds:1 present: no row wait
    handle = manager.handles[0]
    assert handle.contexts == [{"headless": True}]
    assert handle.screenshots == []
    assert manager.runs == [(gf.BROWSER_JOB_TIMEOUT_S, "google_flights")]


def test_browser_mode_dom_fallback_waits_for_rows() -> None:
    page = FakePage(fixture("dom_rows_en.html"))
    offers = provider(browser=FakeBrowserManager([page])).search_flights(ICN_NRT_2A)
    assert [o.raw["source"] for o in offers] == ["dom"] * 3
    assert page.waits and page.waits[0][0] == "attached"


def test_browser_block_raises_with_screenshot() -> None:
    sorry_url = "https://www.google.com/sorry/index?continue=x"
    page = FakePage(SORRY_HTML, final_url=sorry_url, status=429)
    manager = FakeBrowserManager([page])
    with pytest.raises(ProviderBlocked) as caught:
        provider(browser=manager).search_flights(ICN_NRT_2A)
    assert "HTTP 429" in str(caught.value)
    assert "screenshot" in str(caught.value)
    assert manager.handles[0].screenshots == [gf.SCREENSHOT_NAME]
    assert page.waits == []  # a block page is not waited on


def test_browser_page_without_ds1_or_rows_is_blocked() -> None:
    page = FakePage("<html><body>Something else</body></html>", rows_appear=False)
    manager = FakeBrowserManager([page])
    with pytest.raises(ProviderBlocked, match="no ds:1 and no result rows") as caught:
        provider(browser=manager).search_flights(ICN_NRT_2A)
    assert manager.handles[0].screenshots == [gf.SCREENSHOT_NAME]
    assert "screenshot" in str(caught.value)
    assert page.waits  # rows were waited for before giving up


def test_consent_redirect_in_browser_is_blocked() -> None:
    page = FakePage("<html>consent</html>", final_url="https://consent.google.com/ml?continue=x")
    with pytest.raises(ProviderBlocked, match="consent"):
        provider(browser=FakeBrowserManager([page])).search_flights(ICN_NRT_2A)


@respx.mock
def test_browser_unavailable_falls_back_to_http() -> None:
    route = respx.get(url__startswith=GOOGLE).mock(
        return_value=httpx.Response(200, text=fixture("rt_icn_nrt_2adults.html"))
    )
    manager = FakeBrowserManager([], error=BrowserUnavailableError("No browser could be launched."))
    offers = provider(browser=manager).search_flights(ICN_NRT_2A)
    assert len(offers) == 48
    assert route.call_count == 1


@respx.mock
def test_missing_browser_manager_falls_back_to_http() -> None:
    respx.get(url__startswith=GOOGLE).mock(
        return_value=httpx.Response(200, text=fixture("rt_icn_nrt_2adults.html"))
    )
    assert len(provider(browser=None).search_flights(ICN_NRT_2A)) == 48


@pytest.mark.parametrize(
    "error",
    [TimeoutError("browser job timed out"), PlaywrightError("net::ERR_NAME_NOT_RESOLVED")],
)
def test_browser_failures_are_provider_errors(error: Exception) -> None:
    manager = FakeBrowserManager([], error=error)
    with pytest.raises(ProviderError) as caught:
        provider(browser=manager).search_flights(ICN_NRT_2A)
    assert not isinstance(caught.value, ProviderBlocked)


def test_navigation_error_inside_job_is_provider_error() -> None:
    page = FakePage("", goto_error=PlaywrightError("net::ERR_CONNECTION_RESET"))
    with pytest.raises(ProviderError, match="ERR_CONNECTION_RESET"):
        provider(browser=FakeBrowserManager([page])).search_flights(ICN_NRT_2A)


# --------------------------------------------------------------------------- http mode


@respx.mock
def test_http_mode_request_and_offers() -> None:
    route = respx.get(url__startswith=GOOGLE).mock(
        return_value=httpx.Response(200, text=fixture("rt_icn_nrt_2adults.html"))
    )
    browser = FakeBrowserManager([])
    offers = provider("http", browser=browser).search_flights(ICN_NRT_2A)
    assert len(offers) == 48 and browser.runs == []
    request = route.calls.last.request
    assert request.url.path == "/travel/flights"
    assert request.url.params["tfs"] == SEARCH_TFS
    assert request.url.params["tfu"] == "EgQIABABIgA"
    assert request.url.params["curr"] == "KRW"
    assert request.url.params["hl"] == "en"
    assert request.headers["User-Agent"] == HTTP_USER_AGENT
    assert "Edg/" in HTTP_USER_AGENT and "Headless" not in HTTP_USER_AGENT
    assert request.headers["Accept-Language"].startswith("en")


@respx.mock
def test_http_429_is_blocked() -> None:
    respx.get(url__startswith=GOOGLE).mock(return_value=httpx.Response(429, text="Too many"))
    with pytest.raises(ProviderBlocked, match="429"):
        provider("http").search_flights(ICN_NRT_2A)


@respx.mock
def test_http_redirect_to_sorry_page_is_blocked() -> None:
    sorry = "https://www.google.com/sorry/index?continue=x"
    respx.get(url__startswith=GOOGLE).mock(
        return_value=httpx.Response(302, headers={"Location": sorry})
    )
    respx.get(url__startswith="https://www.google.com/sorry/").mock(
        return_value=httpx.Response(200, text=SORRY_HTML)
    )
    with pytest.raises(ProviderBlocked, match="sorry"):
        provider("http").search_flights(ICN_NRT_2A)


@respx.mock
def test_http_server_error_and_network_error() -> None:
    respx.get(url__startswith=GOOGLE).mock(return_value=httpx.Response(500, text="oops"))
    with pytest.raises(ProviderError, match="HTTP 500"):
        provider("http").search_flights(ICN_NRT_2A)
    respx.get(url__startswith=GOOGLE).mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(ProviderError, match="request failed"):
        provider("http").search_flights(ICN_NRT_2A)
    respx.get(url__startswith=GOOGLE).mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(ProviderError, match="timed out"):
        provider("http").search_flights(ICN_NRT_2A)


@respx.mock
def test_no_results_pages_return_empty_lists() -> None:
    error_page = (
        "<script class=\"ds:1\">AF_initDataCallback({key: 'ds:1', hash: '1', data:[null,[3]],"
        "errorHasStatus: true,});</script>"
    )
    empty_page = (
        "<script class=\"ds:1\">AF_initDataCallback({key: 'ds:1', data:[], sideChannel: {}});"
        "</script>"
    )
    for html in (error_page, empty_page):
        respx.get(url__startswith=GOOGLE).mock(return_value=httpx.Response(200, text=html))
        assert provider("http").search_flights(ICN_NRT_2A) == []


@respx.mock
def test_unrecognised_ds1_falls_back_to_rows_then_errors() -> None:
    weird = (
        "<script class=\"ds:1\">AF_initDataCallback({key: 'ds:1', data:[1], sideChannel: {}});"
        "</script>"
    )
    respx.get(url__startswith=GOOGLE).mock(
        return_value=httpx.Response(200, text=weird + fixture("dom_rows_en.html"))
    )
    offers = provider("http").search_flights(ICN_NRT_2A)
    assert [o.raw["source"] for o in offers] == ["dom"] * 3
    respx.get(url__startswith=GOOGLE).mock(return_value=httpx.Response(200, text=weird))
    with pytest.raises(ProviderError, match="layout not recognised") as caught:
        provider("http").search_flights(ICN_NRT_2A)
    assert not isinstance(caught.value, ProviderBlocked)


# --------------------------------------------------------------------------- format vs block

UNDECODABLE_DS1 = (
    "<script class=\"ds:1\">AF_initDataCallback({key: 'ds:1', hash: '5', "
    "data:[null,null,[[1,,2]]], sideChannel: {}});</script>"
)
UNPRICED_ROW = (
    '<ul><li><div role="link" aria-label="Total price is unavailable. Nonstop flight with '
    "EASTAR JET. Leaves Incheon International Airport at 8:40 AM on Friday, November 20 and "
    "arrives at Narita International Airport at 10:55 AM on Friday, November 20. Total duration "
    '2 hr 15 min."></div></li></ul>'
)


def _no_tim_links(html: str) -> str:
    """Rows without the travelimpactmodel link or the route-code class (airports unreadable)."""
    return html.replace("data-travelimpactmodelwebsiteurl", "data-x").replace("QylvBf", "Zz9")


@respx.mock
def test_undecodable_ds1_without_rows_is_a_format_error(cooldown_clock: CooldownClock) -> None:
    route = respx.get(url__startswith=GOOGLE).mock(
        return_value=httpx.Response(200, text=UNDECODABLE_DS1)
    )
    with pytest.raises(ProviderError, match="not decodable") as caught:
        provider("http").search_flights(ICN_NRT_2A)
    assert not isinstance(caught.value, ProviderBlocked)
    assert "no ds:1 and no result rows" not in str(caught.value)
    assert cooldown_clock.cooldown.remaining() == (0.0, "")  # a format error does not pause
    with pytest.raises(ProviderError):
        provider("http").search_flights(ICN_NRT_2A)
    assert route.call_count == 2


@respx.mock
def test_undecodable_ds1_falls_back_to_rows() -> None:
    respx.get(url__startswith=GOOGLE).mock(
        return_value=httpx.Response(200, text=UNDECODABLE_DS1 + fixture("dom_rows_en.html"))
    )
    offers = provider("http").search_flights(ICN_NRT_2A)
    assert [o.raw["source"] for o in offers] == ["dom"] * 3


def test_browser_undecodable_ds1_waits_for_rows_and_screenshots() -> None:
    page = FakePage(UNDECODABLE_DS1, rows_appear=False)
    manager = FakeBrowserManager([page])
    with pytest.raises(ProviderError, match="not decodable") as caught:
        provider(browser=manager).search_flights(ICN_NRT_2A)
    assert not isinstance(caught.value, ProviderBlocked)
    assert page.waits and page.waits[0][0] == "attached"  # unusable ds:1: rows waited for
    assert manager.handles[0].screenshots == [gf.UNRECOGNISED_SCREENSHOT_NAME]
    assert "screenshot" in str(caught.value)


@pytest.mark.parametrize(
    "mangle",
    [_no_tim_links, lambda html: html.replace("Leaves ", "Departs ")],
    ids=["airports-unreadable", "times-unreadable"],
)
@respx.mock
def test_unreadable_result_rows_are_a_format_error(mangle: Callable[[str], str]) -> None:
    respx.get(url__startswith=GOOGLE).mock(
        return_value=httpx.Response(200, text=mangle(fixture("dom_rows_en.html")))
    )
    with pytest.raises(ProviderError, match="rows not recognised") as caught:
        provider("http").search_flights(ICN_NRT_2A)
    assert not isinstance(caught.value, ProviderBlocked)
    assert "3 of 4" in str(caught.value)  # the 4th row is legitimately unpriced


@respx.mock
def test_rows_that_are_all_unpriced_or_off_route_give_no_offers() -> None:
    respx.get(url__startswith=GOOGLE).mock(return_value=httpx.Response(200, text=UNPRICED_ROW))
    assert provider("http").search_flights(ICN_NRT_2A) == []
    respx.get(url__startswith=GOOGLE).mock(
        return_value=httpx.Response(200, text=fixture("dom_rows_en.html"))
    )
    assert provider("http").search_flights(replace(ICN_NRT_2A, destination="KIX")) == []


@respx.mock
def test_return_page_without_ds1_or_rows_is_blocked(cooldown_clock: CooldownClock) -> None:
    respx.get(url__startswith=GOOGLE).mock(
        return_value=httpx.Response(200, text="<html><body>Something went wrong</body></html>")
    )
    with pytest.raises(ProviderBlocked, match="no ds:1 and no result rows"):
        provider("http")._return_options(ICN_NRT_2A, [YP735])
    assert cooldown_clock.cooldown.remaining()[0] > 0


def test_browser_return_page_without_ds1_is_blocked_with_screenshot() -> None:
    page = FakePage("<html><body>Something went wrong</body></html>", rows_appear=False)
    manager = FakeBrowserManager([page])
    with pytest.raises(ProviderBlocked, match="screenshot"):
        provider(browser=manager)._return_options(ICN_NRT_2A, [YP735])
    assert manager.handles[0].screenshots == [gf.SCREENSHOT_NAME]


@respx.mock
def test_return_page_with_undecodable_ds1_or_rows_only_is_a_format_error() -> None:
    for html, reason in (
        (UNDECODABLE_DS1, "not decodable"),
        (fixture("dom_rows_en.html"), "result rows only"),
    ):
        respx.get(url__startswith=GOOGLE).mock(return_value=httpx.Response(200, text=html))
        with pytest.raises(ProviderError, match=reason) as caught:
            provider("http")._return_options(ICN_NRT_2A, [YP735])
        assert not isinstance(caught.value, ProviderBlocked)
    assert gf.BLOCK_COOLDOWN.remaining() == (0.0, "")


# --------------------------------------------------------------------------- block pause


@respx.mock
def test_block_pauses_all_google_requests(cooldown_clock: CooldownClock) -> None:
    route = respx.get(url__startswith=GOOGLE).mock(return_value=httpx.Response(429))
    with pytest.raises(ProviderBlocked, match="paused until") as first:
        provider("http").search_flights(ICN_NRT_2A)
    assert "HTTP 429" in str(first.value)
    other = provider("http")  # another trip's job: separate instance, same process
    with pytest.raises(ProviderBlocked, match="no request sent") as paused:
        other.search_flights(ICN_NRT_2A)
    assert "HTTP 429" in str(paused.value)
    assert other.get_flight_details(cheapest_offer()).inbound is None  # best effort, no request
    assert "paused after a Google block" in other.status().message
    assert other.status().configured  # still runs, so the search service reports the block
    assert route.call_count == 1
    left, _ = cooldown_clock.cooldown.remaining()
    assert left == pytest.approx(DEFAULT_BLOCK_COOLDOWN_MINUTES * 60)
    cooldown_clock.now += left + 1
    route.mock(return_value=httpx.Response(200, text=fixture("rt_icn_nrt_2adults.html")))
    assert len(other.search_flights(ICN_NRT_2A)) == 48
    assert route.call_count == 2
    assert "paused" not in other.status().message


@respx.mock
def test_pause_is_rechecked_after_waiting_for_the_request_slot(
    monkeypatch: pytest.MonkeyPatch, cooldown_clock: CooldownClock
) -> None:
    route = respx.get(url__startswith=GOOGLE).mock(
        return_value=httpx.Response(200, text=fixture("rt_icn_nrt_2adults.html"))
    )

    def blocked_while_waiting(_seconds: float) -> None:  # another thread hit a captcha meanwhile
        cooldown_clock.cooldown.start(3600, "Google captcha page")

    spacer = RequestSpacer(clock=lambda: 0.0, sleep=blocked_while_waiting, rng=lambda: 0.0)
    monkeypatch.setattr(gf, "REQUEST_SPACER", spacer)
    provider("http").search_flights(ICN_NRT_2A)
    with pytest.raises(ProviderBlocked, match="captcha"):
        provider("http").search_flights(ICN_NRT_2A)
    assert route.call_count == 1


@respx.mock
def test_block_cooldown_option(cooldown_clock: CooldownClock) -> None:
    route = respx.get(url__startswith=GOOGLE).mock(return_value=httpx.Response(429))
    no_pause = provider("http", block_cooldown_minutes=0)
    for _ in range(2):
        with pytest.raises(ProviderBlocked, match="wait a few hours"):
            no_pause.search_flights(ICN_NRT_2A)
    assert route.call_count == 2
    assert provider(block_cooldown_minutes="30").block_cooldown_s == 1800
    default = DEFAULT_BLOCK_COOLDOWN_MINUTES * 60
    for bad in ("soon", -5, float("inf"), True):
        assert provider(block_cooldown_minutes=bad).block_cooldown_s == default
    with pytest.raises(ProviderBlocked):
        provider("http", block_cooldown_minutes=30).search_flights(ICN_NRT_2A)
    assert cooldown_clock.cooldown.remaining()[0] == pytest.approx(1800)


def test_block_cooldown_extends_never_shortens() -> None:
    now = [0.0]
    cooldown = BlockCooldown(clock=lambda: now[0])
    assert cooldown.remaining() == (0.0, "")
    cooldown.start(600, "HTTP 429 from Google")
    cooldown.start(60, "Google captcha page")
    assert cooldown.remaining() == (600, "HTTP 429 from Google")
    cooldown.start(0, "ignored")
    now[0] = 601
    assert cooldown.remaining() == (0.0, "")
    cooldown.start(60, "Google consent wall")
    cooldown.reset()
    assert cooldown.remaining() == (0.0, "")


# --------------------------------------------------------------------------- spacing


def test_spacer_waits_between_requests_and_serializes() -> None:
    now = [0.0]
    sleeps: list[float] = []

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    spacer = RequestSpacer(5.0, 5.0, clock=lambda: now[0], sleep=fake_sleep, rng=lambda: 0.4)
    with spacer.slot() as waited:
        assert waited == 0
        now[0] += 1.0  # the request takes 1 s
    with spacer.slot() as waited:
        assert waited == pytest.approx(7.0)  # 5 + 0.4 * 5, counted from the end of the request
    now[0] += 30.0
    with spacer.slot() as waited:
        assert waited == 0  # long enough ago
    assert sleeps == [pytest.approx(7.0)]
    spacer.reset()
    with spacer.slot() as waited:
        assert waited == 0


def test_spacer_defaults_resolve_time_functions_at_call_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [50.0]
    sleeps: list[float] = []
    monkeypatch.setattr(gf.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(gf.time, "sleep", sleeps.append)
    monkeypatch.setattr(gf.random, "random", lambda: 0.0)
    spacer = RequestSpacer()
    with spacer.slot():
        pass
    with spacer.slot():
        pass
    assert sleeps == [gf.MIN_REQUEST_INTERVAL_S]
    assert gf.MIN_REQUEST_INTERVAL_S >= 5.0


def test_spacer_counts_failed_requests() -> None:
    now = [0.0]
    sleeps: list[float] = []
    spacer = RequestSpacer(5.0, 0.0, clock=lambda: now[0], sleep=sleeps.append)
    with pytest.raises(RuntimeError), spacer.slot():
        raise RuntimeError("request failed")
    with spacer.slot():
        pass
    assert sleeps == [5.0]


@respx.mock
def test_consecutive_searches_are_spaced(no_wait_spacer: list[float]) -> None:
    route = respx.get(url__startswith=GOOGLE).mock(
        return_value=httpx.Response(200, text=fixture("rt_icn_nrt_2adults.html"))
    )
    first, second = provider("http"), provider("http")  # spacing is per process, not instance
    first.search_flights(ICN_NRT_2A)
    second.search_flights(ICN_NRT_2A)
    assert route.call_count == 2
    assert len(no_wait_spacer) == 1
    assert no_wait_spacer[0] >= 5.0


# --------------------------------------------------------------------------- details / price


def details_router() -> respx.Route:
    pages = {
        SEARCH_TFS: fixture("rt_icn_nrt_2adults.html"),
        RETURN_TFS: fixture("return_list_after_yp735.html"),
    }

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=pages[request.url.params["tfs"]])

    return respx.get(url__startswith=GOOGLE).mock(side_effect=respond)


def cheapest_offer() -> FlightOffer:
    payload = extract_ds1(fixture("rt_icn_nrt_2adults.html"))
    assert payload is not None
    return min(parse_offers(payload, ICN_NRT_2A), key=lambda offer: offer.price_total)


@respx.mock
def test_get_flight_details_fills_inbound_and_booking_link() -> None:
    route = details_router()
    offer = cheapest_offer()
    detailed = provider("http").get_flight_details(offer)
    assert route.call_count == 1
    assert route.calls.last.request.url.params["tfs"] == RETURN_TFS
    assert detailed.inbound is not None
    assert detailed.inbound.flight_numbers == ["YP732"]
    assert detailed.inbound.departure_time == "2026-11-27T12:30"
    assert detailed.price_total == 631400
    assert "inbound_round_trip_total" not in detailed.raw
    assert detailed.raw["inbound_source"] == "outbound_selected_search"
    assert detailed.booking_url is not None and detailed.booking_url.startswith(USER_BOOKING_URL)
    query = parse_qs(urlparse(detailed.booking_url).query)
    assert query["tfs"] == [BOTH_SELECTED_TFS]  # byte-identical to Google's own booking URL
    assert query["hl"] == ["ko"]
    assert offer.inbound is None  # not mutated
    assert detailed.offer_key() == (
        "ICN-NRT-2026-11-20T13:55-YP735|NRT-ICN-2026-11-27T12:30-YP732|ECONOMY"
    )
    assert provider("http").get_booking_url(detailed) == detailed.booking_url


@respx.mock
def test_get_flight_details_records_a_moved_fare() -> None:
    details_router()
    offer = replace(cheapest_offer(), price_total=600000.0, price_per_person=300000.0)
    detailed = provider("http").get_flight_details(offer)
    assert detailed.inbound is not None
    assert detailed.raw["inbound_round_trip_total"] == 631400
    assert detailed.price_total == 600000.0  # the observation itself is not rewritten


@respx.mock
def test_get_flight_details_is_best_effort() -> None:
    respx.get(url__startswith=GOOGLE).mock(return_value=httpx.Response(429))
    offer = cheapest_offer()
    assert provider("http").get_flight_details(offer) is offer
    one_way = replace(offer, return_date=None)
    assert provider("http").get_flight_details(one_way) is one_way
    no_numbers = replace(offer, outbound=replace(offer.outbound, segments=[]), raw={})
    assert provider("http").get_flight_details(no_numbers) is no_numbers


def test_get_flight_details_uses_original_metro_search() -> None:
    offer = cheapest_offer()
    offer.raw["search"] = {
        "origin": "SEL",
        "destination": "TYO",
        "direct_only": True,
        "max_stops": 0,
    }
    query = search_query_for_offer(offer)
    assert (query.origin, query.destination) == ("SEL", "TYO")
    assert query.direct_only and query.max_stops == 0
    assert query.return_date == date(2026, 11, 27) and query.adults == 2
    offer.raw.pop("search")
    assert search_query_for_offer(offer).origin == "ICN"


def test_outbound_selection_from_segments_or_raw() -> None:
    offer = cheapest_offer()
    assert outbound_selection(offer) == (YP735,)
    no_segments = replace(offer.outbound, segments=[])
    dom_like = replace(offer, outbound=no_segments, raw={"selected": [YP735.to_list()]})
    assert outbound_selection(dom_like) == (YP735,)
    broken = replace(dom_like, raw={"selected": [["ICN"]]})
    assert outbound_selection(broken) is None


def test_pick_return_option_prefers_listed_total() -> None:
    leg = cheapest_offer().outbound
    options = [
        ReturnOption(700000, leg, "top"),
        ReturnOption(650000, leg, "other"),
        ReturnOption(680000, leg, "other"),
    ]
    assert pick_return_option(options, 680000).price == 680000
    assert pick_return_option(options, 1).price == 650000
    assert pick_return_option([], 1) is None


@respx.mock
def test_get_price_matches_outbound_and_exact_return() -> None:
    route = details_router()
    offer = cheapest_offer()
    gfp = provider("http")
    assert gfp.get_price(offer) == 315700
    detailed = gfp.get_flight_details(offer)
    assert round_trip_match_key(detailed) == offer.offer_key()
    assert gfp.get_price(detailed) == 315700
    gone = replace(offer, outbound=replace(offer.outbound, departure_time="2026-11-20T06:00"))
    assert gfp.get_price(gone) is None
    assert route.call_count == 5


def test_get_booking_url_falls_back_to_search_link() -> None:
    offer = replace(cheapest_offer(), booking_url=None, source_url=None)
    url = provider().get_booking_url(offer)
    assert url is not None and "/travel/flights/search?" in url and "hl=ko" in url


# --------------------------------------------------------------------------- live


LIVE_SPACER = RequestSpacer(10.0, 3.0)
"""Shared by the live tests: >= 10 s between the (at most 4) real Google requests."""
LIVE_RT = FlightQuery("ICN", "NRT", date(2026, 11, 20), date(2026, 11, 27), adults=1)


def _live_summary(label: str, offers: list[FlightOffer]) -> FlightOffer:
    assert offers, f"{label}: no offers"
    cheapest = min(offers, key=lambda offer: offer.price_total)
    print(  # noqa: T201 - live check report
        f"\n[live] {label}: {len(offers)} offers, cheapest KRW {cheapest.price_total:,.0f} "
        f"({cheapest.offer_key()}, source={cheapest.raw.get('source')})"
    )
    assert cheapest.currency == "KRW"
    assert cheapest.price_total > 10_000
    return cheapest


@pytest.mark.live
def test_live_browser_mode_icn_nrt(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.browser.manager import BrowserManager

    monkeypatch.setattr(gf, "REQUEST_SPACER", LIVE_SPACER)
    manager = BrowserManager(channel="msedge", headless=True)
    try:
        offers = provider("browser", browser=manager).search_flights(LIVE_RT)
    finally:
        manager.shutdown()
    _live_summary("browser ICN-NRT 1 adult", offers)


@pytest.mark.live
def test_live_http_mode_icn_nrt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gf, "REQUEST_SPACER", LIVE_SPACER)
    offers = provider("http").search_flights(LIVE_RT)
    _live_summary("http ICN-NRT 1 adult", offers)


@pytest.mark.live
def test_live_metro_codes_and_details(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gf, "REQUEST_SPACER", LIVE_SPACER)
    gfp = provider("http")
    offers = gfp.search_flights(replace(LIVE_RT, origin="SEL", destination="TYO"))
    cheapest = _live_summary("http SEL-TYO 1 adult", offers)
    pairs = sorted({(o.outbound.departure_airport, o.outbound.arrival_airport) for o in offers})
    print(f"[live] airport pairs: {pairs}")  # noqa: T201
    assert {pair[0] for pair in pairs} <= {"ICN", "GMP"}
    assert {pair[1] for pair in pairs} <= {"NRT", "HND"}
    detailed = gfp.get_flight_details(cheapest)
    print(  # noqa: T201
        f"[live] details: inbound={detailed.inbound and detailed.inbound.flight_numbers} "
        f"booking_url={detailed.booking_url}"
    )
    assert detailed.inbound is not None
