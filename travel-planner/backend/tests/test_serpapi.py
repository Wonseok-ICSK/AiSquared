"""SerpApi Google Flights provider. Fixture shapes follow serpapi.com/google-flights-api docs
(values are synthetic but realistic: SEL->TYO round trip, 2 adults, KRW)."""

from __future__ import annotations

import copy
import json
import logging
import os
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from app.providers.flights import default_provider_keys, get_provider_class
from app.providers.flights import serpapi as serpapi_module
from app.providers.flights.base import (
    FlightOffer,
    FlightQuery,
    ProviderBlocked,
    ProviderContext,
    ProviderError,
    ProviderNotConfigured,
)
from app.providers.flights.serpapi import (
    ACCOUNT_URL,
    EXHAUSTED_PAUSE,
    MAX_PAUSE,
    RATE_LIMIT_PAUSE,
    SERPAPI_URL,
    SerpApiProvider,
    active_pause,
    build_params,
    check_response,
    clear_quota_pauses,
    local_iso,
    parse_offers,
    pick_return_option,
    quota_pause,
    return_options,
    round_trip_match_key,
    stops_param,
)

FIXTURES = Path(__file__).parent / "fixtures" / "serpapi"
API_KEY = "test-serpapi-key-0123456789abcdef"
# Account API shape (serpapi.com/account-api); it echoes the key, which must never leak.
ACCOUNT_WITH_SEARCHES = {
    "api_key": API_KEY,
    "account_email": "someone@example.com",
    "plan_name": "Free Plan",
    "plan_renewal_date": "2026-11-06",
    "searches_per_month": 250,
    "plan_searches_left": 180,
    "extra_credits": 0,
    "total_searches_left": 180,
    "this_hour_searches": 50,
    "account_rate_limit_per_hour": 50,
}
ACCOUNT_EXHAUSTED = {**ACCOUNT_WITH_SEARCHES, "plan_searches_left": 0, "total_searches_left": 0}
SEL_TYO = FlightQuery("SEL", "TYO", date(2026, 11, 20), date(2026, 11, 23), adults=2)
GFL_URL_PREFIX = "https://www.google.com/travel/flights?hl=en&gl=kr&curr=KRW&tfs="
# Read at import time: the autouse ``settings`` fixture removes SERPAPI_API_KEY from the env.
LIVE_KEY = os.environ.get("SERPAPI_API_KEY_LIVE") or os.environ.get("SERPAPI_API_KEY")


@pytest.fixture(autouse=True)
def _no_quota_pauses() -> Iterator[None]:
    clear_quota_pauses()
    yield
    clear_quota_pauses()


def load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def make_provider(
    key: str | None = API_KEY, options: dict[str, Any] | None = None, timeout: float = 30.0
) -> SerpApiProvider:
    secrets = {"serpapi": key}
    ctx = ProviderContext(
        get_secret=lambda name: secrets.get(name), http_timeout=timeout, options=options or {}
    )
    return SerpApiProvider(ctx)


def offers_by_number(offers: list[FlightOffer]) -> dict[str, FlightOffer]:
    return {"+".join(o.outbound.flight_numbers): o for o in offers}


def one_way_response() -> dict[str, Any]:
    data = load("round_trip_krw.json")
    data["search_parameters"].update(type="2", adults=1)
    data["search_parameters"].pop("return_date")
    data["best_flights"] = data["best_flights"][:1]
    data["other_flights"] = []
    item = data["best_flights"][0]
    item.update(price=138700, type="One way", booking_token=item.pop("departure_token"))
    return data


# --------------------------------------------------------------------------- registration


def test_registered_with_contract_attributes() -> None:
    assert get_provider_class("serpapi") is SerpApiProvider
    assert SerpApiProvider.requires_api_key is True
    assert SerpApiProvider.api_key_name == "serpapi"
    assert SerpApiProvider.enabled_by_default is True
    assert SerpApiProvider.supports_metro_codes is True
    assert SerpApiProvider.max_queries_per_run == 1
    assert SerpApiProvider.uses_browser is False
    assert "serpapi" in default_provider_keys()


def test_status_reflects_api_key() -> None:
    assert make_provider().status().configured is True
    for missing in (None, "", "   "):
        status = make_provider(key=missing).status()
        assert status.configured is False
        assert "SerpApi API key" in status.message


