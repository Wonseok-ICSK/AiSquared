"""Google Flights ``tfs`` encoder. Expected values are tfs strings echoed by real Google Flights
pages saved by the researcher on 2026-10-06 (docs/research/google-flights-browser.md); the
nonstop and both-legs-selected ones were also produced by Google's own UI byte for byte."""

from __future__ import annotations

from datetime import date
from urllib.parse import parse_qs, urlparse

import pytest

from app.providers.flights.base import FlightLeg, FlightQuery, FlightSegment
from app.providers.flights.google_flights_tfs import (
    SEARCH_PAGE_URL,
    TFU_BEST,
    USER_BOOKING_URL,
    USER_SEARCH_URL,
    SelectedFlight,
    TfsLeg,
    booking_link,
    bytes_field,
    decode_message,
    decode_tfs,
    encode_tfs,
    encode_varint,
    max_stops_value,
    place_codes,
    query_tfs,
    search_url,
    selected_from_leg,
    user_link,
    varint_field,
)

# ICN<->NRT 2026-11-20/27, 2 adults, economy (nrt_2a_tfu.html).
TFS_RT_2A = (
    "CBwQAhoeEgoyMDI2LTExLTIwagcIARIDSUNOcgcIARIDTlJUGh4SCjIwMjYtMTEtMjdqBwgBEgNOUlRyBwgBEgNJQ05A"
    "AUABSAFwAYIBCwj___________8BmAEB"
)
# Same, nonstop only (leg field 5 = 0) — identical to the research doc's UI-verified example.
TFS_RT_2A_NONSTOP = (
    "CBwQAhogEgoyMDI2LTExLTIwKABqBwgBEgNJQ05yBwgBEgNOUlQaIBIKMjAyNi0xMS0yNygAagcIARIDTlJUcgcIARID"
    "SUNOQAFAAUgBcAGCAQsI____________AZgBAQ"
)
# ICN -> NRT+HND (two destination places).
TFS_RT_2A_NRT_HND = (
    "CBwQAhonEgoyMDI2LTExLTIwagcIARIDSUNOcgcIARIDTlJUcgcIARIDSE5EGicSCjIwMjYtMTEtMjdqBwgBEgNOUlRq"
    "BwgBEgNITkRyBwgBEgNJQ05AAUABSAFwAYIBCwj___________8BmAEB"
)
# Seoul /m/0hsqf <-> Tokyo /m/07dfk city ids (place type 2).
TFS_RT_2A_CITY_IDS = (
    "CBwQAhooEgoyMDI2LTExLTIwagwIAhIIL20vMGhzcWZyDAgCEggvbS8wN2RmaxooEgoyMDI2LTExLTI3agwIAhIIL20v"
    "MDdkZmtyDAgCEggvbS8waHNxZkABQAFIAXABggELCP___________wGYAQE"
)
# Outbound YP735 selected (sel_out.html: the return-flight list).
TFS_RT_2A_OUT_YP735 = (
    "CBwQAho_EgoyMDI2LTExLTIwIh8KA0lDThIKMjAyNi0xMS0yMBoDTlJUKgJZUDIDNzM1agcIARIDSUNOcgcIARIDTlJU"
    "Gh4SCjIwMjYtMTEtMjdqBwgBEgNOUlRyBwgBEgNJQ05AAUABSAFwAYIBCwj___________8BmAEB"
)
# YP735 + YP732 selected (booking.html: "Booking options"; also what Google's UI generated).
TFS_RT_2A_BOTH_SELECTED = (
    "CBwQAho_EgoyMDI2LTExLTIwIh8KA0lDThIKMjAyNi0xMS0yMBoDTlJUKgJZUDIDNzM1agcIARIDSUNOcgcIARIDTlJU"
    "Gj8SCjIwMjYtMTEtMjciHwoDTlJUEgoyMDI2LTExLTI3GgNJQ04qAllQMgM3MzJqBwgBEgNOUlRyBwgBEgNJQ05AAUAB"
    "SAFwAYIBCwj___________8BmAEB"
)

