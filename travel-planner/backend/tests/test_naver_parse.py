"""Pure parsing of Naver's final ``searchFlights`` event (``naver_parse``).

Fixture ``fixtures/naver/final_event_icn_nrt_2adults.json``: the real final SSE event of an
ICN-NRT 2026-12-03/12-07 search for 2 adults (captured 2026-10-06), trimmed to 6 fare mappings
and the itineraries they reference (``popularFlights`` emptied). It contains:

* ``7C1107-7C1182`` — first in ``fareMappings`` because of a card-conditional fare (279,100
  A01/B16 하나카드 이용실적 충족시); its cheapest A01 fare is 293,393 (SKP206).
* ``7C1107-MM0641`` — the cheapest A01 fare overall (284,020 = ``status.lowestFare.a01``) sold
  by BDC219, a partner outside Naver's Korean OTA list (tax/qCharge 0, baggage UNKNOWN).
* ``7C1107-BX0224`` — cheapest A01 from TOV025 (투어비스) with PARTIAL baggage.
"""

from __future__ import annotations

import copy
import json
import logging
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from app.providers.flights.base import FlightQuery
from app.providers.flights.naver_parse import (
    KOREAN_OTA_PARTNERS,
    MAX_OFFERS,
    NaverParseError,
    check_lowest_fare,
    cheapest_a01_fare,
    hidden_stop_code,
    leg_stops,
    local_iso,
    min_a01_price,
    parse_final_event,
    partner_names_from_graphql,
)

FIXTURE = Path(__file__).parent / "fixtures" / "naver" / "final_event_icn_nrt_2adults.json"
URL = (
    "https://flight.naver.com/flights/international/ICN:airport-NRT:airport-20261203/"
    "NRT:airport-ICN:airport-20261207?adult=2&isDirect=false&fareType=Y"
)
QUERY = FlightQuery("ICN", "NRT", date(2026, 12, 3), date(2026, 12, 7), adults=2)
SEL_TYO = FlightQuery("SEL", "TYO", date(2026, 11, 20), date(2026, 11, 27))
KE_ROUND_TRIP = "20261120ICNNRTKE0701-20261127NRTICNKE0702"
VIA_PVG = "20261120ICNPVGOZ0361+20261120PVGHNDOZ9001"


@pytest.fixture
def event() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def by_ids(offers: list[Any], itinerary_ids: str) -> Any:
    return next(o for o in offers if o.raw["itinerary_ids"] == itinerary_ids)


def segment(
    dep: str, arr: str, start: str, end: str, flight: str, hidden: list[Any] | None = None
) -> dict[str, Any]:
    return {
        "departure": {"airportCode": dep, "dateTime": start},
        "arrival": {"airportCode": arr, "dateTime": end},
        "marketingCarrier": {"airlineCode": flight[:2], "flightNumber": flight[2:]},
        "operatingCarrier": {"airlineCode": flight[:2], "flightNumber": flight[2:]},
        "flightDuration": 7200,
        "groundDuration": 0,
        "hiddenStops": hidden or [],
    }


def a01(price: int, partner: str = "WHY002", **extra: Any) -> dict[str, Any]:
    return {
        "partnerCode": partner,
        "fareType": "A01",
        "adult": {"totalFare": price, "tax": 50000, "qCharge": 10000},
        "isConfirmed": True,
        "baggageFeeType": "FREE",
        **extra,
    }


def synthetic_event(
    mappings: list[dict[str, Any]], itineraries: list[dict[str, Any]], a01_low: int | None = None
) -> dict[str, Any]:
    return {
        "status": {
            "isCompleted": True,
            "airlinesCodeMap": {"KE": "대한항공", "OZ": "아시아나항공"},
            "fareTypesCodeMap": {"A01": {"name": "성인/모든 결제수단"}},
            "lowestFare": {"a01": a01_low} if a01_low else {},
        },
        "itineraries": itineraries,
        "fareMappings": mappings,
    }


# --------------------------------------------------------------------------- fixture


def test_one_offer_per_mapping_sorted_by_cheapest_a01(event: dict[str, Any]) -> None:
    offers = parse_final_event(event, QUERY, search_url=URL)
    assert [o.price_per_person for o in offers] == [
        284020.0,
        292169.0,
        293393.0,
        299681.0,
        305720.0,
        310100.0,
    ]
    assert all(o.fare_type == "A01" and o.currency == "KRW" and o.adults == 2 for o in offers)
    assert all(o.price_total == o.price_per_person * 2 for o in offers)
    assert all(o.booking_url == URL and o.source_url == URL for o in offers)
    assert all(o.raw["no_llm"] is True for o in offers)


