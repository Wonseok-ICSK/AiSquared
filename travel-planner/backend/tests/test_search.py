from __future__ import annotations

import sys
import threading
import time
from datetime import date

import pytest
from sqlalchemy.orm import Session

from app.providers.flights.base import (
    FlightOffer,
    FlightQuery,
    ProviderBlocked,
    ProviderError,
)
from app.providers.flights.mock import MockProvider
from app.services import search
from app.services.search import (
    build_query_plan,
    offer_matches_trip,
    plan_queries,
    resolve_providers,
    safe_error,
    search_trip,
)
from app.settings_store import save_runtime_settings
from monitoring_fakes import (  # noqa: F401  (register is a fixture)
    TODAY,
    days,
    fake_provider_class,
    make_leg,
    make_offer,
    make_trip,
    offers_for_query,
    register,
)

AIRPORT_ONLY = fake_provider_class("airport_only", lambda q: [], metro=False)
METRO_OK = fake_provider_class("metro_ok", lambda q: [], metro=True)


def routes(queries: list[FlightQuery]) -> list[str]:
    return [f"{q.origin}-{q.destination}" for q in queries]


def offsets(
    queries: list[FlightQuery], trip_depart: date, trip_return: date
) -> list[tuple[int, int]]:
    return [
        ((q.depart_date - trip_depart).days, (q.return_date - trip_return).days) for q in queries
    ]


# --------------------------------------------------------------------------- query plan


def test_exact_dates_single_query_with_trip_fields() -> None:
    trip = make_trip(adults=2, cabin="business", currency="JPY", max_stops=1)
    queries, notes = build_query_plan(trip, AIRPORT_ONLY, TODAY, 12)
    assert queries == [
        FlightQuery(
            origin="ICN",
            destination="NRT",
            depart_date=date(2026, 11, 20),
            return_date=date(2026, 11, 24),
            adults=2,
            cabin="business",
            direct_only=False,
            max_stops=1,
            currency="JPY",
        )
    ]
    assert notes == []


def test_direct_only_sets_max_stops_zero() -> None:
    queries, _ = build_query_plan(make_trip(direct_only=True, max_stops=2), METRO_OK, TODAY, 12)
    assert queries[0].direct_only is True and queries[0].max_stops == 0


def test_flex_dates_exact_first_then_by_distance() -> None:
    trip = make_trip(flex_depart_days=1, flex_return_days=1)
    queries, notes = build_query_plan(trip, METRO_OK, TODAY, 12)
    assert offsets(queries, trip.depart_date, trip.return_date) == [
        (0, 0),
        (0, -1), (0, 1), (-1, 0), (1, 0),
        (-1, -1), (-1, 1), (1, -1), (1, 1),
    ]
    assert notes == []


def test_one_way_ignores_return_flex() -> None:
    trip = make_trip(trip_type="one_way", return_date=None, flex_depart_days=1, flex_return_days=3)
    queries, _ = build_query_plan(trip, METRO_OK, TODAY, 12)
    assert [q.depart_date for q in queries] == [
        date(2026, 11, 20), date(2026, 11, 19), date(2026, 11, 21)
    ]
    assert all(q.return_date is None for q in queries)


def test_past_departures_dropped_and_noted() -> None:
    trip = make_trip(depart_date=TODAY + days(1), return_date=TODAY + days(5), flex_depart_days=3)
    plan = plan_queries(trip, METRO_OK, TODAY, 12)
    assert all(q.depart_date >= TODAY for q in plan.queries)
    assert len(plan.queries) == plan.total == 5  # -1..+3 (today is still searchable)
    assert any(
        "Skipped 2 date combination(s) departing in the past" in n
        and (TODAY - days(2)).isoformat() in n
        for n in plan.notes
    )


def test_return_before_departure_dropped_and_noted() -> None:
    trip = make_trip(
        depart_date=date(2026, 11, 20), return_date=date(2026, 11, 21),
        flex_depart_days=1, flex_return_days=1,
    )
    queries, notes = build_query_plan(trip, METRO_OK, TODAY, 12)
    assert len(queries) == 8
    assert all(q.return_date >= q.depart_date for q in queries)
    assert "Skipped 1 date combination(s) returning before departure" in notes