# --------------------------------------------------------------------------- request params


def test_build_params_round_trip_expands_metro_codes() -> None:
    assert build_params(SEL_TYO) == {
        "engine": "google_flights",
        "departure_id": "ICN,GMP",
        "arrival_id": "NRT,HND",
        "outbound_date": "2026-11-20",
        "return_date": "2026-11-23",
        "type": 1,
        "travel_class": 1,
        "adults": 2,
        "stops": 0,
        "currency": "KRW",
        "hl": "en",
        "gl": "kr",
    }


def test_build_params_one_way_cabins_and_deep_search() -> None:
    query = FlightQuery("icn", "KIX", date(2026, 12, 1), None, cabin="business", currency="jpy")
    params = build_params(query, deep_search=True)
    assert params["type"] == 2 and "return_date" not in params
    assert params["departure_id"] == "ICN" and params["arrival_id"] == "KIX"
    assert params["travel_class"] == 3 and params["currency"] == "JPY"
    assert params["deep_search"] == "true"
    for cabin, value in {"economy": 1, "premium_economy": 2, "business": 3, "first": 4}.items():
        assert build_params(FlightQuery("ICN", "NRT", date(2026, 12, 1), None, cabin=cabin))[
            "travel_class"
        ] == value
    assert "api_key" not in params


@pytest.mark.parametrize(
    ("direct_only", "max_stops", "expected"),
    [
        (False, None, 0),
        (True, None, 1),
        (True, 2, 1),  # direct_only wins
        (False, 0, 1),
        (False, 1, 2),
        (False, 2, 3),
        (False, 3, 0),  # SerpApi cannot express "3 stops or fewer" -> any
        (False, -1, 0),
    ],
)
def test_stops_param(direct_only: bool, max_stops: int | None, expected: int) -> None:
    query = FlightQuery(
        "ICN", "NRT", date(2026, 11, 20), None, direct_only=direct_only, max_stops=max_stops
    )
    assert stops_param(query) == expected


def test_local_iso() -> None:
    assert local_iso("2026-11-20 09:05") == "2026-11-20T09:05"
    assert local_iso("2026-11-20 9:05") == "2026-11-20T09:05"
    assert local_iso("2026-11-20T23:59:00") == "2026-11-20T23:59"
    assert local_iso("") is None and local_iso(None) is None and local_iso("tomorrow") is None


# --------------------------------------------------------------------------- search (respx)


def test_search_round_trip_parses_fixture() -> None:
    provider = make_provider(timeout=12.5)
    with respx.mock as router:
        route = router.get(SERPAPI_URL).mock(
            return_value=httpx.Response(200, json=load("round_trip_krw.json"))
        )
        offers = provider.search_flights(SEL_TYO)

    request = route.calls.last.request
    params = dict(request.url.params)
    assert params == {
        **{k: str(v) for k, v in build_params(SEL_TYO).items()},
        "api_key": API_KEY,
    }
    assert request.extensions["timeout"]["read"] == 12.5

    # 7 rows: the unpriced ZIPAIR row is skipped; best_flights first.
    assert len(offers) == 6
    by_number = offers_by_number(offers)
    assert "ZG42" not in by_number
    assert [o.outbound.flight_numbers[0] for o in offers[:3]] == ["MM702", "YP731", "KE2101"]

    peach = by_number["MM702"]
    assert peach.provider == "serpapi"
    assert peach.price_total == 652872 and peach.price_per_person == 326436
    assert peach.currency == "KRW" and peach.adults == 2 and peach.cabin == "economy"
    assert peach.inbound is None and peach.return_date == "2026-11-23"
    out = peach.outbound
    assert (out.departure_airport, out.arrival_airport) == ("ICN", "NRT")
    assert (out.departure_time, out.arrival_time) == ("2026-11-20T10:55", "2026-11-20T13:20")
    assert out.duration_minutes == 145 and out.stops == 0 and out.layovers == []
    assert out.airlines == ["Peach"]
    segment = out.segments[0]
    assert segment.carrier_code == "MM" and segment.carrier_name == "Peach"
    assert segment.flight_number == "MM702" and segment.aircraft == "Airbus A320"
    assert peach.booking_url == peach.source_url
    assert peach.booking_url is not None and peach.booking_url.startswith(GFL_URL_PREFIX)
    assert peach.baggage == "Checked baggage for a fee; Bag and fare conditions depend on the return flight"
    assert peach.offer_key() == "ICN-NRT-2026-11-20T10:55-MM702|RT:2026-11-23|ECONOMY"

    raw = peach.raw
    assert raw["source"] == "best_flights" and raw["type"] == "Round trip"
    assert raw["departure_token"].startswith("WyJ")
    assert raw["price_insights"] == {
        "lowest_price": 541200,
        "price_level": "typical",
        "typical_price_range": [580000, 820000],
    }
    assert raw["search_id"] == "6703a1f0c9d14e2a8b5f3e21"
    assert raw["request"]["departure_id"] == "ICN,GMP" and "api_key" not in raw["request"]
    json.dumps(peach.to_dict())  # JSON-safe

    connecting = by_number["MU5034+MU523"]
    assert connecting.raw["source"] == "other_flights"
    # the cheapest offer of the search carries the price history (once per search)
    assert len(connecting.raw["price_insights"]["price_history"]) == 9
    assert [o for o in offers if "price_history" in o.raw["price_insights"]] == [connecting]
    leg = connecting.outbound
    assert leg.stops == 1 and leg.layovers == ["PVG"] and leg.duration_minutes == 480
    assert leg.arrival_airport == "NRT" and leg.arrival_time == "2026-11-20T17:40"
    assert [s.arrival_airport for s in leg.segments] == ["PVG", "NRT"]
    assert leg.airlines == ["China Eastern"]

    gimpo = by_number["KE2101"]
    assert (gimpo.outbound.departure_airport, gimpo.outbound.arrival_airport) == ("GMP", "HND")
    assert gimpo.baggage is None
    assert by_number["KE5705"].outbound.segments[0].operating_carrier == "Jin Air"
    assert by_number["7C1102"].outbound.segments[0].carrier_code == "7C"


