"""Golden tests for notification texts (spec §8 example layout, en + ko)."""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from app.models import FlightOffer, Itinerary, SearchRun, Trip
from app.services.events import AlertEvent
from app.services.notifications.base import full_text, truncate_text, truncate_utf8
from app.services.notifications.formatter import (
    format_amount,
    format_itinerary_ready,
    format_price_alert,
    format_test_message,
)

SPEC_EXAMPLE = """Flight Price Alert

Seoul (ICN) ↔ Tokyo (NRT)

Travel Dates:
Nov 20 – Nov 23

Previous Lowest Price:
KRW 320,000

Current Price:
KRW 278,000

Target Price:
KRW 300,000

The current price is below your target price."""

GOLDEN_EN = (
    SPEC_EXAMPLE
    + """
This is a new lowest price.

Outbound: Korean Air KE703 · ICN 09:05 → NRT 11:30 · nonstop
Return: Korean Air KE704 · NRT 13:00 → ICN 15:20 · nonstop
Source: Mock (demo data)"""
)

GOLDEN_KO = """항공권 가격 알림

서울 (ICN) ↔ 도쿄 (NRT)

여행 날짜:
11월 20일 – 11월 23일

이전 최저가:
KRW 320,000

현재 가격:
KRW 278,000

목표 가격:
KRW 300,000

현재 가격이 목표 가격보다 낮습니다.
새로운 최저가입니다.

가는 편: Korean Air KE703 · ICN 09:05 → NRT 11:30 · 직항
오는 편: Korean Air KE704 · NRT 13:00 → ICN 15:20 · 직항
출처: Mock (demo data)"""


def make_trip(**overrides: Any) -> Trip:
    values: dict[str, Any] = {
        "id": 1,
        "name": "Seoul → Tokyo",
        "origin": "SEL",
        "destination": "TYO",
        "trip_type": "round_trip",
        "depart_date": date(2026, 11, 20),
        "return_date": date(2026, 11, 23),
        "adults": 1,
        "price_basis": "per_person",
        "currency": "KRW",
        "target_price": 300_000.0,
    }
    return Trip(**(values | overrides))


def make_offer(**overrides: Any) -> FlightOffer:
    values: dict[str, Any] = {
        "id": 9,
        "trip_id": 1,
        "run_id": 5,
        "provider": "mock",
        "seller": None,
        "offer_key": "k",
        "origin": "ICN",
        "destination": "NRT",
        "depart_date": date(2026, 11, 20),
        "return_date": date(2026, 11, 23),
        "airline": "Korean Air",
        "flight_number": "KE703",
        "departure_airport": "ICN",
        "arrival_airport": "NRT",
        "departure_time": "2026-11-20T09:05",
        "arrival_time": "2026-11-20T11:30",
        "stops": 0,
        "inbound": {
            "departure_airport": "NRT",
            "arrival_airport": "ICN",
            "departure_time": "2026-11-23T13:00",
            "arrival_time": "2026-11-23T15:20",
            "stops": 0,
            "airlines": ["Korean Air"],
            "flight_numbers": ["KE704"],
        },
        "price_total": 278_000.0,
        "price_per_person": 278_000.0,
        "currency": "KRW",
        "booking_url": "https://example.com/book?id=1",
        "source_url": "https://example.com/search",
    }
    return FlightOffer(**(values | overrides))


def event(kind: str, **overrides: Any) -> AlertEvent:
    values: dict[str, Any] = {
        "rule_id": {"below_target": 1, "price_drop": 2, "new_low": 3}[kind],
        "kind": kind,
        "trip_id": 1,
        "run_id": 5,
        "offer_id": 9,
        "price": 278_000.0,
        "basis": "per_person",
        "currency": "KRW",
        "previous_lowest": 320_000.0,
    }
    if kind == "below_target":
        values["threshold"] = 300_000.0
    return AlertEvent(**(values | overrides))


RUN = SearchRun(id=5, trip_id=1, currency="KRW")


def test_spec_example_layout_is_reproduced_exactly() -> None:
    message = format_price_alert(make_trip(), RUN, make_offer(), [event("below_target")])
    assert message.title == "Flight Price Alert"
    assert full_text(message).startswith(SPEC_EXAMPLE)