YP735 = SelectedFlight("ICN", "2026-11-20", "NRT", "YP", "735")
YP732 = SelectedFlight("NRT", "2026-11-27", "ICN", "YP", "732")


def rt(origin: str = "ICN", destination: str = "NRT", **kwargs: object) -> FlightQuery:
    outbound, inbound = date(2026, 11, 20), date(2026, 11, 27)
    return FlightQuery(origin, destination, outbound, inbound, **kwargs)  # type: ignore[arg-type]


def params(url: str) -> dict[str, list[str]]:
    return parse_qs(urlparse(url).query)


# --------------------------------------------------------------------------- byte-for-byte


def test_round_trip_two_adults_matches_google() -> None:
    assert query_tfs(rt(adults=2)) == TFS_RT_2A


def test_direct_only_sets_max_stops_zero_on_both_legs() -> None:
    assert query_tfs(rt(adults=2, direct_only=True)) == TFS_RT_2A_NONSTOP
    assert query_tfs(rt(adults=2, max_stops=0)) == TFS_RT_2A_NONSTOP


def test_multiple_destination_airports_are_repeated_places() -> None:
    legs = [
        TfsLeg("2026-11-20", ("ICN",), ("NRT", "HND")),
        TfsLeg("2026-11-27", ("NRT", "HND"), ("ICN",)),
    ]
    assert encode_tfs(legs, adults=2) == TFS_RT_2A_NRT_HND


def test_city_ids_use_place_type_two() -> None:
    legs = [
        TfsLeg("2026-11-20", ("/m/0hsqf",), ("/m/07dfk",)),
        TfsLeg("2026-11-27", ("/m/07dfk",), ("/m/0hsqf",)),
    ]
    assert encode_tfs(legs, adults=2) == TFS_RT_2A_CITY_IDS
    assert query_tfs(rt("/m/0hsqf", "/m/07dfk", adults=2)) == TFS_RT_2A_CITY_IDS


def test_selected_outbound_segment() -> None:
    assert query_tfs(rt(adults=2), outbound=[YP735]) == TFS_RT_2A_OUT_YP735


def test_selected_segments_on_both_legs() -> None:
    assert query_tfs(rt(adults=2), outbound=[YP735], inbound=[YP732]) == TFS_RT_2A_BOTH_SELECTED


# --------------------------------------------------------------------------- structure


def test_metro_codes_expand_to_airports() -> None:
    assert place_codes("SEL") == ("ICN", "GMP")
    assert place_codes("tyo") == ("NRT", "HND")
    assert place_codes("ICN") == ("ICN",)
    assert place_codes("/m/07dfk") == ("/m/07dfk",)
    top = decode_message(decode_tfs(query_tfs(rt("SEL", "TYO"))))
    first_leg_bytes = next(value for field, value in top if field == 3)
    first_leg = decode_message(first_leg_bytes)  # type: ignore[arg-type]
    origins = [decode_message(v) for f, v in first_leg if f == 13]  # type: ignore[arg-type]
    destinations = [decode_message(v) for f, v in first_leg if f == 14]  # type: ignore[arg-type]
    assert origins == [[(1, 1), (2, b"ICN")], [(1, 1), (2, b"GMP")]]
    assert destinations == [[(1, 1), (2, b"NRT")], [(1, 1), (2, b"HND")]]


def test_one_way_has_one_leg_and_trip_two() -> None:
    query = FlightQuery("ICN", "NRT", date(2026, 11, 20), None)
    top = decode_message(decode_tfs(query_tfs(query)))
    assert [field for field, _ in top].count(3) == 1
    assert (19, 2) in top
    assert [value for field, value in top if field == 8] == [1]


@pytest.mark.parametrize(
    ("cabin", "seat"), [("economy", 1), ("premium_economy", 2), ("business", 3), ("first", 4)]
)
def test_cabin_maps_to_seat_field(cabin: str, seat: int) -> None:
    top = decode_message(decode_tfs(query_tfs(rt(cabin=cabin))))
    assert (9, seat) in top


def test_passengers_repeat_field_eight_per_adult() -> None:
    top = decode_message(decode_tfs(query_tfs(rt(adults=2))))
    assert [value for field, value in top if field == 8] == [1, 1]