def test_cap_keeps_closest_dates_and_summarizes_the_rest() -> None:
    trip = make_trip(flex_depart_days=3, flex_return_days=3)
    plan = plan_queries(trip, METRO_OK, TODAY, 12)
    # 49 date pairs, 3 of them return before departing (e.g. out 11-23, back 11-21)
    assert plan.total == 46 and len(plan.queries) == 12
    distances = [
        abs(d) + abs(r) for d, r in offsets(plan.queries, trip.depart_date, trip.return_date)
    ]
    assert distances == sorted(distances) and distances[0] == 0 and max(distances) <= 2
    reversed_note, cap_note = plan.notes
    assert reversed_note == "Skipped 3 date combination(s) returning before departure"
    assert "Searching 12 of 46 combinations" in cap_note
    assert "not searched: 34" in cap_note
    assert "2026-11-17..2026-11-23" in cap_note and "ICN→NRT" in cap_note


def test_provider_limit_is_named_in_note() -> None:
    small = fake_provider_class("small", lambda q: [], max_queries=4)
    queries, notes = build_query_plan(make_trip(flex_depart_days=3), small, TODAY, 12)
    assert len(queries) == 4
    assert "provider limit max_queries_per_run=4" in notes[0]


def test_setting_limit_is_named_in_note() -> None:
    queries, notes = build_query_plan(make_trip(flex_depart_days=3), METRO_OK, TODAY, 5)
    assert len(queries) == 5
    assert "setting max_search_combinations=5" in notes[0]
    assert "not searched: 2" in notes[0]


def test_metro_codes_passed_as_is_for_capable_providers() -> None:
    trip = make_trip(origin="SEL", destination="TYO")
    queries, notes = build_query_plan(trip, METRO_OK, TODAY, 12)
    assert routes(queries) == ["SEL-TYO"]
    assert notes == []


def test_metro_codes_expanded_for_airport_only_providers() -> None:
    trip = make_trip(origin="SEL", destination="TYO", flex_depart_days=1)
    queries, notes = build_query_plan(trip, AIRPORT_ONLY, TODAY, 12)
    assert routes(queries[:4]) == ["ICN-NRT", "ICN-HND", "GMP-NRT", "GMP-HND"]
    assert {q.depart_date for q in queries[:4]} == {trip.depart_date}  # dates before routes
    assert len(queries) == 12
    assert any("SEL searched as ICN, GMP" in n for n in notes)
    assert any("TYO searched as NRT, HND" in n for n in notes)


def test_nearby_airports_for_airport_only_provider() -> None:
    trip = make_trip(include_nearby_airports=True)
    queries, _ = build_query_plan(trip, AIRPORT_ONLY, TODAY, 12)
    # own airports first, then metro siblings, then other nearby airports
    assert routes(queries) == ["ICN-NRT", "ICN-HND", "GMP-NRT", "ICN-IBR", "GMP-HND", "GMP-IBR"]


def test_nearby_airports_use_metro_code_for_capable_provider() -> None:
    trip = make_trip(include_nearby_airports=True)
    queries, notes = build_query_plan(trip, METRO_OK, TODAY, 12)
    assert routes(queries) == ["SEL-TYO", "SEL-IBR"]
    assert any("ICN searched as SEL" in n for n in notes)
    assert any("NRT searched as TYO" in n for n in notes)


def test_nearby_airport_limit_is_noted() -> None:
    trip = make_trip(destination="FUK", include_nearby_airports=True)
    queries, notes = build_query_plan(trip, AIRPORT_ONLY, TODAY, 12)
    assert routes(queries)[0] == "ICN-FUK"
    assert {q.destination for q in queries} == {"FUK", "HSG", "KKJ"}
    (note,) = [n for n in notes if "Nearby airports for FUK" in n]
    assert "limited to the 2 nearest" in note and "IKI" in note


def test_alt_destinations() -> None:
    trip = make_trip(alt_destinations=["OSA"])
    queries, _ = build_query_plan(trip, METRO_OK, TODAY, 12)
    assert routes(queries) == ["ICN-NRT", "ICN-OSA"]
    queries, _ = build_query_plan(trip, AIRPORT_ONLY, TODAY, 12)
    assert routes(queries) == ["ICN-NRT", "ICN-KIX", "ICN-ITM", "ICN-UKB"]


def test_alt_destination_already_covered_is_not_duplicated() -> None:
    trip = make_trip(origin="ICN", destination="TYO", alt_destinations=["HND", "KIX"])
    queries, _ = build_query_plan(trip, METRO_OK, TODAY, 12)
    assert routes(queries) == ["ICN-TYO", "ICN-KIX"]