def test_golden_english_merges_events_in_canonical_order() -> None:
    events = [event("new_low"), event("below_target")]
    message = format_price_alert(make_trip(), RUN, make_offer(), events, "en")
    assert full_text(message) == GOLDEN_EN
    assert message.url == "https://example.com/book?id=1"
    assert message.url_label == "Open booking page"
    assert message.data["subject"] == (
        "Flight Price Alert: ICN ↔ NRT KRW 278,000 (below target, new low)"
    )
    assert message.data["summary"] == (
        "Seoul (ICN) ↔ Tokyo (NRT) · Nov 20 – Nov 23\n"
        "Now KRW 278,000 · target KRW 300,000\n"
        "Below target · new low"
    )
    assert message.data["rule_ids"] == [1, 3]
    assert message.data["alert_kinds"] == ["below_target", "new_low"]
    assert message.data["current_price"] == 278_000.0
    assert len(message.data["summary"]) <= 200


def test_golden_korean() -> None:
    events = [event("below_target"), event("new_low")]
    message = format_price_alert(make_trip(), RUN, make_offer(), events, "ko")
    assert full_text(message) == GOLDEN_KO
    assert message.url_label == "예약 페이지 열기"
    assert message.data["subject"] == (
        "항공권 가격 알림: ICN ↔ NRT KRW 278,000 (목표가 이하, 최저가 갱신)"
    )


def _one_stop_return_offer(**overrides: Any) -> FlightOffer:
    """As monitoring stores it: nonstop outbound leg, 1-stop return, flattened stops = max."""
    outbound = {
        "departure_airport": "ICN",
        "arrival_airport": "NRT",
        "departure_time": "2026-11-20T09:05",
        "arrival_time": "2026-11-20T11:30",
        "duration_minutes": 145,
        "stops": 0,
        "airlines": ["Korean Air"],
        "flight_numbers": ["KE703"],
    }
    inbound = {
        "departure_airport": "NRT",
        "arrival_airport": "ICN",
        "departure_time": "2026-11-23T13:00",
        "arrival_time": "2026-11-23T18:20",
        "stops": 1,
        "airlines": ["Korean Air"],
        "flight_numbers": ["KE704", "KE1102"],
    }
    values = {"stops": 1, "outbound": outbound, "inbound": inbound}
    return make_offer(**(values | overrides))


def test_outbound_stops_come_from_the_outbound_leg_not_the_max_over_legs() -> None:
    message = format_price_alert(make_trip(), RUN, _one_stop_return_offer(), [event("new_low")])
    assert message.body.endswith(
        "Outbound: Korean Air KE703 · ICN 09:05 → NRT 11:30 · nonstop\n"
        "Return: Korean Air KE704 / KE1102 · NRT 13:00 → ICN 18:20 · 1 stop\n"
        "Source: Mock (demo data)"
    )
    ko = format_price_alert(make_trip(), RUN, _one_stop_return_offer(), [event("new_low")], "ko")
    assert "가는 편: Korean Air KE703 · ICN 09:05 → NRT 11:30 · 직항\n" in ko.body
    assert "오는 편: Korean Air KE704 / KE1102 · NRT 13:00 → ICN 18:20 · 경유 1회\n" in ko.body


def test_outbound_stops_fallback_without_stored_leg() -> None:
    # legacy row without an outbound dict: the max equals the return's stops -> unknown, omit
    unknown = format_price_alert(
        make_trip(), RUN, _one_stop_return_offer(outbound={}), [event("new_low")]
    )
    assert "Outbound: Korean Air KE703 · ICN 09:05 → NRT 11:30\n" in unknown.body
    # the return has fewer stops than the max -> the max is the outbound's
    inbound = _one_stop_return_offer().inbound | {"stops": 0}
    known = _one_stop_return_offer(outbound={}, stops=2, inbound=inbound)
    message = format_price_alert(make_trip(), RUN, known, [event("new_low")])
    assert "Outbound: Korean Air KE703 · ICN 09:05 → NRT 11:30 · 2 stops\n" in message.body
    # one-way: the flattened value is the outbound's
    one_way = make_offer(inbound=None, return_date=None, stops=1)
    assert "· 1 stop\nSource:" in format_price_alert(
        make_trip(trip_type="one_way"), RUN, one_way, [event("new_low")]
    ).body


