"""Google Flights ``tfs`` URL parameter: a hand-rolled protobuf writer (no protobuf dependency).

``tfs`` is a URL-safe base64 protobuf message (DESIGN §13.1,
docs/research/google-flights-browser.md).
Field map, verified by decoding URLs Google's own UI produced (byte-identical output):

* top level: ``1=28``, ``2=2`` (UI constants), ``3`` = repeated leg, ``8`` = repeated passenger
  (one varint per person; ``1`` = adult), ``9`` = seat (1 economy .. 4 first), ``14=1``,
  ``16={1: uint64 max}`` (no max price), ``19`` = trip (1 round trip, 2 one way).
* leg: ``2`` = date ``"YYYY-MM-DD"``, ``4`` = repeated selected segment ``{1 from, 2 date, 3 to,
  5 airline IATA, 6 flight number}``, ``5`` = max stops (0 nonstop, 1, 2; omitted = any),
  ``13`` / ``14`` = repeated origin / destination place ``{1: type, 2: code}`` with type 1 = airport
  IATA code, 2 = city Freebase id (``/m/07dfk``).

Metro codes are expanded to their airports (``SEL`` -> ICN + GMP as two places) with
:func:`app.core.airports.airports_for`. Machine fetches use the base path ``/travel/flights``
(not robots-disallowed); ``/travel/flights/search`` and ``/travel/flights/booking`` URLs are built
only as user-facing links.
"""

from __future__ import annotations

import base64
import re
from collections.abc import Sequence
from dataclasses import dataclass
from urllib.parse import urlencode

from app.core.airports import airports_for
from app.providers.flights.base import (
    FlightLeg,
    FlightQuery,
    FlightSegment,
    normalize_flight_number,
)

SEARCH_PAGE_URL = "https://www.google.com/travel/flights"
"""Base path fetched by the provider (what fast-flights calls; not listed in robots.txt)."""
USER_SEARCH_URL = "https://www.google.com/travel/flights/search"
"""User-facing search link (robots-disallowed for crawlers: never fetched by the provider)."""
USER_BOOKING_URL = "https://www.google.com/travel/flights/booking"
"""User-facing "Booking options" link (all legs selected; never fetched by the provider)."""

TFU_BEST = "EgQIABABIgA"
"""``tfu`` of the "Best" tab: without it Google truncates the result list."""
COUNTRY = "KR"
MACHINE_LANGUAGE = "en"
USER_LANGUAGE = "ko"

SEAT_CODES: dict[str, int] = {"economy": 1, "premium_economy": 2, "business": 3, "first": 4}
TRIP_ROUND = 1
TRIP_ONE_WAY = 2
PASSENGER_ADULT = 1
PLACE_AIRPORT = 1
PLACE_CITY = 2
MAX_STOPS_FILTER = 2
"""Largest max-stops value Google's filter offers; larger limits mean "any"."""

# Top-level fields.
_F_UI_A, _F_UI_B, _F_LEG, _F_PASSENGER = 1, 2, 3, 8
_F_SEAT, _F_UI_C, _F_PRICE, _F_TRIP = 9, 14, 16, 19
_UI_A_VALUE, _UI_B_VALUE, _UI_C_VALUE = 28, 2, 1
# Leg fields.
_F_DATE, _F_SELECTED, _F_MAX_STOPS, _F_ORIGIN, _F_DESTINATION = 2, 4, 5, 13, 14
# Place / selected-segment fields.
_F_PLACE_TYPE, _F_PLACE_CODE = 1, 2
_F_SEL_FROM, _F_SEL_DATE, _F_SEL_TO, _F_SEL_CARRIER, _F_SEL_NUMBER = 1, 2, 3, 5, 6

_WIRE_VARINT = 0
_WIRE_LEN = 2
_UINT64_MASK = (1 << 64) - 1
_CITY_ID_PREFIX = "/m/"
_FLIGHT_RE = re.compile(r"^([A-Z0-9]{2})(\d{1,4}[A-Z]?)$")


# --------------------------------------------------------------------------- data


@dataclass(frozen=True)
class SelectedFlight:
    """One flown segment selected in a leg (leg field 4), e.g. ``ICN 2026-11-20 NRT YP 735``."""

    departure_airport: str
    date: str  # "YYYY-MM-DD" (local departure date)
    arrival_airport: str
    carrier_code: str  # "YP"
    number: str  # flight number without the carrier: "735"

    @classmethod
    def from_segment(cls, segment: FlightSegment) -> SelectedFlight | None:
        """Selection for a parsed segment; ``None`` when carrier or flight number is unknown."""
        if not segment.flight_number or len(segment.departure_time) < 10:
            return None
        match = _FLIGHT_RE.match(normalize_flight_number(segment.flight_number))
        if match is None:
            return None
        carrier, number = match.groups()
        if segment.carrier_code and segment.carrier_code.upper() != carrier:
            return None
        return cls(
            departure_airport=segment.departure_airport.upper(),
            date=segment.departure_time[:10],
            arrival_airport=segment.arrival_airport.upper(),
            carrier_code=carrier,
            number=number,
        )

    def to_list(self) -> list[str]:
        """JSON-safe ``[from, date, to, carrier, number]`` (stored in ``FlightOffer.raw``)."""
        return [
            self.departure_airport,
            self.date,
            self.arrival_airport,
            self.carrier_code,
            self.number,
        ]

    @classmethod
    def from_list(cls, value: Sequence[str]) -> SelectedFlight:
        """Inverse of :meth:`to_list` (``ValueError`` on a malformed entry)."""
        if len(value) != 5 or not all(isinstance(part, str) and part for part in value):
            raise ValueError(f"malformed selected flight: {value!r}")
        return cls(*(part.upper() if index != 1 else part for index, part in enumerate(value)))


