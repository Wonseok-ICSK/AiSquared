"""Naver Flights provider: URL building, capture signals, spacing, caching and pausing.

The browser is faked (``BrowserManager.run`` and ``capture_search`` are replaced); the real
page-side capture is covered by ``test_naver_capture.py`` and the live test.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from app.browser.manager import OFFSCREEN_ARGS, BrowserUnavailableError
from app.providers.flights import default_provider_keys, get_provider_class
from app.providers.flights import naver
from app.providers.flights.base import (
    FlightQuery,
    ProviderBlocked,
    ProviderContext,
    ProviderError,
    ProviderNotConfigured,
)

FIXTURE = Path(__file__).parent / "fixtures" / "naver" / "final_event_icn_nrt_2adults.json"
QUERY = FlightQuery("ICN", "NRT", date(2026, 12, 3), date(2026, 12, 7), adults=2)


class Clock:
    """Fake monotonic clock + sleep that advances it."""

    def __init__(self) -> None:
        self.now = 1_000.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class FakeManager:
    """Stands in for ``BrowserManager``: runs the job inline with a dummy handle."""

    def __init__(self, error: BaseException | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.error = error

    def run(
        self,
        fn: Callable[[Any], Any],
        timeout: float,
        label: str = "",
        *,
        queue_timeout: float | None = None,
    ) -> Any:
        self.calls.append({"timeout": timeout, "label": label, "queue_timeout": queue_timeout})
        if self.error is not None:
            raise self.error
        return fn("HANDLE")


@pytest.fixture(autouse=True)
def clean_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[Clock]:
    clock = Clock()
    naver.reset_state()
    monkeypatch.setattr(naver, "_monotonic", clock.monotonic)
    monkeypatch.setattr(naver, "_sleep", clock.sleep)
    yield clock
    naver.reset_state()


def search_key_for(url: str) -> str:
    """The ``status.searchKey`` Naver reports for a search URL built by the provider."""
    path = url.split(naver.SEARCH_PATH, 1)[1].split("?", 1)[0]
    legs = []
    for leg in path.split("/"):
        origin, destination, day = leg.split("-")
        legs.append(
            f"{day}{origin[:3]}{origin[4].upper()}{destination[:3]}{destination[4].upper()}"
        )
    return f"{'RT' if len(legs) == 2 else 'OW'}Y100PN" + "".join(legs)


def fresh_event(minutes: float = 10.0, url: str | None = None) -> dict[str, Any]:
    event = json.loads(FIXTURE.read_text(encoding="utf-8"))
    expires = datetime.now(UTC) + timedelta(minutes=minutes)
    event["status"]["expireAt"] = expires.isoformat().replace("+00:00", "Z")
    if url is not None:
        event["status"]["searchKey"] = search_key_for(url)
    return event


@pytest.fixture
def captures(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Replace ``capture_search``; each call is recorded and returns the fixture event."""
    calls: list[dict[str, Any]] = []

    def fake_capture(
        handle: Any, url: str, *, show_window: bool = False, want_partners: bool = True
    ) -> naver.CaptureResult:
        call = {"handle": handle, "url": url, "show_window": show_window}
        calls.append({**call, "want_partners": want_partners})
        partners = {"BDC219": "Example Partner", "WHY002": "와이페이모어"} if want_partners else None
        return naver.CaptureResult(event=fresh_event(url=url), partners=partners, elapsed_s=12.0)

    monkeypatch.setattr(naver, "capture_search", fake_capture)
    return calls


def provider(browser: Any = None, **options: Any) -> naver.NaverFlightsProvider:
    return naver.NaverFlightsProvider(
        ProviderContext(get_secret=lambda _name: None, browser=browser, options=options)
    )


# --------------------------------------------------------------------------- metadata


def test_registered_as_opt_in_browser_provider() -> None:
    cls = get_provider_class("naver")
    assert cls is naver.NaverFlightsProvider
    assert cls.display_name == "Naver Flights (opt-in)"
    assert cls.enabled_by_default is False and "naver" not in default_provider_keys()
    assert cls.uses_browser and cls.supports_metro_codes and not cls.requires_api_key
    assert cls.max_queries_per_run == 4
    assert "terms of service forbid automated collection" in cls.description
    assert "own risk" in cls.description and "personal" in cls.description.lower()