def test_search_one_way() -> None:
    query = FlightQuery("ICN", "NRT", date(2026, 11, 20), None)
    with respx.mock as router:
        route = router.get(SERPAPI_URL).mock(
            return_value=httpx.Response(200, json=one_way_response())
        )
        offers = make_provider().search_flights(query)
    params = route.calls.last.request.url.params
    assert params["type"] == "2" and "return_date" not in params
    assert len(offers) == 1
    offer = offers[0]
    assert offer.inbound is None and offer.return_date is None
    assert offer.price_total == offer.price_per_person == 138700
    assert offer.raw["booking_token"] and "departure_token" not in offer.raw
    assert offer.offer_key() == "ICN-NRT-2026-11-20T10:55-MM702|-|ECONOMY"


def test_parse_offers_dedupes_keeps_cheapest_and_skips_incomplete() -> None:
    data = load("round_trip_krw.json")
    peach, premia, korean = data["best_flights"]
    cheaper = copy.deepcopy(peach)
    cheaper["price"] = 600000
    del premia["flights"][0]["departure_airport"]["time"]
    korean["price"] = True  # not a number
    data["best_flights"] = [peach, korean]
    data["other_flights"] = [cheaper, premia, {"price": 1000, "flights": []}, "junk"]
    offers = parse_offers(data, SEL_TYO)
    assert [(o.outbound.flight_numbers[0], o.price_total) for o in offers] == [("MM702", 600000)]


def test_parse_offers_without_metadata_url_or_currency() -> None:
    data = one_way_response()
    del data["search_metadata"]["google_flights_url"]
    del data["search_parameters"]["currency"]
    del data["price_insights"]
    query = FlightQuery("SEL", "TYO", date(2026, 11, 20), None, currency="usd")
    offer = parse_offers(data, query)[0]
    assert offer.currency == "USD"
    assert offer.booking_url is not None
    assert offer.booking_url.startswith("https://www.google.com/travel/flights?q=Flights+from+ICN")
    assert "price_insights" not in offer.raw