def test_cheapest_offer_is_lowest_a01_and_flags_unlisted_partner(event: dict[str, Any]) -> None:
    best = parse_final_event(event, QUERY, search_url=URL)[0]
    assert best.price_per_person == event["status"]["lowestFare"]["a01"] == 284020
    assert best.price_total == 568040.0
    assert best.seller == "BDC219"  # unknown code keeps the code
    assert best.raw["partner_code"] == "BDC219"
    assert best.raw["partner_name"] is None
    assert best.raw["outside_korean_ota_list"] is True
    assert best.raw["baggage_fee_type"] == "UNKNOWN" and best.baggage is None
    assert best.raw["fare_type_label"] == "성인/모든 결제수단"
    assert best.provider_offer_id == "20261203ICNNRT7C1107-20261207NRTICNMM0641"
    assert best.offer_key() == (
        "ICN-NRT-2026-12-03T14:50-7C1107|NRT-ICN-2026-12-07T08:30-MM641|ECONOMY"
    )


def test_card_conditional_fare_is_never_the_price(event: dict[str, Any]) -> None:
    first_ids = "20261203ICNNRT7C1107-20261207NRTICN7C1182"
    assert event["fareMappings"][0]["itineraryIds"] == first_ids
    offer = by_ids(parse_final_event(event, QUERY), first_ids)
    assert offer.price_per_person == 293393.0
    assert offer.seller == "SKP206"
    assert offer.raw["conditional_min_fare"] == {
        "price_per_person": 279100,
        "fare_type": "A01/B16",
        "label": "성인/하나카드(이용실적 충족시)",
        "partner_code": "TBK033",
    }


def test_known_korean_partner_gets_its_name_and_baggage_text(event: dict[str, Any]) -> None:
    offer = by_ids(parse_final_event(event, QUERY), "20261203ICNNRT7C1107-20261207NRTICNBX0224")
    assert offer.price_per_person == 310100.0
    assert offer.seller == "투어비스"
    assert offer.raw["outside_korean_ota_list"] is False
    assert offer.raw["baggage_fee_type"] == "PARTIAL"
    assert offer.baggage == "Checked baggage included on some flights"


def test_legs_and_segments(event: dict[str, Any]) -> None:
    best = parse_final_event(event, QUERY)[0]
    out, back = best.outbound, best.inbound
    assert back is not None
    assert (out.departure_airport, out.arrival_airport) == ("ICN", "NRT")
    assert (out.departure_time, out.arrival_time) == ("2026-12-03T14:50", "2026-12-03T17:15")
    assert out.duration_minutes == 145 and out.stops == 0 and out.layovers == []
    assert out.airlines == ["제주항공"] and out.flight_numbers == ["7C1107"]
    seg = out.segments[0]
    assert (seg.carrier_code, seg.carrier_name, seg.flight_number) == ("7C", "제주항공", "7C1107")
    assert seg.duration_minutes == 145 and seg.aircraft == "7M8" and seg.operating_carrier is None
    assert (back.departure_airport, back.arrival_airport) == ("NRT", "ICN")
    assert back.flight_numbers == ["MM641"] and back.airlines == ["피치항공"]


def test_partner_names_and_korean_list_can_be_overridden(event: dict[str, Any]) -> None:
    offers = parse_final_event(
        event, QUERY, partner_names={"BDC219": "Example OTA"}, korean_partners={"BDC219"}
    )
    assert offers[0].seller == "Example OTA"
    assert offers[0].raw["outside_korean_ota_list"] is False
    assert by_ids(offers, "20261203ICNNRT7C1107-20261207NRTICNBX0224").seller == "TOV025"


def test_default_korean_list_is_the_bookingpartners_list(event: dict[str, Any]) -> None:
    assert len(KOREAN_OTA_PARTNERS) == 26
    flags = {o.seller: o.raw["outside_korean_ota_list"] for o in parse_final_event(event, QUERY)}
    assert flags["와이페이모어"] is False and flags["SKP206"] is True