def test_overlapping_routes_dropped_and_noted() -> None:
    trip = make_trip(include_nearby_airports=True, alt_destinations=["GMP"])
    queries, notes = build_query_plan(trip, AIRPORT_ONLY, TODAY, 20)
    assert "GMP-GMP" not in routes(queries) and "ICN-GMP" in routes(queries)
    assert any("Skipped 1 route(s)" in n and "GMP→GMP" in n for n in notes)


def test_zero_cap_drops_everything_with_note() -> None:
    queries, notes = build_query_plan(make_trip(), METRO_OK, TODAY, 0)
    assert queries == [] and "not searched: 1" in notes[0]


# --------------------------------------------------------------------------- filters

CASES = [
    # (trip overrides, offer kwargs, expected match, expected reason prefix or None)
    ({}, {}, True, None),
    ({"direct_only": True}, {"outbound": make_leg(stops=1)}, False, "stops: outbound has 1"),
    ({"direct_only": True},
     {"inbound": make_leg("NRT", "ICN", date(2026, 11, 24), stops=1)}, False, "stops: return"),
    ({"direct_only": True}, {"inbound": None, "return_date": "2026-11-24"}, True, "not checked"),
    ({"max_stops": 1}, {"outbound": make_leg(stops=2)}, False, "stops: outbound has 2"),
    ({"max_stops": 1}, {"outbound": make_leg(stops=1)}, True, None),
    ({"direct_only": True, "max_stops": 2}, {"outbound": make_leg(stops=1)}, False, "stops"),
    ({"outbound_depart_from": "08:00", "outbound_depart_to": "10:00"}, {}, True, None),
    ({"outbound_depart_from": "07:00", "outbound_depart_to": "08:30"}, {}, False,
     "outbound departure time: 09:00 outside 07:00–08:30"),
    ({"outbound_depart_to": "09:00"}, {}, True, None),  # bounds are inclusive
    ({"outbound_arrive_from": "12:00"}, {}, False, "outbound arrival time"),
    ({"outbound_depart_from": "22:00", "outbound_depart_to": "02:00"},
     {"outbound": make_leg(depart="23:30")}, True, None),
    ({"outbound_depart_from": "22:00", "outbound_depart_to": "02:00"}, {}, False, "outbound"),
    ({"return_depart_from": "14:00"}, {}, False, "return departure time: 13:00"),
    ({"return_arrive_to": "16:00"}, {}, True, None),
    ({"return_depart_from": "14:00"}, {"inbound": None, "return_date": "2026-11-24"}, True,
     "not checked: return departure time"),
    ({"max_price": 250_000}, {}, False, "max price: 300,000 KRW per person > 250,000 KRW"),
    ({"max_price": 300_000}, {}, True, None),
    ({"max_price": 650_000, "price_basis": "total", "adults": 2}, {"adults": 2}, True, None),
    ({"max_price": 550_000, "price_basis": "total", "adults": 2}, {"adults": 2}, False,
     "max price: 600,000 KRW total"),
    ({"max_price": 1_000_000}, {"currency": "USD"}, False, "currency: offer in USD"),
]


@pytest.mark.parametrize(("trip_kwargs", "offer_kwargs", "expected", "reason"), CASES)
def test_offer_matches_trip(
    trip_kwargs: dict, offer_kwargs: dict, expected: bool, reason: str | None
) -> None:
    ok, reasons = offer_matches_trip(make_offer(**offer_kwargs), make_trip(**trip_kwargs))
    assert ok is expected, reasons
    if reason is None:
        assert reasons == []
    else:
        assert any(r.startswith(reason) for r in reasons), reasons


def test_unknown_inbound_notes_are_deduplicated() -> None:
    trip = make_trip(direct_only=True, return_depart_from="10:00", return_arrive_to="20:00")
    ok, reasons = offer_matches_trip(make_offer(inbound=None, return_date="2026-11-24"), trip)
    assert ok
    assert len(reasons) == 3 and all(r.startswith("not checked") for r in reasons)


def test_one_way_offer_without_inbound_has_no_notes() -> None:
    trip = make_trip(trip_type="one_way", return_date=None, direct_only=True)
    assert offer_matches_trip(make_offer(inbound=None), trip) == (True, [])


def test_summarize_mismatches_counts_filters() -> None:
    summary = search.summarize_mismatches(
        [["max price: x", "stops: y"], ["max price: z", "not checked: w"]]
    )
    assert summary == "max price 2, stops 1"
    assert search.summarize_mismatches([["not checked: w"]]) is None