@dataclass(frozen=True)
class TfsLeg:
    """One leg of the ``tfs`` message."""

    date: str  # "YYYY-MM-DD"
    origins: tuple[str, ...]  # IATA codes or /m/ city ids
    destinations: tuple[str, ...]
    max_stops: int | None = None
    selected: tuple[SelectedFlight, ...] = ()


def selected_from_leg(leg: FlightLeg) -> tuple[SelectedFlight, ...] | None:
    """Selections for every segment of ``leg``; ``None`` if a segment cannot be selected."""
    if not leg.segments:
        return None
    selected = [SelectedFlight.from_segment(segment) for segment in leg.segments]
    if any(item is None for item in selected):
        return None
    return tuple(item for item in selected if item is not None)


# --------------------------------------------------------------------------- protobuf writer


def encode_varint(value: int) -> bytes:
    """Protobuf base-128 varint (negative values as 64-bit two's complement)."""
    remaining = value & _UINT64_MASK
    out = bytearray()
    while True:
        byte = remaining & 0x7F
        remaining >>= 7
        if remaining:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def varint_field(number: int, value: int) -> bytes:
    """A varint field (wire type 0)."""
    return encode_varint((number << 3) | _WIRE_VARINT) + encode_varint(value)


def bytes_field(number: int, value: bytes | str) -> bytes:
    """A length-delimited field (wire type 2); ``str`` is UTF-8 encoded."""
    data = value.encode("utf-8") if isinstance(value, str) else value
    return encode_varint((number << 3) | _WIRE_LEN) + encode_varint(len(data)) + data


def encode_place(code: str) -> bytes:
    """Place message: airport IATA code (type 1) or ``/m/`` city id (type 2)."""
    place_type = PLACE_CITY if code.startswith(_CITY_ID_PREFIX) else PLACE_AIRPORT
    return varint_field(_F_PLACE_TYPE, place_type) + bytes_field(_F_PLACE_CODE, code)


def encode_selected(flight: SelectedFlight) -> bytes:
    """Selected-segment message (leg field 4)."""
    return (
        bytes_field(_F_SEL_FROM, flight.departure_airport)
        + bytes_field(_F_SEL_DATE, flight.date)
        + bytes_field(_F_SEL_TO, flight.arrival_airport)
        + bytes_field(_F_SEL_CARRIER, flight.carrier_code)
        + bytes_field(_F_SEL_NUMBER, flight.number)
    )


def encode_leg(leg: TfsLeg) -> bytes:
    """Leg message, fields in the order Google's UI writes them."""
    out = bytes_field(_F_DATE, leg.date)
    for flight in leg.selected:
        out += bytes_field(_F_SELECTED, encode_selected(flight))
    if leg.max_stops is not None:
        out += varint_field(_F_MAX_STOPS, leg.max_stops)
    for code in leg.origins:
        out += bytes_field(_F_ORIGIN, encode_place(code))
    for code in leg.destinations:
        out += bytes_field(_F_DESTINATION, encode_place(code))
    return out


def encode_tfs(
    legs: Sequence[TfsLeg],
    *,
    adults: int = 1,
    seat: int = SEAT_CODES["economy"],
    trip: int = TRIP_ROUND,
) -> str:
    """The ``tfs`` value (URL-safe base64 without padding), byte-identical to Google's UI."""
    if not legs:
        raise ValueError("a tfs message needs at least one leg")
    out = varint_field(_F_UI_A, _UI_A_VALUE) + varint_field(_F_UI_B, _UI_B_VALUE)
    for leg in legs:
        out += bytes_field(_F_LEG, encode_leg(leg))
    out += varint_field(_F_PASSENGER, PASSENGER_ADULT) * max(adults, 1)
    out += varint_field(_F_SEAT, seat)
    out += varint_field(_F_UI_C, _UI_C_VALUE)
    out += bytes_field(_F_PRICE, varint_field(1, _UINT64_MASK))
    out += varint_field(_F_TRIP, trip)
    return base64.urlsafe_b64encode(out).decode("ascii").rstrip("=")


# --------------------------------------------------------------------------- decoding (debug/tests)