def test_capture_script_matches_binding_and_api() -> None:
    script = naver.capture_script()
    assert f'"{naver.BINDING_NAME}"' in script
    assert naver.SEARCH_API_MARKER in script


# --------------------------------------------------------------------------- URL


def test_round_trip_url_with_metro_codes() -> None:
    query = FlightQuery("SEL", "TYO", date(2026, 11, 20), date(2026, 11, 27))
    assert naver.build_search_url(query) == (
        "https://flight.naver.com/flights/international/"
        "SEL:city-TYO:city-20261120/TYO:city-SEL:city-20261127"
        "?adult=1&isDirect=false&fareType=Y"
    )


def test_one_way_url_with_airports_cabin_and_direct() -> None:
    query = FlightQuery(
        "icn", "NRT", date(2026, 12, 10), None, adults=2, cabin="business", direct_only=True
    )
    assert naver.build_search_url(query) == (
        "https://flight.naver.com/flights/international/ICN:airport-NRT:airport-20261210"
        "?adult=2&isDirect=true&fareType=C"
    )


@pytest.mark.parametrize(
    ("cabin", "param"),
    [("economy", "Y"), ("premium_economy", "P"), ("business", "C"), ("first", "F")],
)
def test_fare_type_param(cabin: str, param: str) -> None:
    query = FlightQuery("ICN", "HND", date(2026, 12, 1), None, cabin=cabin)
    assert naver.build_search_url(query).endswith(f"fareType={param}")


def test_route_tokens() -> None:
    assert naver.route_token("SEL") == "SEL:city"
    assert naver.route_token("osa") == "OSA:city"
    assert naver.route_token("FUK") == "FUK:airport"  # single-airport city
    assert naver.route_token("GMP") == "GMP:airport"
    assert naver.route_token("ZZZ") == "ZZZ:airport"


# --------------------------------------------------------------------------- signals


def test_classify_http() -> None:
    assert naver.classify_http(201, "text/event-stream") is None
    assert naver.classify_http(200, "text/event-stream; charset=utf-8") is None
    for status in (403, 429, 503):
        failure = naver.classify_http(status, "text/html")
        assert failure is not None and failure.blocked and f"HTTP {status}" in failure.message
    html = naver.classify_http(200, "text/html; charset=utf-8")
    assert html is not None and html.blocked and "text/html" in html.message
    server_error = naver.classify_http(500, "application/json")
    assert server_error is not None and not server_error.blocked


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def failure_of(state: naver.CaptureState) -> naver.CaptureFailure:
    outcome = state.outcome()
    assert isinstance(outcome, naver.CaptureFailure), outcome
    return outcome


def test_capture_state_known_http_status_wins() -> None:
    state = naver.CaptureState()
    state.on_request_failed("net::ERR_FAILED", had_response=False)
    state.on_http(503, "text/html")  # CDP read the status CORS hid from the page
    assert failure_of(state) == naver.CaptureFailure(True, "Naver refused the search (HTTP 503)")
    state.on_http(500, "application/json")  # a later, weaker failure does not replace it
    assert "HTTP 503" in failure_of(state).message
    state.on_message({"kind": "final", "data": "{}"})
    assert state.outcome() == "{}"


def test_capture_state_cors_less_refusal_is_blocked_at_once() -> None:
    """A CORS-less 403/429/503 (or refused preflight) reaches Playwright only as ERR_FAILED."""
    state = naver.CaptureState(clock=FakeClock())
    state.on_message({"kind": "error", "reason": "network", "message": "Failed to fetch"})
    state.on_request_failed("net::ERR_FAILED", had_response=False)
    failure = failure_of(state)  # no grace: the page retries every second
    assert failure.blocked and "ERR_FAILED" in failure.message


def test_capture_state_ignores_aborted_requests() -> None:
    state = naver.CaptureState()
    state.on_request_failed("net::ERR_ABORTED", had_response=False)
    assert state.outcome() is None