def test_price_history_is_trimmed_and_stored_once() -> None:
    data = load("round_trip_krw.json")
    # Like the docs example: ~2 months of daily points.
    data["price_insights"]["price_history"] = [
        [1_691_013_600 + i * 86_400, 600_000 + i] for i in range(61)
    ]
    offers = parse_offers(data, SEL_TYO)
    with_history = [o for o in offers if "price_history" in o.raw["price_insights"]]
    assert len(with_history) == 1
    cheapest = with_history[0]
    assert cheapest.price_total == min(o.price_total for o in offers) == 541200
    history = cheapest.raw["price_insights"]["price_history"]
    assert len(history) == serpapi_module.PRICE_HISTORY_MAX_POINTS == 30
    assert history[-1] == [1_691_013_600 + 60 * 86_400, 600_060]
    for offer in offers:
        if offer is not cheapest:
            assert set(offer.raw["price_insights"]) == {
                "lowest_price",
                "price_level",
                "typical_price_range",
            }
            assert len(json.dumps(offer.raw["price_insights"])) < 120
    # each offer owns its copy (no shared mutable state between persisted rows)
    offers[0].raw["price_insights"]["typical_price_range"].append(1)
    assert offers[1].raw["price_insights"]["typical_price_range"] == [580000, 820000]
    assert data["price_insights"]["typical_price_range"] == [580000, 820000]


def test_price_insights_without_history_or_summary() -> None:
    data = load("round_trip_krw.json")
    del data["price_insights"]["price_history"]
    offers = parse_offers(data, SEL_TYO)
    assert all("price_history" not in o.raw["price_insights"] for o in offers)
    data["price_insights"] = {"price_history": [[1_700_000_000, 1]]}
    offers = parse_offers(data, SEL_TYO)
    holders = [o for o in offers if "price_insights" in o.raw]
    assert len(holders) == 1 and holders[0].raw["price_insights"] == {
        "price_history": [[1_700_000_000, 1]]
    }


# --------------------------------------------------------------------------- errors


def test_missing_key_raises_without_http_call() -> None:
    with respx.mock(assert_all_called=False) as router:
        route = router.get(SERPAPI_URL)
        with pytest.raises(ProviderNotConfigured):
            make_provider(key=None).search_flights(SEL_TYO)
        assert route.call_count == 0


@pytest.mark.parametrize(
    ("status", "body", "exc_type", "fragment"),
    [
        (
            401,
            {"error": "Invalid API key. Your API key should be here: https://serpapi.com/manage-api-key"},
            ProviderNotConfigured,
            "Invalid API key",
        ),
        (403, {"error": "This account has been deleted."}, ProviderNotConfigured, "deleted"),
        (429, {"error": "Your account has run out of searches."}, ProviderBlocked, "run out"),
        (400, {"error": "Missing `outbound_date` parameter."}, ProviderError, "outbound_date"),
        (
            200,
            {"search_metadata": {"status": "Error"}, "error": "We couldn't get valid results for this search."},
            ProviderError,
            "valid results",
        ),
        (503, {"error": "Something went wrong."}, ProviderError, "HTTP 503"),
        (200, {"search_metadata": {"status": "Error"}}, ProviderError, "status = Error"),
    ],
)
def test_error_mapping(
    status: int, body: dict[str, Any], exc_type: type[Exception], fragment: str
) -> None:
    with respx.mock(assert_all_called=False) as router:
        router.get(SERPAPI_URL).mock(return_value=httpx.Response(status, json=body))
        account = router.get(ACCOUNT_URL).mock(
            return_value=httpx.Response(200, json=ACCOUNT_WITH_SEARCHES)
        )
        with pytest.raises(exc_type) as info:
            make_provider().search_flights(SEL_TYO)
    assert fragment in str(info.value)
    if exc_type is ProviderError:
        assert type(info.value) is ProviderError
    # only a 429 consults the (free) Account API
    assert account.call_count == (1 if status == 429 else 0)


def test_no_results_message_means_empty_list() -> None:
    body = {
        "search_metadata": {"id": "x", "status": "Success"},
        "error": "Google Flights hasn't returned any results for this query.",
    }
    with respx.mock as router:
        router.get(SERPAPI_URL).mock(return_value=httpx.Response(200, json=body))
        assert make_provider().search_flights(SEL_TYO) == []
    assert check_response(200, body) is True
    assert check_response(200, load("round_trip_krw.json")) is False


