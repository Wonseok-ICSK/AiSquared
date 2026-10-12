"""Google Flights page parsers (ds:1 JSON, aria-label fallback, block detection).

Fixtures in ``fixtures/google_flights`` were cut from real pages saved on 2026-10-06 (ICN<->NRT
2026-11-20/27, KRW, hl=en unless noted):

* ``rt_icn_nrt_2adults.html`` / ``rt_icn_nrt_1adult.html`` — the verbatim ds:1 script only;
* ``return_list_after_yp735.html`` — ds:1 of the search with outbound YP735 selected;
* ``rt_sel_tyo_trimmed.html`` — Seoul<->Tokyo (city ids) ds:1, re-serialized with unused
  top-level blocks nulled and the "other" group cut to 21 rows;
* ``dom_rows_en.html`` / ``dom_rows_ko.html`` — a few result ``li`` nodes (no ds:1), the first
  one duplicated like the page's Best/Cheapest panels.

Error / empty / block pages are synthetic, shaped after the research notes.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from app.providers.flights.base import FlightQuery
from app.providers.flights.google_flights_parse import (
    DS1_MISSING,
    DS1_NO_RESULTS,
    DS1_OK,
    DS1_UNDECODABLE,
    DS1_UNRECOGNISED,
    DS1_USABLE,
    Ds1FormatError,
    DomParseResult,
    count_result_rows,
    detect_block,
    ds1_state,
    expected_airports,
    extract_ds1,
    has_ds1,
    is_result_label,
    iter_rows,
    parse_dom_offers,
    parse_dom_rows,
    parse_itineraries,
    parse_offers,
    parse_price_insights,
    parse_return_options,
    parse_row_label,
    price_level,
)
from app.providers.flights.google_flights_tfs import USER_BOOKING_URL, USER_SEARCH_URL, decode_tfs

FIXTURES = Path(__file__).parent / "fixtures" / "google_flights"
ICN_NRT_2A = FlightQuery("ICN", "NRT", date(2026, 11, 20), date(2026, 11, 27), adults=2)
ICN_NRT_1A = FlightQuery("ICN", "NRT", date(2026, 11, 20), date(2026, 11, 27), adults=1)
SEL_TYO_2A = FlightQuery("SEL", "TYO", date(2026, 11, 20), date(2026, 11, 27), adults=2)

ERROR_PAGE = (
    "<html><body><script class=\"ds:1\" nonce=\"n\">AF_initDataCallback({key: 'ds:1', hash: '1', "
    "data:[null,[[1791272642689858,99203721,2301593480]],[[3,[\"Bad request\"]]]],"
    "errorHasStatus: true,});</script></body></html>"
)
ERROR_PAGE_BROKEN_JSON = ERROR_PAGE.replace("data:[null,", "data:[null,,")
EMPTY_PAGE = (
    "<html><body><script class=\"ds:1\" nonce=\"n\">AF_initDataCallback({key: 'ds:1', hash: '2', "
    "data:[], sideChannel: {}});</script></body></html>"
)
CONSENT_PAGE = (
    "<html><head><title>Before you continue to Google</title></head><body>"
    '<form action="https://consent.google.com/save" method="POST"><button>Reject all</button>'
    "</form></body></html>"
)
SORRY_PAGE = (
    "<html><body><div>Our systems have detected unusual traffic from your computer network."
    '</div><form id="captcha-form" action="index" method="post"></form></body></html>'
)


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def payload(name: str) -> list:
    data = extract_ds1(fixture(name))
    assert data is not None
    return data


@pytest.fixture(scope="module")
def offers_2a() -> list:
    return parse_offers(payload("rt_icn_nrt_2adults.html"), ICN_NRT_2A)


# --------------------------------------------------------------------------- extract_ds1


def test_fixture_sizes_stay_small() -> None:
    assert sum(path.stat().st_size for path in FIXTURES.iterdir()) < 600_000


def test_extract_ds1_returns_the_payload() -> None:
    data = payload("rt_icn_nrt_2adults.html")
    assert len(data) == 32
    assert data[25] == [[None, 631400]]
    assert has_ds1(fixture("rt_icn_nrt_2adults.html"))


def test_extract_ds1_error_status_means_no_results() -> None:
    assert extract_ds1(ERROR_PAGE) == []
    assert extract_ds1(ERROR_PAGE_BROKEN_JSON) == []
    assert parse_offers([], ICN_NRT_2A) == []


def test_extract_ds1_empty_payload_means_no_results() -> None:
    assert extract_ds1(EMPTY_PAGE) == []


def test_extract_ds1_missing_or_garbled() -> None:
    assert extract_ds1(fixture("dom_rows_en.html")) is None
    assert not has_ds1(fixture("dom_rows_en.html"))
    assert extract_ds1(CONSENT_PAGE) is None
    garbled = EMPTY_PAGE.replace("data:[]", "data:[1, 2")
    assert extract_ds1(garbled) is None
    no_data = EMPTY_PAGE.replace("data:[]", "payload:[]")
    assert extract_ds1(no_data) is None


def test_ds1_state_tells_missing_from_undecodable_and_unrecognised() -> None:
    assert ds1_state(fixture("rt_icn_nrt_2adults.html")) == DS1_OK
    assert ds1_state(fixture("return_list_after_yp735.html")) == DS1_OK
    assert ds1_state(ERROR_PAGE) == DS1_NO_RESULTS
    assert ds1_state(EMPTY_PAGE) == DS1_NO_RESULTS
    assert ds1_state(fixture("dom_rows_en.html")) == DS1_MISSING
    assert ds1_state(CONSENT_PAGE) == DS1_MISSING
    assert ds1_state(EMPTY_PAGE.replace("data:[]", "data:[null,null,[[1,,2]]]")) == (
        DS1_UNDECODABLE
    )
    assert ds1_state(EMPTY_PAGE.replace("data:[]", "data:[1]")) == DS1_UNRECOGNISED
    bad_rows = "data:[null,null,[[[[1],[2]]]],null]"
    assert ds1_state(EMPTY_PAGE.replace("data:[]", bad_rows)) == DS1_UNRECOGNISED
    no_rows = "data:[null,null,null,null]"
    assert ds1_state(EMPTY_PAGE.replace("data:[]", no_rows)) == DS1_OK  # groups, zero rows
    assert DS1_USABLE == {DS1_OK, DS1_NO_RESULTS}


# --------------------------------------------------------------------------- parse_offers


def test_parse_counts_and_cheapest(offers_2a: list) -> None:
    rows = list(iter_rows(payload("rt_icn_nrt_2adults.html")))
    assert len(rows) == 62  # 4 top + 58 other
    assert len(offers_2a) == 48  # 14 rows have no price
    cheapest = min(offers_2a, key=lambda offer: offer.price_total)
    assert cheapest.price_total == 631400
    assert cheapest.price_per_person == 315700
    assert cheapest.currency == "KRW"
    assert cheapest.adults == 2
    assert cheapest.outbound.flight_numbers == ["YP735"]


def test_both_groups_are_parsed(offers_2a: list) -> None:
    groups = {offer.raw["group"] for offer in offers_2a}
    assert groups == {"top", "other"}
    assert sum(offer.raw["group"] == "top" for offer in offers_2a) == 3
    assert any(offer.outbound.flight_numbers == ["YP731"] for offer in offers_2a)  # other group


def test_unpriced_rows_are_skipped(offers_2a: list) -> None:
    itineraries = parse_itineraries(payload("rt_icn_nrt_2adults.html"))
    unpriced = [it for it in itineraries if it.price is None]
    assert len(unpriced) == 14
    assert any(it.leg.flight_numbers == ["ZE601"] for it in unpriced)  # Eastar Jet
    numbers = {tuple(offer.outbound.flight_numbers) for offer in offers_2a}
    assert ("ZE601",) not in numbers
    assert ("HX625", "HX604") not in numbers


def test_two_adult_total_is_twice_the_one_adult_total(offers_2a: list) -> None:
    offers_1a = parse_offers(payload("rt_icn_nrt_1adult.html"), ICN_NRT_1A)
    by_key_1a = {offer.offer_key(): offer for offer in offers_1a}
    cheapest_2a = min(offers_2a, key=lambda offer: offer.price_total)
    cheapest_1a = by_key_1a[cheapest_2a.offer_key()]
    assert cheapest_1a.price_total == 315700
    assert cheapest_2a.price_total == 2 * cheapest_1a.price_total
    assert cheapest_2a.price_per_person == cheapest_1a.price_per_person


def test_round_trip_offers_have_unknown_inbound_and_return_date(offers_2a: list) -> None:
    offer = offers_2a[0]
    assert offer.inbound is None
    assert offer.return_date == "2026-11-27"
    assert offer.offer_key() == "ICN-NRT-2026-11-20T13:55-YP735|RT:2026-11-27|ECONOMY"
    assert offer.provider == "google_flights"
    assert offer.cabin == "economy"
    assert offer.raw["source"] == "ds1"
    assert offer.raw["search"] == {
        "origin": "ICN",
        "destination": "NRT",
        "direct_only": False,
        "max_stops": None,
    }


def test_leg_and_segment_details(offers_2a: list) -> None:
    offer = next(o for o in offers_2a if o.outbound.flight_numbers == ["KE787", "GK508"])
    leg = offer.outbound
    assert (leg.departure_airport, leg.arrival_airport) == ("ICN", "NRT")
    assert (leg.departure_time, leg.arrival_time) == ("2026-11-20T07:45", "2026-11-20T15:30")
    assert leg.duration_minutes == 465
    assert leg.stops == 1
    assert leg.airlines == ["Korean Air", "Jetstar"]
    assert leg.layovers == ["FUK"]
    first, second = leg.segments
    assert first.carrier_code == "KE"
    assert (first.carrier_name, first.flight_number) == ("Korean Air", "KE787")
    assert (first.departure_airport, first.arrival_airport) == ("ICN", "FUK")
    assert (first.departure_time, first.arrival_time) == ("2026-11-20T07:45", "2026-11-20T09:15")
    assert first.duration_minutes == 90
    assert first.aircraft == "Boeing 787-10"
    assert (second.departure_airport, second.arrival_airport) == ("FUK", "NRT")
    assert offer.raw["mixed_cabin"] is True  # "Business Class + Economy"
    assert "self_transfer" not in offer.raw


def test_times_with_omitted_zero_components(offers_2a: list) -> None:
    offer = next(o for o in offers_2a if o.outbound.flight_numbers == ["RS267"])
    assert offer.outbound.arrival_time == "2026-11-20T19:00"  # Google sends [19]
    overnight = next(o for o in offers_2a if o.outbound.flight_numbers == ["OZ124", "JL3082"])
    assert overnight.outbound.arrival_time == "2026-11-21T09:30"


def test_airport_change_is_flagged_as_self_transfer(offers_2a: list) -> None:
    offer = next(o for o in offers_2a if o.outbound.flight_numbers == ["OZ112", "NH2178"])
    assert offer.raw["self_transfer"] is True
    assert offer.raw["airport_changes"] == ["KIX-ITM"]
    assert offer.outbound.layovers == ["KIX"]
    assert offer.outbound.segments[1].operating_carrier == "ANA Wings"
    flagged = [o for o in offers_2a if o.raw.get("self_transfer")]
    assert len(flagged) == 2


def test_offer_links(offers_2a: list) -> None:
    offer = offers_2a[0]
    assert offer.source_url is not None and offer.source_url.startswith(USER_SEARCH_URL)
    assert offer.booking_url is not None and offer.booking_url.startswith(USER_SEARCH_URL)
    booking = parse_qs(urlparse(offer.booking_url).query)
    assert booking["hl"] == ["ko"]
    assert b"YP" in decode_tfs(booking["tfs"][0])  # outbound selected -> return list
    one_way = FlightQuery("ICN", "NRT", date(2026, 11, 20), None, adults=2)
    ow_offer = parse_offers(payload("rt_icn_nrt_2adults.html"), one_way)[0]
    assert ow_offer.return_date is None
    assert ow_offer.offer_key().endswith("|-|ECONOMY")
    assert ow_offer.booking_url is not None and ow_offer.booking_url.startswith(USER_BOOKING_URL)


def test_offers_off_the_requested_route_are_dropped() -> None:
    to_haneda = FlightQuery("ICN", "HND", date(2026, 11, 20), date(2026, 11, 27), adults=2)
    assert parse_offers(payload("rt_icn_nrt_2adults.html"), to_haneda) == []
    assert expected_airports("SEL") == frozenset({"ICN", "GMP"})
    assert expected_airports("/m/07dfk") is None


def test_metro_search_keeps_all_airport_pairs() -> None:
    offers = parse_offers(payload("rt_sel_tyo_trimmed.html"), SEL_TYO_2A)
    pairs = {(o.outbound.departure_airport, o.outbound.arrival_airport) for o in offers}
    assert pairs == {("ICN", "NRT"), ("ICN", "HND"), ("GMP", "HND"), ("GMP", "NRT")}
    assert min(o.price_total for o in offers) == 631400
    assert any(o.raw.get("airport_changes") for o in offers)
    assert all(o.raw["search"]["origin"] == "SEL" for o in offers)


def test_duplicate_rows_keep_the_cheapest() -> None:
    data = payload("rt_icn_nrt_2adults.html")
    data[3][0].append(data[2][0][0])  # same itinerary again, same price
    cheaper = [data[2][0][0][0], [[None, 600000], "token"]]
    data[3][0].append(cheaper)
    offers = parse_offers(data, ICN_NRT_2A)
    yp735 = [o for o in offers if o.outbound.flight_numbers == ["YP735"]]
    assert len(yp735) == 1
    assert yp735[0].price_total == 600000


def test_malformed_rows_are_skipped_and_unknown_layout_raises() -> None:
    data = payload("rt_icn_nrt_2adults.html")
    data[3][0].insert(0, [["XX", [], [], "???"], [[None, 1]]])
    assert len(parse_offers(data, ICN_NRT_2A)) == 48
    with pytest.raises(Ds1FormatError):
        parse_offers([None, None], ICN_NRT_2A)  # booking-page-like short payload
    with pytest.raises(Ds1FormatError):
        parse_offers([None, None, [[["garbage"]]], None], ICN_NRT_2A)
    with pytest.raises(Ds1FormatError):
        parse_offers([None, None, ["not a list of rows"], None], ICN_NRT_2A)
    assert parse_offers([None, None, None, None], ICN_NRT_2A) == []  # no rows at all


# --------------------------------------------------------------------------- price insights


def test_price_insights_summary_on_every_offer_history_on_cheapest() -> None:
    offers = parse_offers(payload("rt_icn_nrt_1adult.html"), ICN_NRT_1A)
    cheapest = min(offers, key=lambda offer: offer.price_total)
    insights = cheapest.raw["price_insights"]
    assert insights["lowest_price"] == 315700
    assert insights["typical_price"] == 384200
    assert insights["typical_price_range"] == [305000, 570000]
    assert insights["price_level"] == "typical"
    assert insights["cheapest_listed"] == 315700
    assert len(insights["price_history"]) == 61
    assert insights["price_history"][-1] == [1791212400, 315700]
    others = [offer for offer in offers if offer is not cheapest]
    assert all("price_history" not in offer.raw["price_insights"] for offer in others)
    assert all(offer.raw["price_insights"]["lowest_price"] == 315700 for offer in others)


def test_price_insights_without_history_and_missing() -> None:
    insights = parse_price_insights(payload("rt_icn_nrt_2adults.html"))
    assert insights is not None
    assert insights["typical_price_range"] == [610000, 1150000]
    assert "price_history" not in insights
    assert parse_price_insights(payload("return_list_after_yp735.html")) == {
        "cheapest_listed": 631400
    }
    assert parse_price_insights([None] * 30) is None


@pytest.mark.parametrize(
    ("current", "expected"), [(500, "low"), (610, "typical"), (1150, "typical"), (1200, "high")]
)
def test_price_level_is_derived_from_the_range(current: float, expected: str) -> None:
    assert price_level(current, 610, 1150) == expected
    assert price_level(None, 610, 1150) is None


# --------------------------------------------------------------------------- return list


def test_return_options_after_selecting_the_outbound() -> None:
    options = parse_return_options(payload("return_list_after_yp735.html"), ICN_NRT_2A)
    assert len(options) == 1
    option = options[0]
    assert option.price == 631400
    assert option.leg.flight_numbers == ["YP732"]
    assert (option.leg.departure_airport, option.leg.arrival_airport) == ("NRT", "ICN")
    assert option.leg.departure_time == "2026-11-27T12:30"
    assert parse_return_options([], ICN_NRT_2A) == []


# --------------------------------------------------------------------------- DOM fallback


def test_dom_fallback_english_rows() -> None:
    html = fixture("dom_rows_en.html")
    assert count_result_rows(html) == 4  # 5 nodes, one duplicate
    offers = parse_dom_offers(html, ICN_NRT_2A)
    assert [o.outbound.flight_numbers for o in offers] == [
        ["YP735"],
        ["KE787", "GK508"],
        ["OZ112", "NH2178"],
    ]  # ZE601 is unpriced
    first = offers[0]
    assert first.price_total == 631400
    assert first.price_per_person == 315700
    assert first.offer_key() == "ICN-NRT-2026-11-20T13:55-YP735|RT:2026-11-27|ECONOMY"
    assert first.outbound.arrival_time == "2026-11-20T16:30"
    assert first.outbound.duration_minutes == 155
    assert first.outbound.airlines == ["Air Premia"]
    assert first.outbound.segments == []
    assert first.raw["source"] == "dom"
    assert first.raw["selected"] == [["ICN", "2026-11-20", "NRT", "YP", "735"]]
    connecting = offers[1].outbound
    assert connecting.stops == 1
    assert connecting.layovers == ["FUK"]
    assert connecting.airlines == ["Korean Air", "Jetstar"]
    assert connecting.duration_minutes == 465
    assert offers[2].raw["self_transfer"] is True


def test_dom_fallback_matches_ds1_keys_and_prices(offers_2a: list) -> None:
    by_key = {offer.offer_key(): offer.price_total for offer in offers_2a}
    for offer in parse_dom_offers(fixture("dom_rows_en.html"), ICN_NRT_2A):
        assert by_key[offer.offer_key()] == offer.price_total


def test_dom_fallback_korean_rows() -> None:
    offers = parse_dom_offers(fixture("dom_rows_ko.html"), ICN_NRT_2A)
    assert len(offers) == 2
    first, transfer = offers
    assert first.price_total == 631400
    assert first.outbound.airlines == ["에어프레미아"]
    assert (first.outbound.departure_time, first.outbound.arrival_time) == (
        "2026-11-20T13:55",
        "2026-11-20T16:30",
    )
    assert transfer.outbound.airlines == ["아시아나항공", "전일본공수"]
    assert transfer.outbound.stops == 1
    assert transfer.outbound.duration_minutes == 490
    assert transfer.raw["self_transfer"] is True


def test_row_label_parsing_edge_cases() -> None:
    label = (
        "From 1,234 South Korean won one way total. Nonstop flight with Jeju Air. Leaves "
        "Incheon International Airport at 11:50 PM on Thursday, December 31 and arrives at "
        "Narita International Airport at 2:05 AM on Friday, January 1. Total duration 2 hr 15 min."
    )
    parsed = parse_row_label(label, date(2026, 12, 31))
    assert parsed is not None
    assert parsed.price == 1234
    assert parsed.departure_time == "2026-12-31T23:50"
    assert parsed.arrival_time == "2027-01-01T02:05"  # year rolls over
    assert parsed.duration_minutes == 135
    hours_only = parse_row_label(label.replace("2 hr 15 min", "7 hr"), date(2026, 12, 31))
    assert hours_only is not None and hours_only.duration_minutes == 420
    assert parse_row_label("Departure time: 1:55 PM.", date(2026, 11, 20)) is None
    assert not is_result_label("Departure time: 1:55 PM.")
    assert is_result_label("Total price is unavailable. Nonstop flight with EASTAR JET.")
    noon = parse_row_label(
        "왕복 총 가격은 최저 100 대한민국 원입니다. 제주항공의 직항 항공편입니다. 금요일, 11월 20 "
        "오후 12:05에 인천국제공항에서 출발하여 금요일, 11월 20 오전 12:30에 나리타 국제 공항에 "
        "도착합니다. 총 비행 시간은 7시간입니다.",
        date(2026, 11, 20),
    )
    assert noon is not None
    assert (noon.departure_time, noon.arrival_time) == ("2026-11-20T12:05", "2026-11-20T00:30")
    assert noon.duration_minutes == 420


def test_dom_without_rows_counts_zero() -> None:
    assert count_result_rows(CONSENT_PAGE) == 0
    assert parse_dom_offers(CONSENT_PAGE, ICN_NRT_2A) == []
    assert parse_dom_rows(CONSENT_PAGE, ICN_NRT_2A) == DomParseResult()


def test_dom_rows_are_classified() -> None:
    html = fixture("dom_rows_en.html")
    result = parse_dom_rows(html, ICN_NRT_2A)
    assert (result.rows, len(result.offers), result.unpriced) == (4, 3, 1)
    assert (result.off_route, result.unparsable) == (0, 0)
    kix = FlightQuery("ICN", "KIX", date(2026, 11, 20), date(2026, 11, 27), adults=2)
    off_route = parse_dom_rows(html, kix)
    assert (off_route.offers, off_route.off_route, off_route.unpriced) == ([], 3, 1)
    assert off_route.unparsable == 0
    ko = parse_dom_rows(fixture("dom_rows_ko.html"), ICN_NRT_2A)
    assert ko.unparsable == 0 and len(ko.offers) == 2


@pytest.mark.parametrize(
    "replacements",
    [
        # no travelimpactmodel link and no route-code class: airports unreadable
        [("data-travelimpactmodelwebsiteurl", "data-x"), ("QylvBf", "Zz9")],
        # new wording: departure / arrival times unreadable
        [("Leaves ", "Departs ")],
        # new prefix + wording: the label is not parsed at all
        [('aria-label="From ', 'aria-label="Cheapest. From '), ("Leaves ", "Departs ")],
    ],
    ids=["no-airports", "no-times", "no-label"],
)
def test_dom_rows_that_cannot_be_read_are_counted(replacements: list[tuple[str, str]]) -> None:
    html = fixture("dom_rows_en.html")
    for old, new in replacements:
        html = html.replace(old, new)
    result = parse_dom_rows(html, ICN_NRT_2A)
    assert result.offers == []
    assert (result.rows, result.unparsable, result.unpriced) == (4, 3, 1)


# --------------------------------------------------------------------------- block detection


def test_detect_block_signals() -> None:
    assert detect_block("", "https://www.google.com/travel/flights", 429) == "HTTP 429 from Google"
    assert detect_block("", "https://www.google.com/travel/flights", 403) is not None
    assert "sorry" in (detect_block("", "https://www.google.com/sorry/index?continue=x", 200) or "")
    assert "consent" in (detect_block("", "https://consent.google.com/ml?continue=x", 200) or "")
    assert detect_block(CONSENT_PAGE, "https://www.google.com/travel/flights", 200) is not None
    assert detect_block(SORRY_PAGE, "https://www.google.com/travel/flights", 200) is not None


def test_normal_pages_are_not_blocks() -> None:
    url = "https://www.google.com/travel/flights?tfs=x"
    for name in ("rt_icn_nrt_2adults.html", "dom_rows_en.html", "dom_rows_ko.html"):
        assert detect_block(fixture(name), url, 200) is None
    # Real result pages list "/recaptcha/challenge" in a config blob; that alone is no block.
    assert detect_block('["/recaptcha/challenge","/recaptcha/challengepage"]', url, 200) is None