# --------------------------------------------------------------------------- providers


def test_resolve_providers_skips_unknown_and_unconfigured(db: Session, register) -> None:
    register(fake_provider_class("nokey", lambda q: [], configured=False))
    slots = resolve_providers(db, make_trip(providers=["mock", "nokey", "nope"]))
    by_key = {key: (provider, reason) for key, provider, reason in slots}
    assert isinstance(by_key["mock"][0], MockProvider) and by_key["mock"][1] is None
    assert by_key["nokey"] == (None, "needs key")
    assert by_key["nope"][0] is None and "unknown provider" in by_key["nope"][1]


def test_resolve_providers_uses_runtime_defaults_and_options(db: Session, register) -> None:
    register(fake_provider_class("opt", lambda q: []))
    save_runtime_settings(
        db, {"default_providers": ["opt"], "provider_options": {"opt": {"fetch_mode": "http"}}}
    )
    ((key, provider, reason),) = resolve_providers(db, make_trip(providers=[]))
    assert key == "opt" and reason is None
    assert provider.ctx.options == {"fetch_mode": "http"}
    assert provider.ctx.browser is None


def test_browser_manager_given_to_browser_providers(
    db: Session, register, monkeypatch: pytest.MonkeyPatch
) -> None:
    sentinel = object()
    monkeypatch.setattr(search, "_browser_manager", lambda: sentinel)
    register(fake_provider_class("browsy", lambda q: [], uses_browser=True))
    ((_, provider, _),) = resolve_providers(db, make_trip(providers=["browsy"]))
    assert provider.ctx.browser is sentinel


def test_browser_manager_import_error_is_tolerated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "app.browser.manager", None)
    assert search._browser_manager() is None


def test_provider_init_failure_is_a_skip(db: Session, register) -> None:
    broken = fake_provider_class("broken", lambda q: [])

    def explode(self, ctx):  # noqa: ANN001
        raise RuntimeError("cannot start token=abc123")

    broken.__init__ = explode  # type: ignore[method-assign]
    register(broken)
    ((key, provider, reason),) = resolve_providers(db, make_trip(providers=["broken"]))
    assert provider is None and "could not be initialised" in reason
    assert "abc123" not in reason and "token=***" in reason


def test_safe_error_redacts_and_truncates() -> None:
    text = safe_error(RuntimeError("GET https://x.test/s?api_key=SECRET&q=1 failed " + "x" * 400))
    assert "SECRET" not in text and "api_key=***" in text and len(text) <= 300


# --------------------------------------------------------------------------- search_trip


def test_search_trip_with_mock(db: Session) -> None:
    trip = make_trip(providers=["mock"], flex_depart_days=1)
    outcome = search_trip(db, trip, today=TODAY)
    assert outcome.status == "ok" and outcome.error is None
    assert outcome.combos_total == 3 and outcome.combos_searched == 3
    stats = outcome.provider_stats["mock"]
    assert stats["status"] == "ok" and stats["queries"] == 3
    assert stats["offers"] == len(outcome.results) > 0
    assert {q.depart_date for _, q in outcome.results} == {
        date(2026, 11, 19), date(2026, 11, 20), date(2026, 11, 21)
    }
    assert all(isinstance(o, FlightOffer) for o, _ in outcome.results)
    assert isinstance(outcome.providers["mock"], MockProvider)
    assert any("[mock] 3/3 queries succeeded" in line for line in outcome.log)


def test_search_trip_errors_blocks_and_partial(db: Session, register) -> None:
    calls: dict[str, list[date]] = {"flaky": [], "blocked": []}

    def flaky(query: FlightQuery) -> list[FlightOffer]:
        calls["flaky"].append(query.depart_date)
        if len(calls["flaky"]) == 1:
            raise ProviderError("HTTP 500")
        return offers_for_query(query, [300_000], "flaky")

    def blocked(query: FlightQuery) -> list[FlightOffer]:
        calls["blocked"].append(query.depart_date)
        if len(calls["blocked"]) == 2:
            raise ProviderBlocked("captcha page")
        return offers_for_query(query, [310_000], "blocked")

    register(fake_provider_class("flaky", flaky))
    register(fake_provider_class("blocked", blocked))
    trip = make_trip(providers=["flaky", "blocked"], flex_depart_days=1)
    outcome = search_trip(db, trip, today=TODAY)

    assert len(calls["flaky"]) == 3 and len(calls["blocked"]) == 2  # block stops the provider
    flaky_stats, blocked_stats = outcome.provider_stats["flaky"], outcome.provider_stats["blocked"]
    assert flaky_stats["status"] == "error" and flaky_stats["queries_failed"] == 1
    assert "HTTP 500" in flaky_stats["error"] and flaky_stats["offers"] == 2
    assert blocked_stats["status"] == "error" and blocked_stats["queries"] == 2
    assert "blocked: captcha page" in blocked_stats["error"]
    assert any("1 remaining query skipped" in line for line in outcome.log)
    assert outcome.status == "partial" and "flaky" in outcome.error and "blocked" in outcome.error
    assert outcome.combos_total == 6 and outcome.combos_searched == 3
    assert len(outcome.results) == 3
    assert outcome.blocked == {"blocked": "captcha page"}  # for the user notice
    assert not outcome.cancelled