@pytest.mark.parametrize(
    "failure",
    ["net::ERR_INTERNET_DISCONNECTED", "net::ERR_NAME_NOT_RESOLVED", "net::ERR_CERT_DATE_INVALID"],
)
def test_capture_state_local_network_errors_are_plain_after_grace(failure: str) -> None:
    clock = FakeClock()
    state = naver.CaptureState(clock=clock)
    state.on_message({"kind": "error", "reason": "network", "message": "Failed to fetch"})
    state.on_request_failed(failure, had_response=False)
    clock.now = naver.NETWORK_ERROR_GRACE_S - 0.1
    assert state.outcome() is None  # the page's own retry may still succeed
    clock.now = naver.NETWORK_ERROR_GRACE_S
    result = failure_of(state)
    assert not result.blocked and failure in result.message


def test_capture_state_broken_stream_is_not_a_refusal() -> None:
    clock = FakeClock()
    state = naver.CaptureState(clock=clock)
    state.on_http(201, "text/event-stream")
    state.on_request_failed("net::ERR_HTTP2_PROTOCOL_ERROR", had_response=True)
    state.on_message({"kind": "error", "reason": "stream", "message": "network error"})
    clock.now = naver.NETWORK_ERROR_GRACE_S
    result = failure_of(state)
    assert not result.blocked and "ERR_HTTP2_PROTOCOL_ERROR" in result.message


def test_capture_state_bare_fetch_failure_counts_as_refusal_before_any_stream() -> None:
    clock = FakeClock()
    state = naver.CaptureState(clock=clock)
    state.on_message({"kind": "error", "reason": "network", "message": "Failed to fetch"})
    assert state.outcome() is None  # waiting for the browser's reason
    clock.now = naver.FETCH_DETAIL_GRACE_S
    assert failure_of(state).blocked
    assert naver.FETCH_DETAIL_GRACE_S < 1.0  # decided before the page's 1-s retry


def test_capture_state_bare_fetch_failure_after_a_healthy_stream_is_plain() -> None:
    clock = FakeClock()
    state = naver.CaptureState(clock=clock)
    state.on_http(201, "text/event-stream")
    clock.now = 5.0
    state.on_message({"kind": "error", "reason": "network", "message": "Failed to fetch"})
    clock.now = 6.0
    assert not failure_of(state).blocked


def test_capture_state_healthy_retry_clears_a_transient_network_error() -> None:
    clock = FakeClock()
    state = naver.CaptureState(clock=clock)
    state.on_request_failed("net::ERR_NETWORK_CHANGED", had_response=False)
    clock.now = 1.0
    state.on_http(201, "text/event-stream")  # the page's own retry opened a healthy stream
    clock.now = 30.0
    assert state.outcome() is None  # FINAL_EVENT_TIMEOUT_S governs the new stream


def test_capture_state_incomplete_stream_waits_for_a_research() -> None:
    clock = FakeClock()
    state = naver.CaptureState(clock=clock)
    state.on_message({"kind": "error", "reason": "incomplete", "completed": 7, "requested": 20})
    assert state.outcome() is None
    clock.now = 1.0
    state.on_http(201, "text/event-stream")  # the re-search started
    clock.now = 20.0  # cold searches take 10-17 s
    assert state.outcome() is None and state.streams_ok == 1
    state.on_message({"kind": "error", "reason": "incomplete", "completed": 9, "requested": 20})
    clock.now += naver.INCOMPLETE_GRACE_S
    failure = failure_of(state)
    assert not failure.blocked and "9/20 partners" in failure.message


def test_capture_state_expired_result_without_research() -> None:
    clock = FakeClock()
    state = naver.CaptureState(clock=clock)
    state.on_message(
        {"kind": "error", "reason": "incomplete", "completed": 20, "requested": 20, "expired": True}
    )
    clock.now = naver.INCOMPLETE_GRACE_S
    assert "expired" in failure_of(state).message


# --------------------------------------------------------------------------- CDP statuses

API_URL = "https://flight-api.naver.com/flight/international/searchFlights"


def cdp_request(request_id: str, method: str, url: str = API_URL) -> dict[str, Any]:
    return {"requestId": request_id, "request": {"url": url, "method": method}}