def test_max_offers_keeps_the_cheapest(event: dict[str, Any]) -> None:
    offers = parse_final_event(event, QUERY, max_offers=2)
    assert [o.price_per_person for o in offers] == [284020.0, 292169.0]
    assert MAX_OFFERS == 30


def test_lowest_fare_cross_check(event: dict[str, Any], caplog: pytest.LogCaptureFixture) -> None:
    assert min_a01_price(event) == 284020
    assert check_lowest_fare(event) is True
    event["status"]["lowestFare"]["a01"] = 280000
    with caplog.at_level(logging.WARNING):
        assert check_lowest_fare(event) is False
        parse_final_event(event, QUERY)
    assert "differs from status.lowestFare.a01" in caplog.text


def test_parse_does_not_mutate_the_event(event: dict[str, Any]) -> None:
    before = copy.deepcopy(event)
    parse_final_event(event, QUERY)
    assert event == before


# --------------------------------------------------------------------------- synthetic cases


ITINERARIES = [
    {
        "itineraryId": "20261120ICNNRTKE0701",
        "duration": 8400,
        "sequence": 1,
        "segments": [segment("ICN", "NRT", "202611200900", "202611201120", "KE0701")],
    },
    {
        "itineraryId": "20261127NRTICNKE0702",
        "duration": 9000,
        "sequence": 2,
        "segments": [segment("NRT", "ICN", "202611271300", "202611271530", "KE0702")],
    },
    {
        "itineraryId": VIA_PVG,
        "sequence": 1,
        "segments": [
            segment(
                "ICN", "PVG", "202611200800", "202611200900", "OZ0361",
                hidden=[{"airportCode": "TAO"}],
            ),
            segment("PVG", "HND", "202611201100", "202611201500", "OZ9001"),
        ],
    },
]


def test_stops_include_hidden_stops_per_segment() -> None:
    mapping = {"itineraryIds": f"{VIA_PVG}-20261127NRTICNKE0702", "fares": [a01(500000)]}
    event = synthetic_event([mapping], ITINERARIES)
    offer = parse_final_event(event, SEL_TYO)[0]
    out = offer.outbound
    assert out.stops == 2  # one connection + one hidden stop
    assert out.layovers == ["TAO", "PVG"]
    assert out.flight_numbers == ["OZ361", "OZ9001"]
    assert out.airlines == ["아시아나항공"]
    assert out.duration_minutes == 240  # no itinerary duration: flight + ground time
    assert offer.inbound is not None and offer.inbound.stops == 0


@pytest.mark.parametrize(
    ("direct_only", "max_stops", "kept"),
    [(True, None, 0), (False, 1, 0), (False, 2, 1), (False, None, 1)],
)
def test_stop_filters(direct_only: bool, max_stops: int | None, kept: int) -> None:
    mapping = {"itineraryIds": f"{VIA_PVG}-20261127NRTICNKE0702", "fares": [a01(500000)]}
    query = FlightQuery(
        "SEL", "TYO", date(2026, 11, 20), date(2026, 11, 27),
        direct_only=direct_only, max_stops=max_stops,
    )
    assert len(parse_final_event(synthetic_event([mapping], ITINERARIES), query)) == kept


def test_mappings_without_usable_a01_or_itineraries_are_skipped() -> None:
    card_only = {
        "fareType": "A01/B05",
        "partnerCode": "WHY002",
        "adult": {"totalFare": 1000},
        "isConfirmed": True,
    }
    disability = {**card_only, "fareType": "A07"}
    unconfirmed = a01(1000, isConfirmed=False)
    mappings = [
        {"itineraryIds": KE_ROUND_TRIP, "fares": [card_only, disability, unconfirmed]},
        {"itineraryIds": "20261120ICNNRTKE0701-MISSING", "fares": [a01(2000)]},
        {"itineraryIds": KE_ROUND_TRIP, "fares": [a01(3000), a01(2500, "TOV025"), card_only]},
    ]
    offers = parse_final_event(synthetic_event(mappings, ITINERARIES), QUERY)
    assert len(offers) == 1
    assert offers[0].price_per_person == 2500.0 and offers[0].seller == "투어비스"
    assert offers[0].raw["conditional_min_fare"]["fare_type"] == "A01/B05"
    assert cheapest_a01_fare(mappings[0]) is None