def test_search_trip_all_failed_is_error(db: Session, register) -> None:
    def fail(query: FlightQuery) -> list[FlightOffer]:
        raise RuntimeError("boom api_key=TOPSECRET")

    register(fake_provider_class("crashy", fail))
    outcome = search_trip(db, make_trip(providers=["crashy"]), today=TODAY)
    assert outcome.status == "error" and outcome.results == []
    assert outcome.error.startswith("All provider searches failed")
    assert "unexpected RuntimeError" in outcome.provider_stats["crashy"]["error"]
    assert "TOPSECRET" not in str(outcome.provider_stats) + " ".join(outcome.log)


def test_search_trip_skips_and_unknown(db: Session, register) -> None:
    register(fake_provider_class("nokey", lambda q: [], configured=False))
    outcome = search_trip(db, make_trip(providers=["nokey", "nope", "mock"]), today=TODAY)
    assert outcome.status == "ok"
    assert outcome.provider_stats["nokey"]["status"] == "skipped"
    assert outcome.provider_stats["nope"]["status"] == "skipped"
    assert any("[nokey] skipped: needs key" in line for line in outcome.log)


def test_search_trip_without_usable_provider(db: Session, register) -> None:
    register(fake_provider_class("nokey", lambda q: [], configured=False))
    outcome = search_trip(db, make_trip(providers=["nokey"]), today=TODAY)
    assert outcome.status == "error"
    assert outcome.error == "No usable flight provider: nokey: needs key"


def test_search_trip_nothing_left_to_search(db: Session) -> None:
    trip = make_trip(providers=["mock"], depart_date=TODAY - days(3), return_date=TODAY)
    outcome = search_trip(db, trip, today=TODAY)
    assert outcome.status == "error" and "No date/airport combination" in outcome.error
    assert outcome.provider_stats["mock"]["status"] == "skipped"
    assert any("departing in the past" in line for line in outcome.log)


def test_search_trip_runs_providers_concurrently(db: Session, register) -> None:
    barrier = threading.Barrier(2, timeout=5)

    def meet(query: FlightQuery) -> list[FlightOffer]:
        barrier.wait()  # raises BrokenBarrierError unless both providers run at once
        return []

    register(fake_provider_class("left", meet))
    register(fake_provider_class("right", meet))
    outcome = search_trip(db, make_trip(providers=["left", "right"]), today=TODAY)
    assert outcome.status == "ok", outcome.provider_stats


def test_search_trip_merges_duplicate_offers(db: Session, register) -> None:
    def twice(query: FlightQuery) -> list[FlightOffer]:
        return [make_offer(320_000, provider="dup"), make_offer(310_000, provider="dup")]

    register(fake_provider_class("dup", twice))
    outcome = search_trip(db, make_trip(providers=["dup"]), today=TODAY)
    assert [o.price_per_person for o, _ in outcome.results] == [310_000]
    assert any("1 duplicate offers merged" in line for line in outcome.log)


