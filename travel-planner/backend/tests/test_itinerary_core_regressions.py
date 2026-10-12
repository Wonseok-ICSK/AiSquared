"""Regression tests for reviewed planner issues: departure-day drops, meal opening hours,
overnight departures, layovers, must-visit cafés, airport-only city centres, hotel-area
labels, finalize_plan trust boundaries and the candidate-aware route checker."""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any

import pytest

from app.schemas import ItineraryPlan, PlanItem, SourceRef
from app.services.itinerary import calendar_util, links, routing
from app.services.itinerary.models import Candidate, PlanInputs, hhmm_to_minutes
from app.services.itinerary.planner import check_plan, finalize_plan, plan_itinerary
from test_itinerary_core_planner import fx_mock, load_candidates, tokyo_inputs  # noqa: F401 - autouse fixture

ASAKUSA = {"hotel_lat": 35.7120, "hotel_lon": 139.7960, "hotel_area": "Asakusa"}
NO_HOTEL = {"hotel_lat": None, "hotel_lon": None}


@pytest.fixture(scope="module")
def candidates() -> list[Candidate]:
    return load_candidates()


def _titles(plan: ItineraryPlan) -> list[str]:
    return [i.title for d in plan.days for i in d.items]


def _assert_sound(plan: ItineraryPlan, inputs: PlanInputs, candidates: list[Candidate]) -> None:
    """The planner's output passes its own full check (timing, closures, opening hours of
    sights and restaurants), has no empty day and starts/ends at the airport."""
    problems = [w for w in check_plan(plan, inputs, candidates) if "returns to an earlier area" not in w]
    assert problems == []
    assert all(day.items for day in plan.days)
    assert plan.days[0].items[0].kind == "arrival" and plan.days[-1].items[-1].kind == "departure"
    for term in inputs.must_visit:
        assert any(term in t for t in _titles(plan)) or any(
            term in w for w in plan.warnings if "must-visit" in w
        ), term


SCENARIOS: list[tuple[str, dict[str, Any]]] = [
    ("day trip, two far must-visits", dict(arrival_at=datetime(2026, 11, 20, 10, 20),
                                           departure_at=datetime(2026, 11, 20, 20, 20), pace="packed",
                                           must_visit=["Tokyo Skytree", "Shibuya Sky"], **ASAKUSA)),
    ("tight last day", dict(arrival_at=datetime(2026, 11, 20, 12, 30), departure_at=datetime(2026, 11, 24, 16, 55),
                            must_visit=["Golden Gai", "teamLab"], **ASAKUSA)),
    ("forced sushi lunch", dict(arrival_at=datetime(2026, 11, 20, 9, 10), departure_at=datetime(2026, 11, 21, 16, 5),
                                pace="relaxed", must_visit=["Sushi Dai"], **ASAKUSA)),
    ("pickup before midnight", dict(arrival_at=datetime(2026, 11, 20, 8, 0),
                                    departure_at=datetime(2026, 11, 21, 4, 0))),
    ("02:00 departure", dict(arrival_at=datetime(2026, 11, 20, 19, 10), departure_at=datetime(2026, 11, 22, 2, 0),
                             **ASAKUSA)),
    ("01:30 departure HND", dict(departure_at=datetime(2026, 11, 23, 1, 30), arrival_airport="HND",
                                 departure_airport="HND")),
    ("night arrival, forced café", dict(arrival_at=datetime(2026, 11, 20, 0, 35),
                                        departure_at=datetime(2026, 11, 21, 6, 40), pace="packed",
                                        must_visit=["Blue Bottle Coffee Shinjuku"], **NO_HOTEL)),
    ("default, forced café", dict(must_visit=["Blue Bottle Coffee Shinjuku"])),
]


@pytest.mark.parametrize("overrides", [s[1] for s in SCENARIOS], ids=[s[0] for s in SCENARIOS])
def test_plans_pass_their_own_full_check(candidates: list[Candidate], overrides: dict[str, Any]) -> None:
    inputs = tokyo_inputs(**overrides)
    _assert_sound(plan_itinerary(inputs, candidates), inputs, candidates)


# --------------------------------------------------------------------------- departure day