def cdp_response(request_id: str, status: int, content_type: str = "text/html") -> dict[str, Any]:
    return {
        "requestId": request_id,
        "statusCode": status,
        "headers": {"Content-Type": content_type},
    }


@pytest.mark.parametrize(("method", "status"), [("POST", 503), ("POST", 429), ("OPTIONS", 503)])
def test_status_watcher_reports_cors_hidden_refusals(method: str, status: int) -> None:
    state = naver.CaptureState()
    watcher = naver.SearchStatusWatcher(state)
    watcher.on_request(cdp_request("1", method))
    watcher.on_response(cdp_response("1", status))
    assert failure_of(state) == naver.CaptureFailure(
        True, f"Naver refused the search (HTTP {status})"
    )


def test_status_watcher_handles_early_extra_info() -> None:
    state = naver.CaptureState()
    watcher = naver.SearchStatusWatcher(state)
    watcher.on_response(cdp_response("7", 429))  # ExtraInfo may precede requestWillBeSent
    assert state.outcome() is None
    watcher.on_request(cdp_request("7", "POST"))
    assert failure_of(state).blocked


def test_status_watcher_ignores_healthy_redirects_and_other_requests() -> None:
    state = naver.CaptureState()
    watcher = naver.SearchStatusWatcher(state)
    watcher.on_request(cdp_request("1", "OPTIONS"))
    watcher.on_response(cdp_response("1", 204))  # healthy preflight
    watcher.on_request(cdp_request("2", "POST"))
    watcher.on_response(cdp_response("2", 201, "text/event-stream"))
    watcher.on_request(cdp_request("3", "POST"))
    watcher.on_response(cdp_response("3", 302))
    watcher.on_request(cdp_request("4", "POST", "https://flight-api.naver.com/graphql"))
    watcher.on_response(cdp_response("4", 503))
    watcher.on_response(cdp_response("unknown", 503))
    watcher.on_request({"requestId": "5"})  # junk
    assert state.outcome() is None
    assert state.streams_ok == 0  # Playwright reports healthy streams; no double counting


def test_status_watcher_non_stream_post_reply_and_plain_errors() -> None:
    state = naver.CaptureState()
    watcher = naver.SearchStatusWatcher(state)
    watcher.on_request(cdp_request("1", "POST"))
    watcher.on_response(cdp_response("1", 500, "application/json"))
    expected = naver.CaptureFailure(False, "Naver search request failed (HTTP 500)")
    assert failure_of(state) == expected
    other = naver.CaptureState()
    other_watcher = naver.SearchStatusWatcher(other)
    other_watcher.on_request(cdp_request("1", "POST"))
    other_watcher.on_response(cdp_response("1", 200, "application/json"))
    assert failure_of(other).blocked


def test_local_network_error_classification() -> None:
    assert naver.is_local_network_error("net::ERR_INTERNET_DISCONNECTED")
    assert naver.is_local_network_error("net::ERR_PROXY_CONNECTION_FAILED")
    assert not naver.is_local_network_error("net::ERR_FAILED")
    assert not naver.is_local_network_error("net::ERR_CONNECTION_RESET")


# --------------------------------------------------------------------------- page watcher


class FakeFrame:
    pass


class FakeRequest:
    def __init__(self, frame: Any, url: str = API_URL, method: str = "POST") -> None:
        self.url, self.method, self.frame = url, method, frame
        self.failure: str | None = "net::ERR_FAILED"


class FakeResponse:
    def __init__(self, request: FakeRequest, status: int, content_type: str) -> None:
        self.request, self.status = request, status
        self.headers = {"content-type": content_type}


class WatchedPage:
    def __init__(self) -> None:
        self.main_frame = FakeFrame()


def test_page_watcher_accepts_top_frame_messages_only() -> None:
    page, state = WatchedPage(), naver.CaptureState()
    watcher = naver.PageWatcher(page, state)  # type: ignore[arg-type]
    iframe = FakeFrame()
    fake = {"kind": "final", "data": '{"status": {"isCompleted": true}}'}
    watcher.on_binding({"frame": iframe}, fake)
    watcher.on_binding({"frame": iframe}, {"kind": "error", "reason": "http", "status": 503})
    assert state.outcome() is None and watcher.ignored_calls == 2
    watcher.on_binding({"frame": page.main_frame}, {"kind": "final", "data": "{}"})
    assert state.outcome() == "{}"