def test_search_trip_never_raises(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("settings exploded")

    monkeypatch.setattr(search, "load_runtime_settings", broken)
    outcome = search_trip(db, make_trip(providers=["mock"]), today=TODAY)
    assert outcome.status == "error" and "settings exploded" in outcome.error


def test_search_trip_caps_per_provider(db: Session, register) -> None:
    seen: list[FlightQuery] = []
    register(fake_provider_class("tiny", lambda q: seen.append(q) or [], max_queries=2))
    outcome = search_trip(db, make_trip(providers=["tiny"], flex_depart_days=3), today=TODAY)
    assert len(seen) == 2 and outcome.combos_total == 7 and outcome.combos_searched == 2
    assert any("[tiny] Searching 2 of 7" in line for line in outcome.log)


def test_provider_queries_run_on_daemon_threads(db: Session, register) -> None:
    """Daemon threads never keep the process alive at exit (unlike concurrent.futures)."""
    flags: list[bool] = []

    def probe(query: FlightQuery) -> list[FlightOffer]:
        flags.append(threading.current_thread().daemon)
        return []

    register(fake_provider_class("probe", probe))
    outcome = search_trip(db, make_trip(providers=["probe"], flex_depart_days=1), today=TODAY)
    assert outcome.status == "ok" and flags == [True, True, True]


def test_cancel_stops_remaining_queries(db: Session, register) -> None:
    calls: list[date] = []

    def cancel_on_first(query: FlightQuery) -> list[FlightOffer]:
        calls.append(query.depart_date)
        assert search.cancel_running_searches() == 1
        return offers_for_query(query, [300_000], "cancelme")

    register(fake_provider_class("cancelme", cancel_on_first))
    outcome = search_trip(db, make_trip(providers=["cancelme"], flex_depart_days=1), today=TODAY)
    assert len(calls) == 1  # the 2 remaining queries were never sent
    assert outcome.cancelled and outcome.status == "error"
    assert outcome.error == search.CANCELLED_ERROR
    assert search.cancel_running_searches() == 0  # finished searches are unregistered


def test_cancel_returns_while_a_query_is_in_flight(db: Session, register) -> None:
    entered, release = threading.Event(), threading.Event()
    calls: list[date] = []

    def hang(query: FlightQuery) -> list[FlightOffer]:
        calls.append(query.depart_date)
        entered.set()
        release.wait(10)
        return []

    register(fake_provider_class("hang", hang))
    canceller = threading.Thread(
        target=lambda: entered.wait(5) and search.cancel_running_searches()
    )
    canceller.start()
    started = time.monotonic()
    try:
        outcome = search_trip(db, make_trip(providers=["hang"], flex_depart_days=1), today=TODAY)
        elapsed = time.monotonic() - started
    finally:
        release.set()
        canceller.join(5)
    assert outcome.cancelled and elapsed < 3  # did not wait for the hung query
    assert outcome.provider_stats["hang"]["error"] == "cancelled"
    time.sleep(0.3)  # the released provider thread sees the stop flag ...
    assert len(calls) == 1  # ... and sends no further query


def test_cancel_does_not_affect_later_searches(db: Session) -> None:
    assert search.cancel_running_searches() == 0
    outcome = search_trip(db, make_trip(providers=["mock"]), today=TODAY)
    assert outcome.status == "ok" and not outcome.cancelled


def test_timed_out_provider_is_abandoned_and_stops(
    db: Session, register, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(search, "SEARCH_TIMEOUT_BASE_S", 0.5)
    monkeypatch.setattr(search, "QUERY_TIMEOUT_S", 0.0)
    release = threading.Event()
    slow_calls: list[date] = []

    def slow(query: FlightQuery) -> list[FlightOffer]:
        slow_calls.append(query.depart_date)
        release.wait(10)
        return offers_for_query(query, [300_000], "slow")

    register(fake_provider_class("slow", slow))
    register(fake_provider_class("fast", lambda q: offers_for_query(q, [280_000], "fast")))
    trip = make_trip(providers=["slow", "fast"], flex_depart_days=1)
    try:
        outcome = search_trip(db, trip, today=TODAY)
    finally:
        release.set()
    assert outcome.status == "partial" and not outcome.cancelled
    assert outcome.provider_stats["slow"]["error"].startswith("timed out after")
    assert outcome.provider_stats["fast"]["status"] == "ok"
    assert {o.provider for o, _ in outcome.results} == {"fast"}
    time.sleep(0.3)
    assert len(slow_calls) == 1  # abandoned provider issued no further queries


@pytest.mark.parametrize(
    ("flex", "depart_offset", "departed"),
    [(0, -1, True), (0, 0, False), (3, -3, False), (3, -4, True), (1, 5, False)],
)
def test_trip_departed_counts_flexible_dates(flex: int, depart_offset: int, departed: bool) -> None:
    trip = make_trip(depart_date=TODAY + days(depart_offset), flex_depart_days=flex,
                     return_date=TODAY + days(10))
    assert search.last_departure_date(trip) == TODAY + days(depart_offset + flex)
    assert search.trip_departed(trip, TODAY) is departed