def test_departure_day_drops_are_reported_and_must_visits_rescued(candidates: list[Candidate]) -> None:
    inputs = tokyo_inputs(**SCENARIOS[0][1])
    plan = plan_itinerary(inputs, candidates)
    assert "Tokyo Skytree" in _titles(plan)  # retried alone after the pair did not fit the short day
    assert any("Shibuya Sky" in w for w in plan.warnings if "must-visit" in w)
    assert "Shibuya Sky" in {u["name"] for u in plan.unscheduled}  # type: ignore[attr-defined]


def test_departure_day_meal_never_overlaps_the_bag_pickup(candidates: list[Candidate]) -> None:
    inputs = tokyo_inputs(**SCENARIOS[1][1])
    last = plan_itinerary(inputs, candidates).days[-1]
    pickup = next(i for i in last.items if i.kind == "hotel")
    before = [i for i in last.items if i.kind in ("meal", "activity", "free")]
    assert before and all(hhmm_to_minutes(i.end) <= hhmm_to_minutes(pickup.start) for i in before)


# --------------------------------------------------------------------------- meals


@pytest.mark.parametrize("overrides", [s[1] for s in SCENARIOS[:3]] + [{"pace": "packed"}, {"pace": "relaxed"}])
def test_meals_fit_restaurant_hours_at_the_real_arrival_time(
    candidates: list[Candidate], overrides: dict[str, Any]
) -> None:
    """Every restaurant meal starts after opening and ends by closing time, travel included
    (the hours used to be checked when the previous stop ended, before the hop)."""
    inputs = tokyo_inputs(**overrides)
    plan = plan_itinerary(inputs, candidates)
    by_id = {c.id: c for c in candidates}
    holidays = calendar_util.holidays_for("JP", [2026])
    meals = [(d.date, i) for d in plan.days for i in d.items if i.kind == "meal" and i.candidate_id]
    assert meals or len(plan.days) == 1  # the 10-hour day trip has no time for a restaurant
    for day, meal in meals:
        windows = calendar_util.open_windows(by_id[meal.candidate_id], day, holidays)  # type: ignore[index]
        start, end = hhmm_to_minutes(meal.start), hhmm_to_minutes(meal.end)
        assert windows is None or any(o <= start and end <= c for o, c in windows), (meal.title, windows)
    if "Sushi Dai" in inputs.must_visit and not any(t.endswith("Sushi Dai") for t in _titles(plan)):
        assert any("Sushi Dai" in w for w in plan.warnings if "must-visit" in w)  # no time before 14:00


def test_must_visit_cafe_is_not_repeated_for_lunch(candidates: list[Candidate]) -> None:
    plan = plan_itinerary(tokyo_inputs(must_visit=["Blue Bottle Coffee Shinjuku"]), candidates)
    visits = [i for d in plan.days for i in d.items if i.kind == "meal" and i.title.endswith("Blue Bottle Coffee Shinjuku")]
    assert visits and all(getattr(i, "meal") == "breakfast" for i in visits)
    assert not any("must-visit" in w for w in plan.warnings)


# --------------------------------------------------------------------------- overnight departures / layovers


def test_overnight_departure_block_stays_on_the_pickup_date(candidates: list[Candidate]) -> None:
    inputs = tokyo_inputs(**SCENARIOS[5][1])  # flight 01:30 on Mon 23 Nov
    plan = plan_itinerary(inputs, candidates)
    assert [d.date for d in plan.days] == [date(2026, 11, 20), date(2026, 11, 21), date(2026, 11, 22)]
    assert plan.title == "Tokyo · 3 days"
    last = plan.days[-1].items
    assert [i.kind for i in last[-3:]] == ["hotel", "transit", "departure"]
    assert last[-1].end == "01:30" and "01:30 on Mon 23 Nov" in last[-1].title
    assert plan.days[-1].theme != "Departure" or not any(i.kind == "activity" for i in last)


def test_pickup_just_before_midnight_is_not_wrapped_onto_the_next_day(candidates: list[Candidate]) -> None:
    inputs = tokyo_inputs(**SCENARIOS[3][1])  # leave the hotel at 00:12, flight 04:00
    plan = plan_itinerary(inputs, candidates)
    assert [d.date for d in plan.days] == [date(2026, 11, 20)]
    tail = plan.days[0].items[-3:]
    assert [(i.kind, i.start) for i in tail] == [("hotel", "23:52"), ("transit", "00:12"), ("departure", "02:00")]
    assert "Sat 21 Nov" in tail[-1].title
    assert check_plan(plan, inputs) == []