def test_page_watcher_network_events_from_the_top_frame_only() -> None:
    page, state = WatchedPage(), naver.CaptureState()
    watcher = naver.PageWatcher(page, state)  # type: ignore[arg-type]
    iframe_reply = FakeResponse(FakeRequest(FakeFrame()), 503, "text/html")
    watcher.on_response(iframe_reply)  # type: ignore[arg-type]
    watcher.on_request_failed(FakeRequest(FakeFrame()))  # type: ignore[arg-type]
    assert state.outcome() is None
    healthy = FakeRequest(page.main_frame)
    watcher.on_response(FakeResponse(healthy, 201, "text/event-stream"))  # type: ignore[arg-type]
    healthy.failure = "net::ERR_FAILED"
    watcher.on_request_failed(healthy)  # type: ignore[arg-type]
    assert state.streams_ok == 1 and state.refusal is None  # a stream that broke mid-way
    watcher.on_request_failed(FakeRequest(page.main_frame))  # type: ignore[arg-type]
    assert failure_of(state).blocked


def test_capture_state_http_signal_from_script_and_junk() -> None:
    state = naver.CaptureState()
    state.on_message("junk")
    state.on_message({"kind": "final"})  # no data
    assert state.outcome() is None
    state.on_message({"kind": "error", "reason": "http", "status": 429, "contentType": "text/html"})
    failure = state.outcome()
    assert isinstance(failure, naver.CaptureFailure) and failure.blocked


def test_decode_event() -> None:
    assert naver.decode_event('{"status": {}}') == {"status": {}}
    with pytest.raises(ProviderError):
        naver.decode_event("not json")
    with pytest.raises(ProviderError):
        naver.decode_event("[1, 2]")


def test_search_key_legs_and_expected_legs() -> None:
    assert naver.search_key_legs("RTY100PN20261120SELCTYOC20261127TYOCSELC") == [
        ("20261120", "SEL", "TYO"),
        ("20261127", "TYO", "SEL"),
    ]
    assert naver.search_key_legs(None) == [] and naver.search_key_legs("v2:abc") == []
    one_way = FlightQuery(" icn ", "nrt", date(2026, 12, 10), None)
    assert naver.expected_legs(one_way) == [("20261210", "ICN", "NRT")]


def test_validate_event() -> None:
    event = fresh_event()  # the fixture: ICN-NRT 2026-12-03 / 12-07
    naver.validate_event(event, QUERY)
    with pytest.raises(ProviderError, match="another search"):
        naver.validate_event(event, FlightQuery("ICN", "NRT", date(2026, 12, 4), date(2026, 12, 7)))
    with pytest.raises(ProviderError, match="another search"):
        naver.validate_event(event, FlightQuery("ICN", "KIX", date(2026, 12, 3), None))
    expired = {**event, "isExpired": True}
    with pytest.raises(ProviderError, match="expired"):
        naver.validate_event(expired, QUERY)
    unknown = {**event, "status": {**event["status"], "searchKey": "format-2027"}}
    naver.validate_event(unknown, FlightQuery("SEL", "OSA", date(2027, 1, 1), None))


def test_result_ttl() -> None:
    now = datetime(2026, 10, 6, 8, 0, tzinfo=UTC)
    assert naver.result_ttl_s({"status": {"expireAt": "2026-10-06T08:05:00.000Z"}}, now) == 300
    assert naver.result_ttl_s({"status": {"expireAt": "2026-10-06T09:00:00Z"}}, now) == 900
    assert naver.result_ttl_s({"status": {"expireAt": "2026-10-06T07:00:00Z"}}, now) == 0
    assert naver.result_ttl_s({"status": {}}, now) == naver.RESULT_CACHE_TTL_S


# --------------------------------------------------------------------------- browser job


