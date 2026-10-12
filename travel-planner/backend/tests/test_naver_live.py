"""Live Naver Flights search (one real search; skipped unless ``RUN_LIVE=1``).

Opens a headed Edge window off-screen, so it needs a logged-in Windows desktop session.
Keep it rare: Naver's terms forbid automated collection (see the provider docstring).
Set ``NAVER_LIVE_DUMP=<path>`` to save the captured final event for inspection.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from app.browser.manager import BrowserManager
from app.providers.flights import naver
from app.providers.flights.base import FlightQuery, ProviderBlocked, ProviderContext

pytestmark = pytest.mark.live


@pytest.fixture
def manager() -> Iterator[BrowserManager]:
    browser_manager = BrowserManager(channel="msedge")
    yield browser_manager
    browser_manager.shutdown()


def test_live_sel_tyo_round_trip(manager: BrowserManager, monkeypatch: pytest.MonkeyPatch) -> None:
    naver.reset_state()
    captured: dict[str, Any] = {}
    original = naver.capture_search

    def recording_capture(*args: Any, **kwargs: Any) -> naver.CaptureResult:
        result = original(*args, **kwargs)
        captured["result"] = result
        return result

    monkeypatch.setattr(naver, "capture_search", recording_capture)
    ctx = ProviderContext(get_secret=lambda _name: None, browser=manager)
    provider = naver.NaverFlightsProvider(ctx)
    query = FlightQuery("SEL", "TYO", date(2026, 11, 20), date(2026, 11, 27), adults=1)
    try:
        offers = provider.search_flights(query)
    except ProviderBlocked as exc:  # report, never retry
        pytest.fail(f"Naver refused the search: {exc}")

    result: naver.CaptureResult = captured["result"]
    dump = os.environ.get("NAVER_LIVE_DUMP")
    if dump:
        Path(dump).write_text(json.dumps(result.event, ensure_ascii=False), encoding="utf-8")
    status = result.event["status"]
    print(
        f"\nNaver live: {len(offers)} offers in {result.elapsed_s:.1f}s; "
        f"cheapest A01 {offers[0].price_per_person:,.0f} KRW/person "
        f"({offers[0].seller}, {offers[0].outbound.flight_numbers}/"
        f"{offers[0].inbound.flight_numbers if offers[0].inbound else '-'}); "
        f"lowestFare={status.get('lowestFare')}; mappings={len(result.event['fareMappings'])}; "
        f"partners={json.dumps(result.partners, ensure_ascii=False)}"
    )
    assert status["isCompleted"] is True
    assert offers and all(o.fare_type == "A01" and o.currency == "KRW" for o in offers)
    assert offers == sorted(offers, key=lambda o: o.price_per_person)
    assert all(o.inbound is not None for o in offers)
    assert {offers[0].outbound.departure_airport} <= {"ICN", "GMP"}
    assert {offers[0].outbound.arrival_airport} <= {"NRT", "HND"}