def test_price_drop_verdict_and_reason() -> None:
    drop = event("price_drop", previous_price=317_000.0, drop_percent=12.3)
    message = format_price_alert(make_trip(), RUN, make_offer(), [drop])
    assert "The price dropped 12.3% since the last check." in message.body
    assert message.data["subject"].endswith("(down 12.3%)")
    ko = format_price_alert(make_trip(), RUN, make_offer(), [drop], "ko")
    assert "지난 확인 이후 가격이 12.3% 내렸습니다." in ko.body


def test_drop_percent_computed_when_missing() -> None:
    drop = event("price_drop", previous_price=320_000.0, drop_percent=None)
    message = format_price_alert(make_trip(), RUN, make_offer(), [drop])
    assert "dropped 13.1%" in message.body


def test_two_adults_per_person_basis_note() -> None:
    trip = make_trip(adults=2)
    offer = make_offer(price_total=556_000.0, price_per_person=278_000.0)
    message = format_price_alert(trip, RUN, offer, [event("below_target")])
    assert "Current Price:\nKRW 278,000 per person" in message.body
    assert "Target Price:\nKRW 300,000 per person" in message.body
    ko = format_price_alert(trip, RUN, offer, [event("below_target")], "ko")
    assert "현재 가격:\nKRW 278,000 (1인 기준)" in ko.body


def test_total_basis_converts_event_values() -> None:
    trip = make_trip(adults=2, price_basis="total", target_price=600_000.0)
    offer = make_offer(price_total=556_000.0, price_per_person=278_000.0)
    # the rule is per person; display follows the trip's total basis
    message = format_price_alert(trip, RUN, offer, [event("below_target")])
    assert "Current Price:\nKRW 556,000 total for 2 adults" in message.body
    assert "Target Price:\nKRW 600,000 total for 2 adults" in message.body
    assert "Previous Lowest Price:\nKRW 640,000 total for 2 adults" in message.body
    ko = format_price_alert(trip, RUN, offer, [event("below_target")], "ko")
    assert "KRW 556,000 (성인 2명 합계)" in ko.body


def test_reached_target_wording() -> None:
    offer = make_offer(price_total=300_000.0, price_per_person=300_000.0)
    message = format_price_alert(make_trip(), RUN, offer, [event("below_target", price=300_000.0)])
    assert "The current price has reached your target price." in message.body


def test_one_way_route_and_dates() -> None:
    trip = make_trip(trip_type="one_way", return_date=None)
    offer = make_offer(inbound=None, return_date=None)
    message = format_price_alert(trip, RUN, offer, [event("new_low")])
    assert message.body.startswith("Seoul (ICN) → Tokyo (NRT)\n\nTravel Dates:\nNov 20\n\n")
    assert "Return:" not in message.body


def test_open_jaw_lists_both_airports_and_overnight_arrival() -> None:
    inbound = make_offer().inbound | {
        "departure_airport": "HND",
        "arrival_airport": "GMP",
        "arrival_time": "2026-11-24T00:40",
    }
    message = format_price_alert(make_trip(), RUN, make_offer(inbound=inbound), [event("new_low")])
    assert message.body.startswith("Seoul (ICN/GMP) ↔ Tokyo (NRT/HND)")
    assert "HND 13:00 → GMP 00:40+1" in message.body


def test_link_falls_back_to_source_url_then_app() -> None:
    message = format_price_alert(
        make_trip(), RUN, make_offer(booking_url=None), [event("new_low")]
    )
    assert message.url == "https://example.com/search"
    assert message.url_label == "Open flight search"
    message = format_price_alert(make_trip(), RUN, None, [event("new_low")])
    assert message.url == "http://127.0.0.1:8765/trips/1"
    assert message.body.startswith("Seoul (SEL) ↔ Tokyo (TYO)\n\nTravel Dates:\nNov 20 – Nov 23")
    assert "Current Price:\nKRW 278,000" in message.body