class FakeCdpSession:
    def __init__(self, error: Exception | None = None) -> None:
        self.events: list[str] = []
        self.sent: list[tuple[str, Any]] = []
        self.error = error

    def on(self, event: str, _handler: Any) -> None:
        self.events.append(event)

    def send(self, method: str, params: Any = None) -> None:
        if self.error is not None:
            raise self.error
        self.sent.append((method, params))


class FakePage:
    def __init__(self, cdp: FakeCdpSession | None = None) -> None:
        self.bindings: list[str] = []
        self.scripts: list[str] = []
        self.events: list[str] = []
        self.main_frame = object()
        self.cdp = cdp or FakeCdpSession()
        self.context = self

    def new_cdp_session(self, page: Any) -> FakeCdpSession:
        assert page is self
        return self.cdp

    def expose_binding(self, name: str, _callback: Any) -> None:
        self.bindings.append(name)

    def add_init_script(self, script: str) -> None:
        self.scripts.append(script)

    def on(self, event: str, _handler: Any) -> None:
        self.events.append(event)


class FakeContext:
    def __init__(self) -> None:
        self.page = FakePage()

    def new_page(self) -> FakePage:
        return self.page


class FakeHandle:
    def __init__(self) -> None:
        self.launches: list[dict[str, Any]] = []
        self.context = FakeContext()

    def launch(self, headless: bool | None = None, args: list[str] | None = None) -> str:
        self.launches.append({"headless": headless, "args": args})
        return "BROWSER"

    def new_context(self, browser: Any = None, **_kwargs: Any) -> FakeContext:
        assert browser == "BROWSER"
        return self.context


@pytest.mark.parametrize("show_window", [False, True])
def test_capture_search_launches_headed_edge(
    monkeypatch: pytest.MonkeyPatch, show_window: bool
) -> None:
    monkeypatch.setattr(naver, "await_final_event", lambda *_args: '{"status": {}}')
    monkeypatch.setattr(naver, "fetch_partner_names", lambda page: {"WHY002": "와이페이모어"})
    handle = FakeHandle()
    result = naver.capture_search(
        handle, "https://example.test/", show_window=show_window  # type: ignore[arg-type]
    )
    expected_args = [] if show_window else list(OFFSCREEN_ARGS)
    assert handle.launches == [{"headless": False, "args": expected_args}]  # never headless
    page = handle.context.page
    assert page.bindings == [naver.BINDING_NAME]
    assert page.scripts == [naver.capture_script()]
    assert set(page.events) == {"dialog", "response", "requestfailed"}
    assert set(page.cdp.events) == {
        "Network.requestWillBeSent",
        "Network.responseReceivedExtraInfo",
    }
    assert [method for method, _params in page.cdp.sent] == ["Network.enable"]
    assert result.event == {"status": {}} and result.partners == {"WHY002": "와이페이모어"}


def test_status_watcher_is_optional() -> None:
    from playwright.sync_api import Error as PlaywrightError

    page: Any = FakePage(FakeCdpSession(error=PlaywrightError("CDP session is only available")))
    assert naver.attach_status_watcher(page, naver.CaptureState()) is False
    working: Any = FakePage()
    assert naver.attach_status_watcher(working, naver.CaptureState()) is True


def test_capture_search_can_skip_partner_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(naver, "await_final_event", lambda *_args: "{}")
    monkeypatch.setattr(naver, "fetch_partner_names", lambda _page: pytest.fail("not called"))
    result = naver.capture_search(FakeHandle(), "u", want_partners=False)  # type: ignore[arg-type]
    assert result.partners is None


# --------------------------------------------------------------------------- provider


def test_status() -> None:
    assert provider().status().configured is False
    status = provider(FakeManager()).status()
    assert status.configured is True and "terms of service" in status.message