def test_stay_shorter_than_the_airport_round_trip_is_a_layover(candidates: list[Candidate]) -> None:
    inputs = tokyo_inputs(arrival_at=datetime(2026, 11, 20, 1, 15), departure_at=datetime(2026, 11, 20, 7, 15),
                          must_visit=["Sushi Dai"])
    plan = plan_itinerary(inputs, candidates)
    assert plan.layover is True and plan.hotel is None  # type: ignore[attr-defined]
    assert [(i.kind, i.start, i.end) for i in plan.days[0].items] == [
        ("arrival", "01:15", "02:45"), ("free", "02:45", "05:15"), ("departure", "05:15", "07:15"),
    ]
    assert any("stays at the airport" in w for w in plan.warnings) and plan.summary.startswith("Only 6.0 h")
    assert any("Sushi Dai" in w for w in plan.warnings if "must-visit" in w)
    lines = {line.category: line for line in plan.budget.lines}  # type: ignore[union-attr]
    assert lines["hotel"].estimated == 0
    assert check_plan(plan, inputs, candidates) == []

    other_airport = tokyo_inputs(arrival_at=datetime(2026, 11, 20, 20, 45),
                                 departure_at=datetime(2026, 11, 21, 3, 15), departure_airport="HND")
    plan = plan_itinerary(other_airport, candidates)
    items = plan.days[0].items
    assert [i.kind for i in items] == ["arrival", "transit", "free", "departure"]
    assert items[1].travel_from_prev is not None and items[1].travel_from_prev.distance_km > 50  # NRT -> HND
    assert "Sat 21 Nov" in items[-1].title and len(plan.days) == 1
    assert check_plan(plan, other_airport) == []


# --------------------------------------------------------------------------- centre and hotel


ISTANBUL = [
    ("Hagia Sophia", "historic", 41.0086, 28.9802), ("Blue Mosque", "historic", 41.0054, 28.9768),
    ("Topkapi Palace", "historic", 41.0115, 28.9834), ("Galata Tower", "viewpoint", 41.0256, 28.9741),
    ("Grand Bazaar", "market", 41.0107, 28.9681), ("Basilica Cistern", "historic", 41.0084, 28.9779),
]


def test_plan_works_when_the_centre_is_only_the_airport() -> None:
    sights = [Candidate(name=n, category=c, lat=la, lon=lo, area="Fatih", coord_source="nominatim")
              for n, c, la, lo in ISTANBUL]
    inputs = PlanInputs(
        destination="IST", city="Istanbul", country="TR", currency="TRY", center_lat=41.2753, center_lon=28.7519,
        center_source="airport", arrival_at="2026-05-01T09:00", departure_at="2026-05-03T20:00",
        arrival_airport="IST", departure_airport="IST", language="en", budget_total=-1, budget_flight=-1,
    )
    plan = plan_itinerary(inputs, sights)
    assert len([i for d in plan.days for i in d.items if i.kind == "activity"]) == len(ISTANBUL)
    assert not any("km from the centre" in w for w in plan.warnings)


def test_unlocated_hotel_area_is_labelled_by_the_place_assumed(candidates: list[Candidate]) -> None:
    plan = plan_itinerary(tokyo_inputs(hotel_area="Ikebukuro", **NO_HOTEL), candidates)
    hotel = plan.hotel  # type: ignore[attr-defined]
    assert hotel["assumed"] is True and hotel["area"] != "Ikebukuro"
    assert any(w.startswith("Could not locate the hotel area “Ikebukuro” — assumed near " + hotel["area"])
               for w in plan.warnings)
    assert not any("Ikebukuro" in t for t in _titles(plan))
    approx = plan_itinerary(tokyo_inputs(hotel_area="Ikebukuro", hotel_lat=35.7295, hotel_lon=139.7109,
                                         hotel_approximate=True), candidates)
    assert approx.hotel["area"] == "Ikebukuro" and approx.hotel["assumed"] is True  # type: ignore[attr-defined]
    assert routing.distance_km((approx.hotel["lat"], approx.hotel["lng"]), (35.7295, 139.7109)) < 0.01  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- finalize_plan trust boundary


