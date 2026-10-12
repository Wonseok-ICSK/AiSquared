from __future__ import annotations

import json
from datetime import date

from app.providers.flights.base import (
    FlightLeg,
    FlightOffer,
    FlightSegment,
    leg_key,
    normalize_flight_number,
    query_for_offer,
)


def leg(
    dep: str = "ICN",
    arr: str = "NRT",
    time: str = "2026-11-20T09:05",
    numbers: list[str] | None = None,
    airlines: list[str] | None = None,
) -> FlightLeg:
    return FlightLeg(
        departure_airport=dep,
        arrival_airport=arr,
        departure_time=time,
        arrival_time="2026-11-20T11:25",
        duration_minutes=140,
        stops=0,
        airlines=airlines if airlines is not None else ["Korean Air"],
        flight_numbers=numbers if numbers is not None else ["KE703"],
    )


def offer(outbound: FlightLeg, inbound: FlightLeg | None = None, cabin: str = "economy") -> FlightOffer:
    return FlightOffer(
        provider="mock",
        outbound=outbound,
        inbound=inbound,
        price_total=600000,
        price_per_person=300000,
        currency="KRW",
        cabin=cabin,  # type: ignore[arg-type]
        adults=2,
    )


def test_one_way_key() -> None:
    assert offer(leg()).offer_key() == "ICN-NRT-2026-11-20T09:05-KE703|-|ECONOMY"


def test_round_trip_key_with_connection() -> None:
    back = leg("NRT", "ICN", "2026-11-24T18:30", ["OZ105", "OZ8930"], ["Asiana Airlines"])
    key = offer(leg(), back, cabin="business").offer_key()
    assert key == "ICN-NRT-2026-11-20T09:05-KE703|NRT-ICN-2026-11-24T18:30-OZ105+OZ8930|BUSINESS"


def test_missing_flight_numbers_use_airlines() -> None:
    no_numbers = leg(numbers=[], airlines=["Jeju Air", "Peach"])
    assert leg_key(no_numbers) == "ICN-NRT-2026-11-20T09:05-JEJU AIR+PEACH"


def test_same_flight_from_different_providers_matches() -> None:
    a = offer(leg(numbers=["KE703"]))
    b = offer(leg(time="2026-11-20 09:05:00", numbers=["ke 0703"]))
    assert a.offer_key() == b.offer_key()
    assert a.offer_key() != offer(leg(), cabin="first").offer_key()


def test_normalize_flight_number() -> None:
    assert normalize_flight_number("ke 0703") == "KE703"
    assert normalize_flight_number("7C-1102") == "7C1102"
    assert normalize_flight_number("NH 7a") == "NH7A"
    assert normalize_flight_number("KAL703") == "KAL703"


def test_dict_roundtrip_is_json_safe() -> None:
    out = leg()
    out.segments = [
        FlightSegment("KE", "Korean Air", "KE703", "ICN", "NRT", "2026-11-20T09:05", "2026-11-20T11:25", 140)
    ]
    original = offer(out, leg("NRT", "ICN", "2026-11-24T18:30", ["KE704"]))
    original.raw = {"id": "x"}
    data = json.loads(json.dumps(original.to_dict()))
    restored = FlightOffer.from_dict(data)
    assert restored == original
    assert restored.offer_key() == original.offer_key()
    assert FlightLeg.from_dict({**out.to_dict(), "unknown": 1}) == out


def test_query_for_offer() -> None:
    query = query_for_offer(offer(leg(), leg("NRT", "ICN", "2026-11-24T18:30", ["KE704"])))
    assert (query.origin, query.destination) == ("ICN", "NRT")
    assert query.depart_date == date(2026, 11, 20) and query.return_date == date(2026, 11, 24)
    assert query.adults == 2 and query.is_round_trip