def test_search_returns_parsed_offers_with_live_partner_names(
    captures: list[dict[str, Any]],
) -> None:
    manager = FakeManager()
    offers = provider(manager).search_flights(QUERY)
    assert len(offers) == 6 and offers[0].price_per_person == 284020.0
    assert offers[0].seller == "Example Partner"
    assert offers[0].raw["outside_korean_ota_list"] is False  # live list = the given partners
    assert offers[0].booking_url == naver.build_search_url(QUERY)
    assert manager.calls == [
        {"timeout": naver.JOB_TIMEOUT_S, "label": "naver", "queue_timeout": naver.QUEUE_TIMEOUT_S}
    ]
    assert captures == [
        {
            "handle": "HANDLE",
            "url": naver.build_search_url(QUERY),
            "show_window": False,
            "want_partners": True,
        }
    ]
    assert naver.cached_partner_names() == {"BDC219": "Example Partner", "WHY002": "와이페이모어"}


def test_repeated_search_is_served_from_the_cache(captures: list[dict[str, Any]]) -> None:
    manager = FakeManager()
    first = provider(manager).search_flights(QUERY)
    first[0].price_per_person = 1.0  # callers may mutate their copies
    second = provider(manager).search_flights(QUERY)
    assert len(captures) == 1 and len(manager.calls) == 1
    assert second[0].price_per_person == 284020.0


def test_cache_expires(captures: list[dict[str, Any]], clean_state: Clock) -> None:
    manager = FakeManager()
    provider(manager).search_flights(QUERY)
    clean_state.now += naver.RESULT_CACHE_TTL_S + 1
    provider(manager).search_flights(QUERY)
    assert len(captures) == 2


def test_searches_are_spaced_15_to_30_seconds(
    captures: list[dict[str, Any]], clean_state: Clock
) -> None:
    manager = FakeManager()
    provider(manager).search_flights(QUERY)
    assert clean_state.sleeps == []  # first search starts at once
    other = FlightQuery("SEL", "TYO", date(2026, 11, 20), date(2026, 11, 27))
    provider(manager).search_flights(other)
    assert len(clean_state.sleeps) == 1 and 15.0 <= clean_state.sleeps[0] <= 30.0
    assert captures[1]["want_partners"] is False  # partner names cached by the first search
    clean_state.now += 60
    provider(manager).search_flights(FlightQuery("SEL", "OSA", date(2026, 11, 20), None))
    assert len(clean_state.sleeps) == 1  # enough time has passed already


def test_show_window_option(captures: list[dict[str, Any]]) -> None:
    provider(FakeManager(), show_window="true").search_flights(QUERY)
    assert captures[0]["show_window"] is True


def test_refusal_pauses_the_provider(monkeypatch: pytest.MonkeyPatch, clean_state: Clock) -> None:
    def refused(*_args: Any, **_kwargs: Any) -> naver.CaptureResult:
        raise ProviderBlocked("Naver refused the search (HTTP 503)")

    monkeypatch.setattr(naver, "capture_search", refused)
    manager = FakeManager()
    with pytest.raises(ProviderBlocked, match="HTTP 503"):
        provider(manager).search_flights(QUERY)
    pause = naver.active_block_pause()
    assert pause is not None and "HTTP 503" in pause.reason
    assert pause.until - datetime.now(UTC) > timedelta(hours=23)
    status = provider(manager).status()
    assert status.configured is False and "paused until" in status.message
    with pytest.raises(ProviderBlocked, match="paused until"):
        provider(manager).search_flights(FlightQuery("SEL", "TYO", date(2026, 11, 20), None))
    assert len(manager.calls) == 1  # no browser job while paused
    assert naver.active_block_pause(datetime.now(UTC) + timedelta(hours=25)) is None


def test_plain_failures_do_not_pause(monkeypatch: pytest.MonkeyPatch) -> None:
    def failing(*_args: Any, **_kwargs: Any) -> naver.CaptureResult:
        raise ProviderError("Naver search request failed (HTTP 500)")

    monkeypatch.setattr(naver, "capture_search", failing)
    with pytest.raises(ProviderError, match="HTTP 500"):
        provider(FakeManager()).search_flights(QUERY)
    assert naver.active_block_pause() is None


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError("browser job 'naver' timed out after 150s"),
        BrowserUnavailableError("No browser could be launched"),
    ],
)
def test_browser_failures_become_provider_errors(error: BaseException) -> None:
    with pytest.raises(ProviderError) as info:
        provider(FakeManager(error=error)).search_flights(QUERY)
    assert not isinstance(info.value, ProviderBlocked)
    assert naver.active_block_pause() is None