def test_finalize_plan_rebuilds_llm_supplied_facts_and_links(candidates: list[Candidate]) -> None:
    inputs = tokyo_inputs()
    plan = plan_itinerary(inputs, candidates)
    by_id = {c.id: c for c in candidates}
    edited = plan.model_copy(deep=True)
    day = edited.days[2]
    index, act = next((k, i) for k, i in enumerate(day.items) if i.kind == "activity" and by_id[i.candidate_id].source_urls)  # type: ignore[index]
    act.lat, act.lng = 35.0, 135.0  # hallucinated coordinates (Kyoto-ish)
    act.source_urls = ["https://hallucinated.example/"]
    act.map_url = "https://evil.example/a"
    act.links = {"phish": "https://evil.example/b"}  # type: ignore[attr-defined]
    edited.days[0].items[0].map_url = "https://evil.example/x"  # arrival item
    edited.days[0].items[0].lat = edited.days[0].items[0].lng = 0.0
    invented = PlanItem(start="18:00", end="18:30", kind="activity", title="Hidden alley", area="Asakusa",
                        lat=35.713, lng=139.795, map_url="https://evil.example/y",
                        source_urls=["https://made-up.example/page", "https://allowed.example/guide"],
                        links={"x": "https://evil.example/z"}, directions_url="https://evil.example/d")
    moon = PlanItem(start="19:00", end="19:10", kind="activity", title="Moon base", lat=10.0, lng=10.0)
    day.items[index + 1:index + 1] = [invented, moon]
    edited.sources = [SourceRef(title="made up", url="https://made-up.example/page"),
                      SourceRef(title="guide", url="https://allowed.example/guide")]

    final = finalize_plan(edited, inputs, candidates, allowed_urls=["https://allowed.example/guide", "javascript:x"])
    dump = json.dumps(final.model_dump(mode="json"))
    for bad in ("evil.example", "hallucinated.example", "made-up.example", "javascript:"):
        assert bad not in dump, bad
    fixed = next(i for i in final.days[2].items if i.candidate_id == act.candidate_id)
    candidate = by_id[act.candidate_id]  # type: ignore[index]
    assert (fixed.lat, fixed.lng) == candidate.point and fixed.source_urls == candidate.source_urls
    assert fixed.map_url == links.place_links(candidate, "Tokyo", "JP")["google_maps"]
    assert final.route_metrics.max_hop_km < 15  # the hallucinated point no longer drives the hops
    arrival = final.days[0].items[0]
    assert arrival.lat == pytest.approx(35.772, abs=0.01) and arrival.map_url.startswith("https://www.google.com/maps/")
    alley = next(i for i in final.days[2].items if i.title == "Hidden alley")
    assert alley.map_url.startswith("https://www.google.com/maps/search/") and alley.coords_approximate  # type: ignore[attr-defined]
    assert alley.source_urls == ["https://allowed.example/guide"] and not hasattr(alley, "links")
    assert next(i for i in final.days[2].items if i.title == "Moon base").lat is None  # implausible: dropped
    assert {s.url for s in final.sources} >= {"https://allowed.example/guide", *candidate.source_urls}


# --------------------------------------------------------------------------- check_plan with candidates


def test_check_plan_flags_closures_hours_and_wet_outdoor_stops(candidates: list[Candidate]) -> None:
    inputs = tokyo_inputs()
    plan = plan_itinerary(inputs, candidates)
    assert check_plan(plan, inputs, candidates) == []
    swapped = plan.model_copy(deep=True)
    sat, sun = swapped.days[1], swapped.days[2]
    sat.items, sun.items = sun.items, sat.items  # an LLM reorder of whole days
    final = finalize_plan(swapped, inputs, candidates)
    assert "Tsukiji Outer Market is closed on Sun 22 Nov." in check_plan(final, inputs, candidates)  # Sundays
    assert not any("closed on" in w for w in check_plan(final, inputs))  # facts need the candidates

    early = plan.model_copy(deep=True)
    museum = next(i for d in early.days for i in d.items if i.title == "Tokyo National Museum")
    museum.start, museum.end = "07:00", "08:00"
    assert any("Tokyo National Museum" in w and "outside its opening hours" in w
               for w in check_plan(early, inputs, candidates))

    outdoor = next((d.date, i.title) for d in plan.days for i in d.items
                   if i.candidate_id and {c.id: c for c in candidates}[i.candidate_id].is_outdoor)
    wet = {outdoor[0]: {"summary": "Heavy rain", "precip_prob": 90, "precip_mm": 20.0}}
    assert any(outdoor[1] in w and "Rain likely" in w for w in check_plan(plan, inputs, candidates, weather=wet))
