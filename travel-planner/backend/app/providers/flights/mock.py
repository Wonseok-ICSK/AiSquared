"""Mock flight provider: deterministic fake offers for development and tests.

The same query always yields the same flights (seeded from route, dates and cabin); prices
drift by a few percent per local calendar day so monitoring shows changes. No network.
"""

from __future__ import annotations

import hashlib
import random
from datetime import date, datetime, timedelta
from urllib.parse import urlencode

from app.core.airports import airports_for, get_airport, haversine_km
from app.core.fx import static_rate
from app.providers.flights.base import (
    FlightLeg,
    FlightOffer,
    FlightProvider,
    FlightQuery,
    FlightSegment,
)
from app.providers.flights.registry import register
from app.timeutil import now_local

_AIRLINES: tuple[tuple[str, str, bool], ...] = (  # (code, name, full_service)
    ("KE", "Korean Air", True),
    ("OZ", "Asiana Airlines", True),
    ("JL", "Japan Airlines", True),
    ("NH", "ANA", True),
    ("7C", "Jeju Air", False),
    ("LJ", "Jin Air", False),
    ("TW", "T'way Air", False),
    ("BX", "Air Busan", False),
    ("ZE", "Eastar Jet", False),
)
_HUBS = ("PVG", "TPE", "HKG", "FUK", "KIX", "PEK", "MNL", "ICN", "NRT")
_CABIN_FACTOR = {"economy": 1.0, "premium_economy": 1.7, "business": 3.2, "first": 5.5}
_ZERO_DECIMAL = {"KRW", "JPY", "VND", "IDR", "TWD", "CLP", "HUF"}
_DEFAULT_DISTANCE_KM = 1500.0
_CRUISE_KMH = 780.0
_TAXI_MINUTES = 35
_BASE_PRICE_KRW = 140_000.0
_PRICE_PER_KM_KRW = 150.0
_ONE_WAY_FACTOR = 0.6
_ONE_STOP_FACTOR = 0.85


def _today() -> date:
    """Local calendar day used as the price 'day salt' (monkeypatched in tests)."""
    return now_local().date()


def _seed(query: FlightQuery) -> int:
    text = f"{query.origin}|{query.destination}|{query.depart_date}|{query.return_date}|{query.cabin}"
    return int.from_bytes(hashlib.sha256(text.upper().encode()).digest()[:8], "big")


def _distance_km(a: str, b: str) -> float:
    first, second = get_airport(a), get_airport(b)
    if first is None or second is None:
        return _DEFAULT_DISTANCE_KM
    return haversine_km(first.lat, first.lon, second.lat, second.lon)


def _flight_minutes(a: str, b: str) -> int:
    return int(_distance_km(a, b) / _CRUISE_KMH * 60) + _TAXI_MINUTES


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M")


def _round_price(value: float, currency: str) -> float:
    if currency == "KRW":
        return float(round(value, -2))
    return float(round(value)) if currency in _ZERO_DECIMAL else round(value, 2)