@pytest.mark.parametrize(
    ("direct_only", "max_stops", "expected"),
    [
        (True, 2, 0),
        (False, None, None),
        (False, 0, 0),
        (False, 1, 1),
        (False, 2, 2),
        (False, 3, None),
    ],
)
def test_max_stops_value(direct_only: bool, max_stops: int | None, expected: int | None) -> None:
    assert max_stops_value(rt(direct_only=direct_only, max_stops=max_stops)) == expected


def test_varint_and_fields() -> None:
    assert encode_varint(0) == b"\x00"
    assert encode_varint(300) == b"\xac\x02"
    assert encode_varint(-1) == b"\xff" * 9 + b"\x01"
    assert varint_field(19, 1) == b"\x98\x01\x01"
    assert bytes_field(2, "ICN") == b"\x12\x03ICN"


def test_decode_accepts_standard_base64_with_padding() -> None:
    standard = TFS_RT_2A.replace("-", "+").replace("_", "/") + "=" * (-len(TFS_RT_2A) % 4)
    assert decode_tfs(standard) == decode_tfs(TFS_RT_2A)


def test_encode_requires_a_leg() -> None:
    with pytest.raises(ValueError):
        encode_tfs([])


# --------------------------------------------------------------------------- selections


def test_selected_flight_from_segment_and_list_round_trip() -> None:
    segment = FlightSegment(
        "YP", "Air Premia", "YP0735", "ICN", "NRT", "2026-11-20T13:55", "2026-11-20T16:30"
    )
    selected = SelectedFlight.from_segment(segment)
    assert selected == YP735
    assert SelectedFlight.from_list(YP735.to_list()) == YP735
    with pytest.raises(ValueError):
        SelectedFlight.from_list(["ICN", "2026-11-20", "NRT", "YP"])


def test_selected_from_leg_needs_every_flight_number() -> None:
    known = FlightSegment(
        "KE", "Korean Air", "KE787", "ICN", "FUK", "2026-11-20T07:45", "2026-11-20T09:15"
    )
    unknown = FlightSegment(
        None, "Jetstar", None, "FUK", "NRT", "2026-11-20T13:35", "2026-11-20T15:30"
    )
    leg = FlightLeg("ICN", "NRT", "2026-11-20T07:45", "2026-11-20T15:30", 465, 1, [], [], [known])
    assert selected_from_leg(leg) == (SelectedFlight("ICN", "2026-11-20", "FUK", "KE", "787"),)
    leg.segments.append(unknown)
    assert selected_from_leg(leg) is None
    assert selected_from_leg(FlightLeg("ICN", "NRT", "x", "y", None, 0, [], [])) is None


# --------------------------------------------------------------------------- URLs


def test_search_url_uses_base_path_with_tfu_and_currency() -> None:
    url = search_url(rt(adults=2, currency="krw"))
    assert url.startswith(SEARCH_PAGE_URL + "?")
    assert params(url) == {
        "tfs": [TFS_RT_2A],
        "tfu": [TFU_BEST],
        "hl": ["en"],
        "gl": ["KR"],
        "curr": ["KRW"],
    }


def test_user_link_is_korean_search_page() -> None:
    url = user_link(rt(adults=2))
    assert url.startswith(USER_SEARCH_URL + "?")
    assert params(url)["hl"] == ["ko"]
    assert params(url)["tfs"] == [TFS_RT_2A]
    assert params(user_link(rt(adults=2), outbound=[YP735]))["tfs"] == [TFS_RT_2A_OUT_YP735]


def test_booking_link_selects_all_legs_without_tfu() -> None:
    url = booking_link(rt(adults=2), [YP735], [YP732])
    assert url.startswith(USER_BOOKING_URL + "?")
    assert params(url) == {
        "tfs": [TFS_RT_2A_BOTH_SELECTED],
        "hl": ["ko"],
        "gl": ["KR"],
        "curr": ["KRW"],
    }
    with pytest.raises(ValueError):
        booking_link(rt(adults=2), [YP735])  # round trip without the return flight
