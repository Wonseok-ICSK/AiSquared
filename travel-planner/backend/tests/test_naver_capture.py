"""``naver_capture.js`` + the page-side wait loop, against a local fake of Naver's SSE API.

Runs real *headless* Edge on ``127.0.0.1`` only (no internet; the HeadlessChrome user agent
only matters to Naver itself). Skipped when Edge is not installed.

Two local origins mimic ``flight.naver.com`` (the page) and ``flight-api.naver.com`` (the
API): the real API is cross-origin, so the page's ``searchFlights`` call needs a CORS
preflight, and a refusal without CORS headers reaches the page only as "Failed to fetch".
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import Counter
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import parse_qs, urlparse

import pytest
from playwright.sync_api import Browser, Page, sync_playwright

from app.providers.flights import naver
from app.providers.flights.base import ProviderBlocked, ProviderError

EDGE_INSTALLED = any(
    (Path(os.environ.get(var, default)) / "Microsoft/Edge/Application/msedge.exe").exists()
    for var, default in (
        ("ProgramFiles(x86)", r"C:\Program Files (x86)"),
        ("ProgramFiles", r"C:\Program Files"),
    )
)
pytestmark = pytest.mark.skipif(not EDGE_INSTALLED, reason="Microsoft Edge is not installed")

API_PATH = "/flight/international/searchFlights"


def snapshot(done: int, total: int = 3, pad: int = 0, expired: bool = False) -> str:
    """One cumulative snapshot as the API sends it (``pad`` inflates it like real payloads)."""
    event: dict[str, Any] = {
        "uniqueId": "test",
        "status": {
            "searchKey": "RTY100PN",
            "requestedPartnerCount": total,
            "completedPartnerCount": done,
            "isCompleted": done == total,
        },
        "itineraries": [],
        "fareMappings": [],
        "isExpired": expired,
        "popularFlights": [{"pad": "x" * pad}] if pad else [],
    }
    return json.dumps(event, separators=(",", ":"))


def sse_body(events: list[str], newline: str = "\n") -> bytes:
    return "".join(f"data:{event}{newline}{newline}" for event in events).encode("utf-8")


SEARCH_JS = """
function search(url, then) {
  return fetch(url, {
    method: 'POST', body: '{}', credentials: 'include',
    headers: {'Content-Type': 'application/json', 'Accept': 'text/event-stream'}
  }).then(r => r.text()).then(t => { window.__pageRead = t.length; if (then) then(); });
}
"""


class FakeNaver(BaseHTTPRequestHandler):
    """``GET /`` page that fires the search; ``POST`` API whose behaviour ``?mode=`` selects.

    ``xo_*`` modes call :class:`FakeNaverApi` on a second origin and, like Naver's page,
    retry every second after a failed fetch.
    """

    api_origin: ClassVar[str] = ""

    def log_message(self, *_args: Any) -> None:  # keep pytest output clean
        return

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        mode = parse_qs(urlparse(self.path).query).get("mode", ["ok"])[0]
        self._send(200, "text/html; charset=utf-8", self._page(mode).encode("utf-8"))

    def _page(self, mode: str) -> str:
        if mode == "none":
            body = ""
        elif mode.startswith("xo_"):
            url = f"{self.api_origin}{API_PATH}?mode={mode}"
            body = f"(function go() {{ search('{url}').catch(() => setTimeout(go, 1000)); }})();"
        elif mode == "iframe":  # an ad frame on another origin tries to feed the binding
            body = f"""
              const frame = document.createElement('iframe');
              frame.src = '{self.api_origin}/ad';
              frame.onload = () => setTimeout(() => search('{API_PATH}?mode=ok'), 300);
              document.body.appendChild(frame);"""
        elif mode == "expired":  # like Naver's client: search again after an expired result
            body = f"search('{API_PATH}?mode=expired', () => search('{API_PATH}?mode=ok'));"
        else:
            body = f"search('{API_PATH}?mode={mode}').catch(e => {{ window.__pageError = e; }});"
        return f"""<!doctype html><html><body><script>{SEARCH_JS}
          fetch('/other/api', {{method: 'POST', body: '{{}}'}}).catch(() => {{}});
          {body}
        </script></body></html>"""

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("content-length") or 0)
        self.rfile.read(length)
        mode = parse_qs(urlparse(self.path).query).get("mode", ["ok"])[0]
        if not self.path.startswith(API_PATH):
            self._send(200, "application/json", b"{}")
        elif mode == "ok":
            self._stream([snapshot(1), snapshot(2), snapshot(3)], "\n")
        elif mode == "crlf":
            self._stream([snapshot(1), snapshot(3)], "\r\n")
        elif mode == "big":
            self._stream([snapshot(1, pad=1_500_000), snapshot(3, pad=2_500_000)], "\n")
        elif mode == "incomplete":
            self._stream([snapshot(1), snapshot(2)], "\n")
        elif mode in ("expired", "expired_only"):
            self._stream([snapshot(1), snapshot(3, expired=True)], "\n")
        elif mode == "503":
            self._send(503, "text/html", b"<html>Service Unavailable</html>")
        elif mode == "500":
            self._send(500, "application/json", b'{"error":"boom"}')
        elif mode == "json":
            self._send(200, "application/json", b'{"message":"no stream"}')
        else:
            self._send(404, "text/plain", b"unknown mode")

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _stream(
        self, events: list[str], newline: str, headers: dict[str, str] | None = None
    ) -> None:
        body = sse_body(events, newline)
        self.send_response(201)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        step = 65_536  # flush in chunks so events straddle read() boundaries
        for start in range(0, len(body), step):
            self.wfile.write(body[start : start + step])
            self.wfile.flush()


class FakeNaverApi(FakeNaver):
    """The API on a second origin. Refusals carry no CORS headers, like an nginx error page."""

    hits: ClassVar[Counter[tuple[str, str]]] = Counter()

    def do_GET(self) -> None:  # noqa: N802 - the ad iframe
        fake = json.dumps(snapshot(3).replace("RTY100PN", "FAKE"))
        script = f"""
          window.__aisqNaverSse({{kind: 'final', data: {fake}}});
          window.__aisqNaverSse({{kind: 'error', reason: 'http', status: 503, contentType: ''}});"""
        self._send(200, "text/html", f"<script>{script}</script>".encode())

    def do_OPTIONS(self) -> None:  # noqa: N802
        mode = self._mode()
        self.hits["OPTIONS", mode] += 1
        if mode == "xo_preflight_503":
            self._send(503, "text/html", b"<html>Service Unavailable</html>")
            return
        self.send_response(204)
        for name, value in self._cors().items():
            self.send_header(name, value)
        self.send_header("Access-Control-Allow-Methods", "POST")
        self.send_header("Access-Control-Allow-Headers", "content-type, accept")
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802
        self.rfile.read(int(self.headers.get("content-length") or 0))
        mode = self._mode()
        self.hits["POST", mode] += 1
        if mode == "xo_ok":
            self._stream([snapshot(1), snapshot(3)], "\n", self._cors())
        elif mode == "xo_429":
            self._send(429, "text/html", b"<html>Too Many Requests</html>")
        else:
            self._send(503, "text/html", b"<html>Service Unavailable</html>")

    def _mode(self) -> str:
        return parse_qs(urlparse(self.path).query).get("mode", [""])[0]

    def _cors(self) -> dict[str, str]:
        return {
            "Access-Control-Allow-Origin": self.headers.get("Origin") or "*",
            "Access-Control-Allow-Credentials": "true",
        }


class FakeHandle:
    """The two ``BrowserHandle`` methods the wait loop uses."""

    def __init__(self) -> None:
        self.screenshots: list[str] = []

    def check_cancelled(self) -> None:
        return None

    def screenshot(self, _page: Page, name: str) -> None:
        self.screenshots.append(name)


def _serve(handler: type[BaseHTTPRequestHandler]) -> tuple[ThreadingHTTPServer, str]:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


@pytest.fixture(scope="module")
def server() -> Iterator[str]:
    api, api_origin = _serve(FakeNaverApi)
    FakeNaver.api_origin = api_origin
    page_server, page_origin = _serve(FakeNaver)
    yield page_origin
    for httpd in (page_server, api):
        httpd.shutdown()
        httpd.server_close()


@pytest.fixture(scope="module")
def browser() -> Iterator[Browser]:
    with sync_playwright() as playwright:
        edge = playwright.chromium.launch(channel="msedge", headless=True)
        yield edge
        edge.close()


@pytest.fixture
def page(browser: Browser) -> Iterator[Page]:
    context = browser.new_context()
    page = context.new_page()
    yield page
    context.close()


def run_capture(
    page: Page, server: str, mode: str, timeout_s: float = 15.0
) -> tuple[str, naver.CaptureState, FakeHandle, list[Any]]:
    """Prepare ``page`` like the provider does and wait for the outcome of ``mode``."""
    state = naver.CaptureState()
    messages: list[Any] = []
    original = state.on_message

    def record(message: Any) -> None:
        messages.append(message)
        original(message)

    state.on_message = record  # type: ignore[method-assign]
    naver.prepare_page(page, state)
    handle = FakeHandle()
    text = naver.await_final_event(
        handle,  # type: ignore[arg-type]
        page,
        f"{server}/?mode={mode}",
        state,
        timeout_s=timeout_s,
    )
    return text, state, handle, messages


def test_forwards_only_the_final_snapshot(page: Page, server: str) -> None:
    text, state, _handle, messages = run_capture(page, server, "ok")
    assert json.loads(text)["status"]["completedPartnerCount"] == 3
    assert [m["kind"] for m in messages] == ["final"]
    assert state.streams_ok == 1
    page.wait_for_function("window.__pageRead > 0")  # the page still read its own copy


def test_crlf_event_separators(page: Page, server: str) -> None:
    text, *_ = run_capture(page, server, "crlf")
    assert json.loads(text)["status"]["isCompleted"] is True


def test_multi_megabyte_snapshots_arrive_intact(page: Page, server: str) -> None:
    text, *_ = run_capture(page, server, "big")
    event = json.loads(text)
    assert event["status"]["isCompleted"] is True
    assert len(event["popularFlights"][0]["pad"]) == 2_500_000


def test_cross_origin_stream_with_preflight(page: Page, server: str) -> None:
    text, state, *_ = run_capture(page, server, "xo_ok")
    assert json.loads(text)["status"]["isCompleted"] is True and state.streams_ok == 1


@pytest.mark.parametrize("mode", ["503", "json"])
def test_refusals_raise_provider_blocked_and_go_offline(page: Page, server: str, mode: str) -> None:
    with pytest.raises(ProviderBlocked):
        run_capture(page, server, mode)
    assert page.is_closed()  # the retry loop is cut off at once


@pytest.mark.parametrize(
    ("mode", "status"), [("xo_503", 503), ("xo_429", 429), ("xo_preflight_503", 503)]
)
def test_cors_less_cross_origin_refusals_are_blocks(
    page: Page, server: str, mode: str, status: int
) -> None:
    """Without CORS headers the page and Playwright never see the status; CDP does."""
    with pytest.raises(ProviderBlocked, match=f"HTTP {status}") as info:
        run_capture(page, server, mode)
    assert page.is_closed()
    time.sleep(1.5)  # the page would retry after 1 s (preflights ignore offline emulation)
    assert FakeNaverApi.hits["OPTIONS", mode] == 1, info.value
    assert FakeNaverApi.hits["POST", mode] == (0 if mode == "xo_preflight_503" else 1)


@pytest.mark.parametrize("mode", ["xo_503", "xo_preflight_503"])
def test_cors_less_refusals_are_blocks_without_cdp(
    page: Page, server: str, mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(naver, "attach_status_watcher", lambda _page, _state: False)
    with pytest.raises(ProviderBlocked, match="ERR_FAILED"):
        run_capture(page, server, mode)
    assert page.is_closed()


def test_sub_frames_cannot_feed_the_binding(page: Page, server: str) -> None:
    text, state, _handle, messages = run_capture(page, server, "iframe")
    assert "FAKE" not in text and json.loads(text)["status"]["searchKey"] == "RTY100PN"
    assert [m["kind"] for m in messages] == ["final"] and state.http_failure is None


def test_expired_snapshot_is_skipped_for_the_research(page: Page, server: str) -> None:
    text, state, _handle, messages = run_capture(page, server, "expired")
    event = json.loads(text)
    assert event["isExpired"] is False and event["status"]["isCompleted"] is True
    assert messages[0]["reason"] == "incomplete" and messages[0]["expired"] is True
    assert messages[-1]["kind"] == "final" and state.streams_ok == 2


def test_expired_snapshot_without_research_fails(
    page: Page, server: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(naver, "INCOMPLETE_GRACE_S", 0.3)
    with pytest.raises(ProviderError, match="expired") as info:
        run_capture(page, server, "expired_only")
    assert not isinstance(info.value, ProviderBlocked)


def test_other_http_errors_are_plain_failures(page: Page, server: str) -> None:
    with pytest.raises(ProviderError) as info:
        run_capture(page, server, "500")
    assert not isinstance(info.value, ProviderBlocked)
    assert "HTTP 500" in str(info.value)


def test_incomplete_stream_fails_after_grace(
    page: Page, server: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(naver, "INCOMPLETE_GRACE_S", 0.3)
    with pytest.raises(ProviderError, match=r"before completion \(2/3 partners\)"):
        run_capture(page, server, "incomplete")


def test_timeout_without_any_search(page: Page, server: str) -> None:
    with pytest.raises(ProviderError, match="no complete result within 1 s") as info:
        run_capture(page, server, "none", timeout_s=1.0)
    assert not isinstance(info.value, ProviderBlocked)