def test_same_flights_keep_the_cheapest_offer() -> None:
    mappings = [
        {"itineraryIds": KE_ROUND_TRIP, "fares": [a01(4000)]},
        {"itineraryIds": KE_ROUND_TRIP, "fares": [a01(3500)]},
    ]
    offers = parse_final_event(synthetic_event(mappings, ITINERARIES), QUERY)
    assert [o.price_per_person for o in offers] == [3500.0]


def test_one_way_event() -> None:
    mapping = {"itineraryIds": "20261120ICNNRTKE0701", "fares": [a01(150000)]}
    query = FlightQuery("ICN", "NRT", date(2026, 11, 20), None, adults=1, cabin="business")
    offer = parse_final_event(synthetic_event([mapping], ITINERARIES, a01_low=150000), query)[0]
    assert offer.inbound is None and offer.cabin == "business"
    assert offer.price_total == offer.price_per_person == 150000.0
    assert offer.offer_key().endswith("|-|BUSINESS")


def test_codeshare_operating_carrier() -> None:
    itinerary = copy.deepcopy(ITINERARIES[0])
    itinerary["segments"][0]["operatingCarrier"] = {"airlineCode": "FM", "flightNumber": ""}
    mapping = {"itineraryIds": itinerary["itineraryId"], "fares": [a01(1)]}
    offer = parse_final_event(synthetic_event([mapping], [itinerary]), QUERY)[0]
    assert offer.outbound.segments[0].operating_carrier == "FM"


def test_structure_errors() -> None:
    with pytest.raises(NaverParseError):
        parse_final_event({"itineraries": [], "fareMappings": []}, QUERY)
    with pytest.raises(NaverParseError):
        parse_final_event({"status": {}, "itineraries": []}, QUERY)
    with pytest.raises(NaverParseError):
        parse_final_event({"status": {}, "fareMappings": []}, QUERY)
    empty = {"status": {"isCompleted": True}, "itineraries": [], "fareMappings": []}
    assert parse_final_event(empty, QUERY) == []


def test_expired_event_is_rejected(event: dict[str, Any]) -> None:
    """Naver's client discards ``isExpired`` snapshots and searches again: stale prices."""
    assert event["isExpired"] is False
    with pytest.raises(NaverParseError, match="expired"):
        parse_final_event({**event, "isExpired": True}, QUERY)


# --------------------------------------------------------------------------- helpers


def test_local_iso() -> None:
    assert local_iso({"dateTime": "202611200905"}) == "2026-11-20T09:05"
    assert local_iso({"date": "20261120", "time": "0905"}) == "2026-11-20T09:05"
    assert local_iso({"dateTime": "bad"}) is None
    assert local_iso({}) is None


def test_hidden_stop_code_shapes() -> None:
    assert hidden_stop_code("tao") == "TAO"
    assert hidden_stop_code({"airportCode": "TAO"}) == "TAO"
    assert hidden_stop_code({"airport": {"airportCode": "TAO"}}) == "TAO"
    assert hidden_stop_code({"duration": 3600}) is None
    assert hidden_stop_code(None) is None


def test_leg_stops() -> None:
    assert leg_stops([]) == 0
    assert leg_stops([{"hiddenStops": []}]) == 0
    assert leg_stops([{"hiddenStops": [1]}, {"hiddenStops": [1, 2]}]) == 4
    assert leg_stops([{}, {}]) == 1


def test_partner_names_from_graphql() -> None:
    payload = [
        {"otaId": "WHY002", "expName": "와이페이모어"},
        {"otaId": "", "expName": "x"},
        "junk",
        {"otaId": "A"},
    ]
    assert partner_names_from_graphql(payload) == {"WHY002": "와이페이모어"}
    assert partner_names_from_graphql(None) == {}


def test_korean_ota_only_reprices_with_listed_partners(event: dict[str, Any]) -> None:
    offers = parse_final_event(event, QUERY, korean_ota_only=True)
    assert offers and all(o.raw["outside_korean_ota_list"] is False for o in offers)
    first = by_ids(offers, "20261203ICNNRT7C1107-20261207NRTICN7C1182")
    unrestricted = by_ids(parse_final_event(event, QUERY), first.raw["itinerary_ids"])
    assert first.price_per_person > unrestricted.price_per_person  # SKP206 fare skipped
    assert first.raw["partner_code"] in KOREAN_OTA_PARTNERS
    # mappings sold only by unlisted partners (e.g. ...-MM0641 via BDC219/SKP206/ATK201) drop out
    assert all("MM0641" not in o.raw["itinerary_ids"] for o in offers)