def test_previous_lowest_override_and_missing() -> None:
    no_prev = event("below_target", previous_lowest=None)
    message = format_price_alert(make_trip(), RUN, make_offer(), [no_prev])
    assert "Previous Lowest Price" not in message.body
    message = format_price_alert(
        make_trip(), RUN, make_offer(), [no_prev], previous_lowest=290_000.0
    )
    assert "Previous Lowest Price:\nKRW 290,000" in message.body


def test_source_line_with_seller_and_unknown_provider() -> None:
    offer = make_offer(provider="some_ota", seller="Trip.com")
    message = format_price_alert(make_trip(), RUN, offer, [event("new_low")])
    assert message.body.endswith("Source: Some Ota (sold by Trip.com)")
    ko = format_price_alert(make_trip(), RUN, offer, [event("new_low")], "ko")
    assert ko.body.endswith("출처: Some Ota (판매처: Trip.com)")


def test_unknown_language_falls_back_to_english() -> None:
    message = format_price_alert(make_trip(), RUN, make_offer(), [event("new_low")], "fr")
    assert message.title == "Flight Price Alert"


@pytest.mark.parametrize(
    ("amount", "currency", "expected"),
    [
        (278000, "KRW", "KRW 278,000"),
        (278000.4, "KRW", "KRW 278,000"),
        (12345, "JPY", "JPY 12,345"),
        (199.5, "USD", "USD 199.50"),
        (200.0, "USD", "USD 200"),
    ],
)
def test_format_amount(amount: float, currency: str, expected: str) -> None:
    assert format_amount(amount, currency) == expected


def test_itinerary_ready_en_and_ko() -> None:
    itinerary = Itinerary(
        id=7,
        trip_id=1,
        status="done",
        generator="claude",
        arrival_at="2026-11-20T11:30",
        departure_at="2026-11-23T13:00",
        plan={
            "title": "Tokyo food & sights",
            "days": [{}, {}, {}, {}],
            "budget": {"currency": "KRW", "total_estimated": 1_234_000, "within_budget": True},
        },
    )
    message = format_itinerary_ready(make_trip(), itinerary)
    assert message.title == "Itinerary ready"
    assert message.body == (
        "Your Tokyo itinerary is ready.\n\n"
        "Tokyo food & sights\n"
        "Nov 20 – Nov 23 · 4 days\n"
        "Estimated cost: KRW 1,234,000 (within budget)\n"
        "Generated by: AI (Claude)"
    )
    assert message.url == "http://127.0.0.1:8765/itineraries/7"  # SPA route /itineraries/:id
    ko = format_itinerary_ready(make_trip(), itinerary, "ko")
    assert ko.title == "여행 일정 준비 완료"
    assert ko.body.startswith("도쿄 여행 일정이 준비되었습니다.")
    assert "11월 20일 – 11월 23일 · 4일" in ko.body
    assert "(예산 이내)" in ko.body


def test_itinerary_failed() -> None:
    itinerary = Itinerary(id=8, trip_id=1, status="error", error="LLM quota exceeded")
    message = format_itinerary_ready(make_trip(), itinerary)
    assert message.title == "Itinerary generation failed"
    assert message.body.endswith("LLM quota exceeded")


def test_test_message() -> None:
    message = format_test_message()
    assert message.title == "AiSquared Travel test notification"
    assert message.url == "http://127.0.0.1:8765/settings"
    assert message.data["test"] is True
    assert format_test_message("ko").title == "AiSquared Travel 테스트 알림"


def test_truncation_helpers() -> None:
    text = "line one is here\nline two is a bit longer than the first"
    assert truncate_text(text, 100) == text
    assert truncate_text(text, 22) == "line one is here…"
    assert truncate_text(text, 30) == "line one is here\nline two is…"
    assert len(truncate_text("x" * 500, 200)) == 200
    korean = "가" * 2000
    clipped = truncate_utf8(korean, 4096)
    assert len(clipped.encode("utf-8")) <= 4096 and clipped.endswith("…")