def test_non_json_and_transport_errors() -> None:
    provider = make_provider()
    with respx.mock as router:
        route = router.get(SERPAPI_URL)
        route.mock(return_value=httpx.Response(502, text="<html>Bad gateway</html>"))
        with pytest.raises(ProviderError, match="non-JSON"):
            provider.search_flights(SEL_TYO)
        route.mock(return_value=httpx.Response(200, json=["unexpected"]))
        with pytest.raises(ProviderError, match="unexpected JSON"):
            provider.search_flights(SEL_TYO)
        route.mock(side_effect=httpx.ReadTimeout("timed out"))
        with pytest.raises(ProviderError, match="timed out after 30 s"):
            provider.search_flights(SEL_TYO)
        route.mock(side_effect=httpx.ConnectError("getaddrinfo failed"))
        with pytest.raises(ProviderError, match="request failed: getaddrinfo failed"):
            provider.search_flights(SEL_TYO)


def test_api_key_never_logged_or_leaked(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    provider = make_provider()
    with respx.mock as router:
        route = router.get(SERPAPI_URL)
        route.mock(return_value=httpx.Response(200, json=load("round_trip_krw.json")))
        offers = provider.search_flights(SEL_TYO)
        route.mock(return_value=httpx.Response(400, json={"error": f"Bad key {API_KEY} given"}))
        with pytest.raises(ProviderError) as info:
            provider.search_flights(SEL_TYO)
        route.mock(side_effect=httpx.ConnectError(f"failed for ?api_key={API_KEY}"))
        with pytest.raises(ProviderError) as transport:
            provider.search_flights(SEL_TYO)
    assert API_KEY not in str(info.value) and "***" in str(info.value)
    assert API_KEY not in str(transport.value)
    assert transport.value.__cause__ is None and transport.value.__suppress_context__
    assert API_KEY not in caplog.text
    assert "api_key=***" in caplog.text  # httpx INFO request log, masked by our filter
    assert "SerpApi ICN,GMP->NRT,HND 2026-11-20/2026-11-23: 6 offers" in caplog.text
    assert all(API_KEY not in json.dumps(o.to_dict()) for o in offers)


# --------------------------------------------------------------------------- details / price


def _peach_offer(provider: SerpApiProvider) -> FlightOffer:
    with respx.mock as router:
        router.get(SERPAPI_URL).mock(
            return_value=httpx.Response(200, json=load("round_trip_krw.json"))
        )
        return offers_by_number(provider.search_flights(SEL_TYO))["MM702"]


def test_get_flight_details_fills_inbound_via_departure_token() -> None:
    provider = make_provider()
    offer = _peach_offer(provider)
    original = copy.deepcopy(offer.to_dict())
    with respx.mock as router:
        route = router.get(SERPAPI_URL).mock(
            return_value=httpx.Response(200, json=load("return_flights_krw.json"))
        )
        detailed = provider.get_flight_details(offer)
    params = dict(route.calls.last.request.url.params)
    assert params["departure_token"] == offer.raw["departure_token"]
    assert params["departure_id"] == "ICN,GMP" and params["return_date"] == "2026-11-23"
    assert params["api_key"] == API_KEY

    assert offer.to_dict() == original  # input not mutated
    inbound = detailed.inbound
    assert inbound is not None
    # exact round-trip price match (652872) wins over list order
    assert inbound.flight_numbers == ["MM701"]
    assert (inbound.departure_airport, inbound.arrival_airport) == ("NRT", "ICN")
    assert inbound.departure_time == "2026-11-23T14:20"
    assert detailed.raw["booking_token"] != offer.raw.get("booking_token")
    assert detailed.raw["booking_token"].startswith("WyJ")
    assert detailed.price_total == offer.price_total
    assert detailed.price_per_person == offer.price_per_person
    assert detailed.offer_key() == (
        "ICN-NRT-2026-11-20T10:55-MM702|NRT-ICN-2026-11-23T14:20-MM701|ECONOMY"
    )


def test_get_flight_details_keeps_offer_when_listed_fare_is_gone(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No return flight priced at the listed total -> no inbound attached (no price mismatch)."""
    caplog.set_level(logging.INFO, logger=serpapi_module.__name__)
    provider = make_provider()
    offer = _peach_offer(provider)
    returns = load("return_flights_krw.json")
    returns["best_flights"] = returns["best_flights"][:1]  # drop MM701 (652872)
    with respx.mock as router:
        route = router.get(SERPAPI_URL).mock(return_value=httpx.Response(200, json=returns))
        detailed = provider.get_flight_details(offer)
    assert route.call_count == 1
    assert detailed is offer and detailed.inbound is None
    assert "no longer offered (return options now from 668872)" in caplog.text


def test_pick_return_option_requires_exact_total() -> None:
    options = return_options(load("return_flights_krw.json"))
    assert [(o.leg.flight_numbers[0], o.price) for o in options] == [
        ("MM703", 668872),
        ("MM701", 652872),
        ("MM705", 684872),
    ]
    picked = pick_return_option(options, 652872.0)
    assert picked is not None and picked.leg.flight_numbers == ["MM701"]
    assert pick_return_option(options, 652872.2) is picked  # float noise tolerated
    assert pick_return_option(options, 541200) is None  # no fallback to the cheapest
    assert pick_return_option([], 652872) is None
    assert return_options({"best_flights": [{"price": 5, "flights": []}, "junk"]}) == []


def test_get_flight_details_is_best_effort() -> None:
    provider = make_provider()
    offer = _peach_offer(provider)
    with respx.mock as router:
        route = router.get(SERPAPI_URL)
        router.get(ACCOUNT_URL).mock(return_value=httpx.Response(200, json=ACCOUNT_WITH_SEARCHES))
        route.mock(return_value=httpx.Response(429, json={"error": "Hourly limit"}))
        assert provider.get_flight_details(offer) is offer
        route.mock(
            return_value=httpx.Response(
                200, json={"error": "Google Flights hasn't returned any results for this query."}
            )
        )
        assert provider.get_flight_details(offer) is offer
        assert route.call_count == 2

    one_way = FlightOffer.from_dict({**offer.to_dict(), "return_date": None})
    no_token = FlightOffer.from_dict({**offer.to_dict(), "raw": {}})
    with respx.mock(assert_all_called=False) as router:
        route = router.get(SERPAPI_URL)
        assert provider.get_flight_details(one_way) is one_way
        assert provider.get_flight_details(no_token) is no_token
        assert route.call_count == 0


def test_get_price_re_searches() -> None:
    provider = make_provider()
    offer = _peach_offer(provider)
    with respx.mock as router:
        route = router.get(SERPAPI_URL).mock(
            return_value=httpx.Response(200, json=load("round_trip_krw.json"))
        )
        assert provider.get_price(offer) == 326436
        assert route.call_count == 1
        gone = load("round_trip_krw.json")
        gone["best_flights"] = gone["best_flights"][1:]  # MM702 no longer listed
        route.mock(return_value=httpx.Response(200, json=gone))
        assert provider.get_price(offer) is None
    params = route.calls.last.request.url.params
    assert params["departure_id"] == "ICN" and params["arrival_id"] == "NRT"
    assert params["return_date"] == "2026-11-23" and "departure_token" not in params
    assert provider.get_booking_url(offer) == offer.booking_url


def _detailed_peach(provider: SerpApiProvider) -> FlightOffer:
    offer = _peach_offer(provider)
    with respx.mock as router:
        router.get(SERPAPI_URL).mock(
            return_value=httpx.Response(200, json=load("return_flights_krw.json"))
        )
        detailed = provider.get_flight_details(offer)
    assert detailed.inbound is not None
    return detailed


def test_round_trip_match_key() -> None:
    provider = make_provider()
    detailed = _detailed_peach(provider)
    expected = "ICN-NRT-2026-11-20T10:55-MM702|RT:2026-11-23|ECONOMY"
    assert detailed.offer_key() != expected
    assert round_trip_match_key(detailed) == expected
    # inbound known but no return_date: the inbound departure date is used
    no_date = FlightOffer.from_dict({**detailed.to_dict(), "return_date": None})
    assert round_trip_match_key(no_date) == expected
    one_way = FlightOffer.from_dict(
        {**detailed.to_dict(), "inbound": None, "return_date": None}
    )
    assert round_trip_match_key(one_way) == one_way.offer_key()


def test_get_price_of_detailed_offer_prices_the_exact_pair() -> None:
    """An enriched offer (inbound known) is re-found by outbound + return date, then its
    exact return flight is priced via the fresh departure_token (2 credits)."""
    provider = make_provider()
    detailed = _detailed_peach(provider)
    returns = load("return_flights_krw.json")
    returns["best_flights"][1]["price"] = 700000  # MM701 pair got dearer
    with respx.mock as router:
        route = router.get(SERPAPI_URL)
        route.side_effect = [
            httpx.Response(200, json=load("round_trip_krw.json")),
            httpx.Response(200, json=returns),
        ]
        assert provider.get_price(detailed) == 350000  # 700000 / 2 adults
    assert route.call_count == 2
    search, follow_up = (dict(call.request.url.params) for call in route.calls)
    assert "departure_token" not in search and search["departure_id"] == "ICN"
    assert follow_up["departure_token"].startswith("WyJ")
    assert follow_up["departure_id"] == "ICN" and follow_up["return_date"] == "2026-11-23"


def test_get_price_of_detailed_offer_none_when_return_flight_gone() -> None:
    provider = make_provider()
    detailed = _detailed_peach(provider)
    returns = load("return_flights_krw.json")
    del returns["best_flights"][1]  # MM701 no longer offered
    with respx.mock as router:
        route = router.get(SERPAPI_URL)
        route.side_effect = [
            httpx.Response(200, json=load("round_trip_krw.json")),
            httpx.Response(200, json=returns),
        ]
        assert provider.get_price(detailed) is None
    assert route.call_count == 2


def test_get_price_of_detailed_offer_skips_follow_up_when_outbound_gone() -> None:
    provider = make_provider()
    detailed = _detailed_peach(provider)
    gone = load("round_trip_krw.json")
    gone["best_flights"] = gone["best_flights"][1:]
    with respx.mock as router:
        route = router.get(SERPAPI_URL).mock(return_value=httpx.Response(200, json=gone))
        assert provider.get_price(detailed) is None
    assert route.call_count == 1


# --------------------------------------------------------------------------- quota pause

NOW = datetime(2026, 10, 6, 3, 0, tzinfo=UTC)
# SerpApi sends this text for both the hourly limit and a used-up plan.
OUT_OF_SEARCHES = {"error": "Your account has run out of searches."}


def test_quota_pause_decisions() -> None:
    hourly = quota_pause(ACCOUNT_WITH_SEARCHES, NOW)
    assert hourly.until == NOW + RATE_LIMIT_PAUSE and "hourly search limit" in hourly.reason

    unknown = quota_pause(None, NOW)
    assert unknown.until == NOW + RATE_LIMIT_PAUSE and unknown.reason.startswith("search limit")
    assert quota_pause({"plan_name": "x"}, NOW).until == NOW + RATE_LIMIT_PAUSE

    exhausted = quota_pause(ACCOUNT_EXHAUSTED, NOW)
    assert exhausted.until == datetime(2026, 11, 6, tzinfo=UTC)
    assert "no SerpApi searches left" in exhausted.reason
    assert "renews 2026-11-06" in exhausted.reason

    no_renewal = quota_pause({**ACCOUNT_EXHAUSTED, "plan_renewal_date": None}, NOW)
    assert no_renewal.until == NOW + EXHAUSTED_PAUSE
    past = quota_pause({**ACCOUNT_EXHAUSTED, "plan_renewal_date": "2026-10-06"}, NOW)
    assert past.until == NOW + RATE_LIMIT_PAUSE  # renewal date already reached: re-probe soon
    far = quota_pause({**ACCOUNT_EXHAUSTED, "plan_renewal_date": "2027-12-01"}, NOW)
    assert far.until == NOW + MAX_PAUSE
    bad = quota_pause({**ACCOUNT_EXHAUSTED, "plan_renewal_date": "soon"}, NOW)
    assert bad.until == NOW + EXHAUSTED_PAUSE


def test_429_pauses_provider_until_plan_renews(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    provider = make_provider()
    renewal = (datetime.now(UTC) + timedelta(days=10)).date().isoformat()
    with respx.mock as router:
        router.get(SERPAPI_URL).mock(
            return_value=httpx.Response(429, json=OUT_OF_SEARCHES)
        )
        account = router.get(ACCOUNT_URL).mock(
            return_value=httpx.Response(
                200, json={**ACCOUNT_EXHAUSTED, "plan_renewal_date": renewal}
            )
        )
        with pytest.raises(ProviderBlocked) as info:
            provider.search_flights(SEL_TYO)
    assert account.calls.last.request.url.params["api_key"] == API_KEY
    assert "run out of searches" in str(info.value)
    assert f"SerpApi paused until {renewal} 09:00" in str(info.value)  # 00:00 UTC in KST

    status = provider.status()
    assert status.configured is False
    assert status.message.startswith(f"SerpApi paused until {renewal} 09:00")
    assert f"renews {renewal}" in status.message
    # another provider instance (next scheduled run) sees the same pause; other keys do not
    assert make_provider().status().configured is False
    assert make_provider(key="another-key").status().configured is True
    pause = active_pause(API_KEY)
    assert pause is not None
    assert active_pause(API_KEY, now=pause.until) is None  # expires on its own
    assert make_provider().status().configured is True

    assert API_KEY not in caplog.text and "someone@example.com" not in caplog.text


def test_hourly_limit_pause_and_success_lifts_it() -> None:
    provider = make_provider()
    with respx.mock as router:
        route = router.get(SERPAPI_URL)
        route.mock(return_value=httpx.Response(429, json=OUT_OF_SEARCHES))
        router.get(ACCOUNT_URL).mock(return_value=httpx.Response(200, json=ACCOUNT_WITH_SEARCHES))
        with pytest.raises(ProviderBlocked, match="hourly search limit reached"):
            provider.search_flights(SEL_TYO)
        pause = active_pause(API_KEY)
        assert pause is not None
        remaining = pause.until - datetime.now(UTC)
        assert timedelta(minutes=59) < remaining <= RATE_LIMIT_PAUSE
        assert provider.status().configured is False

        route.mock(return_value=httpx.Response(200, json=load("round_trip_krw.json")))
        assert provider.search_flights(SEL_TYO)  # an explicit call still goes through ...
    assert active_pause(API_KEY) is None  # ... and lifts the pause
    assert provider.status().configured is True


def test_429_with_failing_account_lookup(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    provider = make_provider()
    with respx.mock as router:
        router.get(SERPAPI_URL).mock(return_value=httpx.Response(429, json={"error": "Too many"}))
        account = router.get(ACCOUNT_URL)
        account.mock(side_effect=httpx.ConnectError(f"boom ?api_key={API_KEY}"))
        with pytest.raises(ProviderBlocked, match="search limit reached"):
            provider.search_flights(SEL_TYO)
        account.mock(return_value=httpx.Response(401, json={"error": "Invalid API key."}))
        with pytest.raises(ProviderBlocked):
            provider.search_flights(SEL_TYO)
        account.mock(return_value=httpx.Response(200, text="<html>"))
        with pytest.raises(ProviderBlocked):
            provider.search_flights(SEL_TYO)
    pause = active_pause(API_KEY)
    assert pause is not None and pause.reason == "search limit reached (HTTP 429)"
    assert "SerpApi account lookup failed" in caplog.text
    assert API_KEY not in caplog.text


def test_deep_search_option_is_sent() -> None:
    with respx.mock as router:
        route = router.get(SERPAPI_URL).mock(
            return_value=httpx.Response(200, json=load("round_trip_krw.json"))
        )
        make_provider(options={"deep_search": True}).search_flights(SEL_TYO)
    assert route.calls.last.request.url.params["deep_search"] == "true"


# --------------------------------------------------------------------------- live


@pytest.mark.live
def test_live_invalid_key_maps_to_not_configured() -> None:
    """Real SerpApi call with a bogus key: must map to ProviderNotConfigured (no credit used)."""
    provider = make_provider(key="invalid-key-for-aisquared-travel-tests")
    query = FlightQuery("ICN", "NRT", date.today() + timedelta(days=60), None)
    with pytest.raises(ProviderNotConfigured, match="HTTP 401"):
        provider.search_flights(query)


@pytest.mark.live
def test_live_search_with_real_key() -> None:
    """One real search (1 credit) when SERPAPI_API_KEY(_LIVE) is exported; skipped otherwise."""
    if not LIVE_KEY:
        pytest.skip("SERPAPI_API_KEY(_LIVE) not set")
    depart = date.today() + timedelta(days=45)
    query = FlightQuery("ICN", "NRT", depart, depart + timedelta(days=3), adults=2)
    offers = make_provider(key=LIVE_KEY, timeout=60).search_flights(query)
    assert offers, "expected priced ICN-NRT offers"
    for offer in offers:
        assert offer.currency == "KRW" and offer.price_total > 0
        assert offer.price_per_person == pytest.approx(offer.price_total / 2)
        assert offer.inbound is None and offer.return_date == query.return_date.isoformat()
