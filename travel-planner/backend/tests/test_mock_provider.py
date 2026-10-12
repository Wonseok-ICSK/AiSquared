from __future__ import annotations

from datetime import date

import pytest

from app.providers.flights import create_provider, get_provider_class, list_provider_classes
from app.providers.flights import mock as mock_module
from app.providers.flights.base import FlightProvider, FlightQuery, ProviderContext
from app.providers.flights.mock import MockProvider

ICN_NRT = FlightQuery(
    origin="ICN", destination="NRT", depart_date=date(2026, 11, 20), return_date=date(2026, 11, 24)
)


@pytest.fixture
def provider() -> FlightProvider:
    return create_provider("mock", ProviderContext(get_secret=lambda _name: None))


def test_registered(provider: FlightProvider) -> None:
    assert get_provider_class("mock") is MockProvider
    assert MockProvider in list_provider_classes()
    assert provider.status().configured is True
    assert MockProvider.supports_metro_codes is True


def test_deterministic(provider: FlightProvider) -> None:
    first = [o.to_dict() for o in provider.search_flights(ICN_NRT)]
    second = [o.to_dict() for o in provider.search_flights(ICN_NRT)]
    assert first == second


def test_offer_mix_and_prices(provider: FlightProvider) -> None:
    offers = provider.search_flights(ICN_NRT)
    assert 4 <= len(offers) <= 8
    stops = {o.outbound.stops for o in offers}
    assert stops == {0, 1}
    for o in offers:
        assert o.currency == "KRW" and o.provider == "mock"
        assert 180_000 <= o.price_per_person <= 650_000
        assert o.price_total == o.price_per_person
        assert o.inbound is not None and o.inbound.departure_time.startswith("2026-11-24")
        assert o.outbound.departure_time.startswith("2026-11-20")
        assert o.booking_url.startswith("https://www.google.com/travel/flights?")
        assert o.outbound.flight_numbers and o.offer_key().endswith("|ECONOMY")
        if o.outbound.stops:
            assert len(o.outbound.segments) == 2 and o.outbound.layovers


def test_day_salt_changes_prices_not_flights(
    provider: FlightProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mock_module, "_today", lambda: date(2026, 10, 1))
    day1 = provider.search_flights(ICN_NRT)
    monkeypatch.setattr(mock_module, "_today", lambda: date(2026, 10, 2))
    day2 = provider.search_flights(ICN_NRT)
    assert [o.offer_key() for o in day1] == [o.offer_key() for o in day2]
    assert [o.price_total for o in day1] != [o.price_total for o in day2]
    for a, b in zip(day1, day2, strict=True):
        assert abs(a.price_per_person - b.price_per_person) <= 0.12 * a.price_per_person


def test_direct_only_is_subset(provider: FlightProvider) -> None:
    all_keys = {o.offer_key() for o in provider.search_flights(ICN_NRT)}
    direct = provider.search_flights(
        FlightQuery("ICN", "NRT", date(2026, 11, 20), date(2026, 11, 24), direct_only=True)
    )
    assert direct and all(o.outbound.stops == 0 and o.inbound.stops == 0 for o in direct)
    assert {o.offer_key() for o in direct} <= all_keys


def test_metro_one_way_adults_and_currency(provider: FlightProvider) -> None:
    offers = provider.search_flights(
        FlightQuery("SEL", "TYO", date(2026, 11, 20), None, adults=2, currency="JPY")
    )
    assert {o.outbound.departure_airport for o in offers} <= {"ICN", "GMP"}
    assert {o.outbound.arrival_airport for o in offers} <= {"NRT", "HND"}
    for o in offers:
        assert o.inbound is None and o.currency == "JPY" and o.adults == 2
        assert o.price_total == pytest.approx(2 * o.price_per_person)
        assert 10_000 < o.price_per_person < 100_000


def test_default_get_price_matches_offer_key(provider: FlightProvider) -> None:
    offer = provider.search_flights(ICN_NRT)[0]
    assert provider.get_price(offer) == offer.price_per_person
    assert provider.get_booking_url(offer) == offer.booking_url


def test_mock_is_not_enabled_by_default() -> None:
    from app.providers.flights import default_provider_keys, resolve_provider_keys

    assert FlightProvider.enabled_by_default is True
    assert MockProvider.enabled_by_default is False
    assert "mock" not in default_provider_keys()
    assert default_provider_keys(["mock", "nope"]) == ["mock"]  # runtime default_providers
    assert resolve_provider_keys([], []) == default_provider_keys()
    assert resolve_provider_keys(["mock", "mock"], ["other"]) == ["mock"]  # trip list wins