def test_no_browser_is_not_configured() -> None:
    with pytest.raises(ProviderNotConfigured):
        provider(None).search_flights(QUERY)


def test_parse_options_reuse_one_search(captures: list[dict[str, Any]]) -> None:
    """The cache holds the captured event per URL; parse options apply per call."""
    manager = FakeManager()
    everyone = provider(manager).search_flights(QUERY)
    listed_only = provider(manager, korean_ota_only=True).search_flights(QUERY)
    nonstop_ok = FlightQuery("ICN", "NRT", date(2026, 12, 3), date(2026, 12, 7), 2, max_stops=0)
    direct_only = provider(manager).search_flights(nonstop_ok)
    assert len(captures) == 1 and len(manager.calls) == 1  # one Naver search for one URL
    assert any(o.raw["outside_korean_ota_list"] for o in everyone)
    assert not any(o.raw["outside_korean_ota_list"] for o in listed_only)
    assert len(direct_only) == 6  # the fixture's itineraries are all nonstop


def test_concurrent_checks_share_one_search(
    captures: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two trips asking for the same URL at once cause one Naver search, even if parsing
    is slow (the result is cached before the search lock is released)."""
    original = naver.parse_final_event

    def slow_parse(*args: Any, **kwargs: Any) -> Any:
        time.sleep(0.2)
        return original(*args, **kwargs)

    monkeypatch.setattr(naver, "parse_final_event", slow_parse)
    manager = FakeManager()
    results: list[int] = []

    def check() -> None:
        results.append(len(provider(manager).search_flights(QUERY)))

    threads = [threading.Thread(target=check) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert results == [6, 6]
    assert len(captures) == 1


def test_mismatched_or_expired_results_are_rejected_and_not_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def wrong_route(_handle: Any, url: str, **_kwargs: Any) -> naver.CaptureResult:
        calls.append(url)
        other = naver.build_search_url(FlightQuery("ICN", "KIX", date(2026, 12, 3), None))
        return naver.CaptureResult(event=fresh_event(url=other), partners=None, elapsed_s=1.0)

    monkeypatch.setattr(naver, "capture_search", wrong_route)
    manager = FakeManager()
    with pytest.raises(ProviderError, match="another search"):
        provider(manager).search_flights(QUERY)

    def expired(_handle: Any, url: str, **_kwargs: Any) -> naver.CaptureResult:
        calls.append(url)
        event = {**fresh_event(url=url), "isExpired": True}
        return naver.CaptureResult(event=event, partners=None, elapsed_s=1.0)

    monkeypatch.setattr(naver, "capture_search", expired)
    with pytest.raises(ProviderError, match="expired"):
        provider(manager).search_flights(QUERY)
    assert len(calls) == 2 and naver.cached_result(naver.build_search_url(QUERY)) is None
    assert naver.active_block_pause() is None


class EvaluatingPage:
    def __init__(self, result: Any = None, error: Exception | None = None) -> None:
        self.result, self.error = result, error
        self.args: list[Any] = []

    def evaluate(self, _script: str, arg: Any) -> Any:
        self.args.append(arg)
        if self.error is not None:
            raise self.error
        return self.result


def test_fetch_partner_names() -> None:
    from playwright.sync_api import Error as PlaywrightError

    page = EvaluatingPage({"partners": [{"otaId": "WHY002", "expName": "와이페이모어"}]})
    assert naver.fetch_partner_names(page) == {"WHY002": "와이페이모어"}  # type: ignore[arg-type]
    assert page.args == [
        [naver.GRAPHQL_URL, naver.BOOKING_PARTNERS_QUERY, naver.PARTNER_FETCH_TIMEOUT_MS]
    ]
    for result in ({"error": "HTTP 503"}, ["unexpected"], {"partners": []}):
        assert naver.fetch_partner_names(EvaluatingPage(result)) is None  # type: ignore[arg-type]
    failing = EvaluatingPage(error=PlaywrightError("Target closed"))
    assert naver.fetch_partner_names(failing) is None  # type: ignore[arg-type]
