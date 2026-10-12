"""Test helpers for the monitoring module tests (search / monitoring / alerts / scheduler).

Fake providers are registered per test by patching the registry dict with ``monkeypatch``
so they never leak into other tests' provider listings.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import date, timedelta
from typing import Any

import pytest
from sqlalchemy.orm import Session

from app.models import Trip
from app.providers.flights import registry
from app.providers.flights.base import (
    FlightLeg,
    FlightOffer,
    FlightProvider,
    FlightQuery,
    ProviderStatus,
)

Handler = Callable[[FlightQuery], list[FlightOffer]]

TRIP_DEFAULTS: dict[str, Any] = {
    "name": "Tokyo autumn",
    "origin": "ICN",
    "destination": "NRT",
    "trip_type": "round_trip",
    "depart_date": date(2026, 11, 20),
    "return_date": date(2026, 11, 24),
    "adults": 1,
    "cabin": "economy",
    "direct_only": False,
    "max_stops": None,
    "price_basis": "per_person",
    "currency": "KRW",
    "flex_depart_days": 0,
    "flex_return_days": 0,
    "include_nearby_airports": False,
    "schedule_mode": "times",
    "active": True,
}
TODAY = date(2026, 10, 6)


def make_trip(**overrides: Any) -> Trip:
    """A transient trip with every field the search code reads set explicitly."""
    values = {**TRIP_DEFAULTS, **overrides}
    values.setdefault("alt_destinations", [])
    values.setdefault("providers", [])
    values.setdefault("schedule_times", ["08:30", "17:30"])
    return Trip(**values)


def add_trip(db: Session, **overrides: Any) -> Trip:
    """A persisted (committed) trip."""
    trip = make_trip(**overrides)
    db.add(trip)
    db.commit()
    return trip


def make_leg(
    dep: str = "ICN",
    arr: str = "NRT",
    day: date = date(2026, 11, 20),
    depart: str = "09:00",
    arrive: str = "11:30",
    stops: int = 0,
    numbers: tuple[str, ...] = ("KE703",),
    airlines: tuple[str, ...] = ("Korean Air",),
    minutes: int | None = 150,
) -> FlightLeg:
    """A flight leg on ``day`` (arrival on the same day)."""
    return FlightLeg(
        departure_airport=dep,
        arrival_airport=arr,
        departure_time=f"{day.isoformat()}T{depart}",
        arrival_time=f"{day.isoformat()}T{arrive}",
        duration_minutes=minutes,
        stops=stops,
        airlines=list(airlines),
        flight_numbers=list(numbers),
        layovers=["PVG"] * stops,
    )


def make_offer(
    price: float = 300_000,
    *,
    provider: str = "fake",
    adults: int = 1,
    currency: str = "KRW",
    outbound: FlightLeg | None = None,
    inbound: FlightLeg | None | str = "default",
    return_date: str | None = None,
    raw: dict[str, Any] | None = None,
) -> FlightOffer:
    """An offer; ``inbound="default"`` = a direct KE704 return on 2026-11-24."""
    if inbound == "default":
        inbound = make_leg("NRT", "ICN", date(2026, 11, 24), "13:00", "15:30", numbers=("KE704",))
    return FlightOffer(
        provider=provider,
        outbound=outbound or make_leg(),
        inbound=inbound,  # type: ignore[arg-type]
        price_total=price * adults,
        price_per_person=price,
        currency=currency,
        cabin="economy",
        adults=adults,
        booking_url="https://example.test/book",
        return_date=return_date,
        raw=raw or {},
    )


def offers_for_query(query: FlightQuery, prices: list[float], provider: str) -> list[FlightOffer]:
    """Direct offers for ``query``'s first airports, one per price (distinct flights)."""
    origin = query.origin if len(query.origin) == 3 else "ICN"
    destination = query.destination
    offers = []
    for index, price in enumerate(prices):
        out = make_leg(
            origin, destination, query.depart_date, f"{8 + index:02d}:00", f"{10 + index:02d}:30",
            numbers=(f"KE{700 + index}",),
        )
        inbound = (
            make_leg(
                destination, origin, query.return_date, f"{12 + index:02d}:00",
                f"{14 + index:02d}:30", numbers=(f"KE{800 + index}",),
            )
            if query.return_date
            else None
        )
        offer = make_offer(price, provider=provider, adults=query.adults, outbound=out,
                           inbound=inbound, currency=query.currency)
        offers.append(offer)
    return offers


def fake_provider_class(
    key: str,
    handler: Handler,
    *,
    metro: bool = True,
    max_queries: int = 12,
    configured: bool = True,
    uses_browser: bool = False,
    details: Callable[[FlightOffer], FlightOffer] | None = None,
) -> type[FlightProvider]:
    """A provider class whose ``search_flights`` delegates to ``handler``."""

    def search_flights(self: FlightProvider, query: FlightQuery) -> list[FlightOffer]:
        return handler(query)

    def status(self: FlightProvider) -> ProviderStatus:
        return ProviderStatus(configured=configured, message="" if configured else "needs key")

    attrs: dict[str, Any] = {
        "key": key,
        "display_name": key.title(),
        "supports_metro_codes": metro,
        "max_queries_per_run": max_queries,
        "enabled_by_default": False,
        "uses_browser": uses_browser,
        "search_flights": search_flights,
        "status": status,
    }
    if details is not None:
        attrs["get_flight_details"] = lambda self, offer: details(offer)
    return type(f"Fake_{key}", (FlightProvider,), attrs)


@pytest.fixture
def register(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[type[FlightProvider]], None]]:
    """``register(cls)`` adds a provider class to the registry for this test only."""

    def _register(cls: type[FlightProvider]) -> None:
        monkeypatch.setitem(registry._registry._classes, cls.key, cls)

    yield _register


def days(n: int) -> timedelta:
    """Shorthand for ``timedelta(days=n)``."""
    return timedelta(days=n)