def decode_tfs(value: str) -> bytes:
    """Raw protobuf bytes of a ``tfs`` value (URL-safe or standard base64, padding optional)."""
    text = value.strip().replace("+", "-").replace("/", "_")
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _read_varint(data: bytes, pos: int) -> tuple[int, int]:
    value = shift = 0
    while True:
        if pos >= len(data):
            raise ValueError("truncated varint")
        byte = data[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        shift += 7
        if not byte & 0x80:
            return value, pos


def decode_message(data: bytes) -> list[tuple[int, int | bytes]]:
    """Flat ``(field, value)`` list of one protobuf message (varint and length-delimited only).

    Nested messages stay ``bytes``; decode them with another call. Raises ``ValueError`` on
    other wire types or truncated input.
    """
    fields: list[tuple[int, int | bytes]] = []
    pos = 0
    while pos < len(data):
        key, pos = _read_varint(data, pos)
        number, wire = key >> 3, key & 7
        if wire == _WIRE_VARINT:
            value, pos = _read_varint(data, pos)
            fields.append((number, value))
        elif wire == _WIRE_LEN:
            length, pos = _read_varint(data, pos)
            if pos + length > len(data):
                raise ValueError("truncated length-delimited field")
            fields.append((number, data[pos : pos + length]))
            pos += length
        else:
            raise ValueError(f"unsupported wire type {wire} (field {number})")
    return fields


# --------------------------------------------------------------------------- query -> tfs / URLs


def place_codes(code: str) -> tuple[str, ...]:
    """Places for a query code: metro codes expand to their airports; ``/m/`` ids pass through."""
    if code.startswith(_CITY_ID_PREFIX):
        return (code,)
    return tuple(airports_for(code))


def max_stops_value(query: FlightQuery) -> int | None:
    """Leg field 5 for the query: ``direct_only`` -> 0; 0..2 as given; otherwise any (``None``)."""
    if query.direct_only:
        return 0
    if query.max_stops is None or query.max_stops < 0 or query.max_stops > MAX_STOPS_FILTER:
        return None
    return query.max_stops


def query_legs(
    query: FlightQuery,
    *,
    outbound: Sequence[SelectedFlight] = (),
    inbound: Sequence[SelectedFlight] = (),
) -> list[TfsLeg]:
    """Outbound (and, for round trips, return) legs of ``query`` with optional selections."""
    origins, destinations = place_codes(query.origin), place_codes(query.destination)
    stops = max_stops_value(query)
    legs = [
        TfsLeg(query.depart_date.isoformat(), origins, destinations, stops, tuple(outbound))
    ]
    if query.return_date is not None:
        legs.append(
            TfsLeg(query.return_date.isoformat(), destinations, origins, stops, tuple(inbound))
        )
    return legs


def query_tfs(
    query: FlightQuery,
    *,
    outbound: Sequence[SelectedFlight] = (),
    inbound: Sequence[SelectedFlight] = (),
) -> str:
    """``tfs`` for ``query`` (passengers = adults, seat = cabin, trip type from return date)."""
    return encode_tfs(
        query_legs(query, outbound=outbound, inbound=inbound),
        adults=query.adults,
        seat=SEAT_CODES.get(query.cabin, SEAT_CODES["economy"]),
        trip=TRIP_ROUND if query.is_round_trip else TRIP_ONE_WAY,
    )


def _url(base: str, tfs: str, hl: str, currency: str, *, tfu: bool) -> str:
    params = {"tfs": tfs}
    if tfu:
        params["tfu"] = TFU_BEST
    params.update({"hl": hl, "gl": COUNTRY, "curr": currency.upper()})
    return f"{base}?{urlencode(params)}"


def search_url(
    query: FlightQuery,
    hl: str = MACHINE_LANGUAGE,
    *,
    outbound: Sequence[SelectedFlight] = (),
) -> str:
    """URL the provider fetches: ``/travel/flights?tfs=..&tfu=EgQIABABIgA&hl=..&gl=KR&curr=..``.

    With ``outbound`` selected (round trips) the page lists the return flights for it.
    """
    return _url(SEARCH_PAGE_URL, query_tfs(query, outbound=outbound), hl, query.currency, tfu=True)


def user_link(
    query: FlightQuery,
    hl: str = USER_LANGUAGE,
    *,
    outbound: Sequence[SelectedFlight] = (),
) -> str:
    """User-facing search link ``/travel/flights/search?tfs=..&hl=ko&gl=KR&curr=..``.

    With ``outbound`` selected it opens that outbound's return-flight list (one click from the
    booking options).
    """
    return _url(USER_SEARCH_URL, query_tfs(query, outbound=outbound), hl, query.currency, tfu=True)


def booking_link(
    query: FlightQuery,
    outbound: Sequence[SelectedFlight],
    inbound: Sequence[SelectedFlight] = (),
    hl: str = USER_LANGUAGE,
) -> str:
    """User-facing "Booking options" link; every leg of ``query`` must have selections."""
    if not outbound or (query.is_round_trip and not inbound):
        raise ValueError("a booking link needs selected flights on every leg")
    tfs = query_tfs(query, outbound=outbound, inbound=inbound)
    return _url(USER_BOOKING_URL, tfs, hl, query.currency, tfu=False)