def _make_leg(
    rng: random.Random, dep: str, arr: str, day: date, airline: tuple[str, str, bool], stops: int
) -> FlightLeg:
    code, name, _ = airline
    start = datetime.combine(day, datetime.min.time()) + timedelta(
        hours=rng.randint(6, 21), minutes=5 * rng.randint(0, 11)
    )
    if stops == 0:
        hops = [(dep, arr)]
    else:
        hub = rng.choice([h for h in _HUBS if h not in (dep, arr)])
        hops = [(dep, hub), (hub, arr)]
    segments: list[FlightSegment] = []
    clock = start
    for index, (src, dst) in enumerate(hops):
        if index:
            clock += timedelta(minutes=rng.randint(90, 240))  # layover
        minutes = _flight_minutes(src, dst)
        segments.append(
            FlightSegment(
                carrier_code=code,
                carrier_name=name,
                flight_number=f"{code}{rng.randint(100, 999)}",
                departure_airport=src,
                arrival_airport=dst,
                departure_time=_fmt(clock),
                arrival_time=_fmt(clock + timedelta(minutes=minutes)),
                duration_minutes=minutes,
            )
        )
        clock += timedelta(minutes=minutes)
    return FlightLeg(
        departure_airport=dep,
        arrival_airport=arr,
        departure_time=_fmt(start),
        arrival_time=_fmt(clock),
        duration_minutes=int((clock - start).total_seconds() // 60),
        stops=stops,
        airlines=[name],
        flight_numbers=[s.flight_number for s in segments if s.flight_number],
        segments=segments,
        layovers=[dst for _, dst in hops[:-1]],
    )


def _booking_url(query: FlightQuery, dep: str, arr: str, currency: str) -> str:
    text = f"Flights from {dep} to {arr} on {query.depart_date.isoformat()}"
    if query.return_date:
        text += f" through {query.return_date.isoformat()}"
    return "https://www.google.com/travel/flights?" + urlencode(
        {"q": text, "curr": currency, "hl": "ko"}
    )


@register
class MockProvider(FlightProvider):
    """Deterministic fake data for development and tests (no network, no key)."""

    key = "mock"
    display_name = "Mock (demo data)"
    description = "Deterministic fake offers for development and testing. Not real prices."
    supports_metro_codes = True
    enabled_by_default = False  # never searched unless a trip / default_providers names it

    def search_flights(self, query: FlightQuery) -> list[FlightOffer]:
        """Generate 4-8 stable offers (direct and 1-stop) for the query."""
        seed = _seed(query)
        rng = random.Random(seed)
        day_rng = random.Random(f"{seed}:{_today().isoformat()}")
        origins, destinations = airports_for(query.origin), airports_for(query.destination)
        count = rng.randint(4, 8)
        one_stop = set(rng.sample(range(1, count), rng.randint(1, max(1, count // 3))))
        stops_allowed = not query.direct_only and (query.max_stops is None or query.max_stops >= 1)

        rate = static_rate("KRW", query.currency)
        currency = query.currency if rate is not None else "KRW"
        offers: list[FlightOffer] = []
        for index in range(count):
            stops = 1 if index in one_stop else 0
            airline = rng.choice(_AIRLINES)
            dep = origins[index % len(origins)]
            arr = destinations[rng.randrange(len(destinations))]
            outbound = _make_leg(rng, dep, arr, query.depart_date, airline, stops)
            inbound = (
                _make_leg(rng, arr, dep, query.return_date, airline, stops)
                if query.return_date
                else None
            )
            per_person_krw = (
                (_BASE_PRICE_KRW + _distance_km(dep, arr) * _PRICE_PER_KM_KRW)
                * (rng.uniform(1.15, 1.35) if airline[2] else rng.uniform(0.70, 0.85))
                * (_ONE_STOP_FACTOR if stops else 1.0)
                * (1.0 if inbound else _ONE_WAY_FACTOR)
                * _CABIN_FACTOR[query.cabin]
                * day_rng.uniform(0.95, 1.05)
            )
            if stops and not stops_allowed:
                continue
            per_person = _round_price(per_person_krw * (rate or 1.0), currency)
            offers.append(
                FlightOffer(
                    provider=self.key,
                    outbound=outbound,
                    inbound=inbound,
                    price_total=_round_price(per_person * query.adults, currency),
                    price_per_person=per_person,
                    currency=currency,
                    cabin=query.cabin,
                    adults=query.adults,
                    fare_type="Standard" if airline[2] else "Basic",
                    baggage="1 x 23kg checked bag" if airline[2] else "Cabin bag only",
                    seller=airline[1],
                    booking_url=_booking_url(query, dep, arr, currency),
                    provider_offer_id=f"mock-{seed % 10**8:08d}-{index}",
                    raw={"mock": True, "index": index},
                )
            )
        return offers
