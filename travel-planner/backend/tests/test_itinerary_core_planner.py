"""Rule-based planner on a real Tokyo data set (arrival Fri 2026-11-20 14:00 NRT, departure
Mon 2026-11-23 19:00 NRT — a Japanese public holiday) plus edge cases and other cities."""

from __future__ import annotations

import json
import random
from collections.abc import Iterator
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from app.core import fx
from app.schemas import ItineraryPlan, PlanItem
from app.services.itinerary import calendar_util, routing
from app.services.itinerary.models import Candidate, PlanInputs, hhmm_to_minutes
from app.services.itinerary.planner import (
    AIRPORT_CUTOFF_MIN,
    IMMIGRATION_MIN,
    check_plan,
    finalize_plan,
    plan_itinerary,
    score_candidate,
)

FIXTURE = Path(__file__).parent / "fixtures" / "itinerary" / "tokyo_candidates.json"
ARRIVAL = datetime(2026, 11, 20, 14, 0)
DEPARTURE = datetime(2026, 11, 23, 19, 0)
SHINJUKU = (35.6905, 139.7000)
TRIP_DATES = [date(2026, 11, 20) + timedelta(days=i) for i in range(4)]


def _fx_handler(request: httpx.Request) -> httpx.Response:
    base, quote = request.url.params["base"], request.url.params["quotes"]
    return httpx.Response(200, json=[{"base": base, "quote": quote, "rate": fx.static_rate(base, quote)}])


@pytest.fixture(autouse=True)
def fx_mock() -> Iterator[respx.MockRouter]:
    """Frankfurter answers with the static table; any other HTTP call fails the test."""
    with respx.mock(assert_all_called=False) as router:
        router.get(fx.FRANKFURTER_V2_URL).mock(side_effect=_fx_handler)
        yield router


def load_candidates() -> list[Candidate]:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return [Candidate(**row) for row in data["pois"] + data["restaurants"]]


def tokyo_inputs(**overrides: Any) -> PlanInputs:
    data: dict[str, Any] = {
        "destination": "TYO", "city": "Tokyo", "country": "JP", "currency": "JPY",
        "timezone": "Asia/Tokyo", "center_lat": 35.6812, "center_lon": 139.7671,
        "arrival_at": ARRIVAL, "departure_at": DEPARTURE,
        "arrival_airport": "NRT", "departure_airport": "NRT", "adults": 2,
        "style_mode": "percent",
        "style_weights": {"sightseeing": 40, "food": 30, "shopping": 10, "relaxation": 20},
        "pace": "moderate", "food_wishlist": ["sushi", "라멘", "yakitori"],
        "hotel_area": "Shinjuku", "hotel_lat": SHINJUKU[0], "hotel_lon": SHINJUKU[1],
        "language": "en", "budget_basis": "per_person", "budget_currency": "KRW",
        "budget_total": 1_500_000, "budget_flight": 350_000,
        "flight_price_total": 600_000, "flight_currency": "KRW",
    }
    data.update(overrides)
    return PlanInputs(**data)


@pytest.fixture(scope="module")
def candidates() -> list[Candidate]:
    return load_candidates()


@pytest.fixture
def plan(candidates: list[Candidate]) -> ItineraryPlan:
    return plan_itinerary(tokyo_inputs(), candidates)


def _abs_minutes(day_date: date, hhmm: str) -> datetime:
    return datetime.combine(day_date, datetime.min.time()) + timedelta(minutes=hhmm_to_minutes(hhmm))


def _items(plan: ItineraryPlan, kind: str) -> list[tuple[date, PlanItem]]:
    return [(d.date, i) for d in plan.days for i in d.items if i.kind == kind]


# --------------------------------------------------------------------------- structure


def test_plan_has_four_days_and_validates(plan: ItineraryPlan) -> None:
    assert [d.date for d in plan.days] == TRIP_DATES
    assert plan.generator == "rule_based"
    assert plan.currency_local == "JPY"
    assert plan.destination == "Tokyo"
    assert plan.cost_basis == "per_person"  # type: ignore[attr-defined]
    again = ItineraryPlan.model_validate(json.loads(plan.model_dump_json()))
    assert again.model_dump() == plan.model_dump()
    assert len(_items(plan, "activity")) >= 10
    assert all(d.theme for d in plan.days)


def test_arrival_and_departure_blocks(plan: ItineraryPlan) -> None:
    first, last = plan.days[0], plan.days[-1]
    assert [i.kind for i in first.items[:3]] == ["arrival", "transit", "hotel"]
    assert first.items[0].start == "14:00"
    assert first.items[0].end == "15:30"  # 90 min immigration & baggage
    assert first.items[1].travel_from_prev is not None
    assert first.items[1].travel_from_prev.distance_km > 50  # Narita -> Shinjuku
    assert [i.kind for i in last.items[-3:]] == ["hotel", "transit", "departure"]
    assert last.items[-1].end == "19:00"
    assert last.items[-1].lat == pytest.approx(35.772, abs=0.01)  # NRT


def test_nothing_before_arrival_or_after_airport_cutoff(plan: ItineraryPlan) -> None:
    ready = ARRIVAL + timedelta(minutes=IMMIGRATION_MIN)
    for item in plan.days[0].items:
        assert _abs_minutes(plan.days[0].date, item.start) >= ARRIVAL
        if item.kind in ("activity", "meal", "free"):
            assert _abs_minutes(plan.days[0].date, item.start) >= ready + timedelta(minutes=60)
    cutoff = DEPARTURE - timedelta(minutes=AIRPORT_CUTOFF_MIN)
    for item in plan.days[-1].items:
        if item.kind not in ("transit", "departure"):
            assert _abs_minutes(plan.days[-1].date, item.end) <= cutoff, item.title
    activities_last_day = [i for i in plan.days[-1].items if i.kind == "activity"]
    assert activities_last_day, "the last morning should still be used"


def test_lunch_and_dinner_present(plan: ItineraryPlan) -> None:
    def meals(day_index: int, meal: str) -> list[PlanItem]:
        return [i for i in plan.days[day_index].items if i.kind == "meal" and getattr(i, "meal", "") == meal]

    assert meals(0, "dinner"), "arrival evening dinner"
    for index in (1, 2):
        assert len(meals(index, "lunch")) == 1
        assert len(meals(index, "dinner")) == 1
    assert len(meals(3, "lunch")) == 1 and not meals(3, "dinner")
    for _, item in _items(plan, "meal"):
        start = hhmm_to_minutes(item.start)
        if item.meal == "lunch":  # type: ignore[attr-defined]
            assert 11 * 60 <= start <= 14 * 60 + 30
        if item.meal == "dinner":  # type: ignore[attr-defined]
            assert 17 * 60 + 30 <= start <= 20 * 60 + 30


def test_food_wishes_and_no_repeated_restaurants(plan: ItineraryPlan) -> None:
    meals = [i for _, i in _items(plan, "meal") if getattr(i, "meal", "") != "breakfast"]
    cuisines = {getattr(i, "cuisine", None) for i in meals}
    assert {"sushi", "ramen"} <= cuisines
    named = [i.candidate_id for i in meals if i.candidate_id]
    assert len(named) == len(set(named))


def test_items_carry_links_costs_and_travel(plan: ItineraryPlan) -> None:
    for day in plan.days:
        for item in day.items:
            if item.kind in ("activity", "meal", "arrival", "departure", "hotel"):
                assert item.map_url and item.map_url.startswith("https://www.google.com/maps/")
            if item.kind == "activity":
                assert item.est_cost is not None and item.cost_currency == "JPY"
                assert item.travel_from_prev is not None
                assert item.candidate_id and item.lat is not None and item.lng is not None
                assert "maps/dir/" in item.directions_url  # type: ignore[attr-defined]
            if item.kind == "meal" and item.candidate_id:
                assert "tabelog.com" in item.links["tabelog"]  # type: ignore[attr-defined]
        assert day.est_day_cost is not None and day.est_day_cost > 0
    assert plan.sources and all(s.url.startswith("http") for s in plan.sources)


def test_timeline_is_consistent(plan: ItineraryPlan) -> None:
    assert check_plan(plan, tokyo_inputs()) == []
    for day in plan.days:
        ends = [hhmm_to_minutes(i.end) for i in day.items if i.kind != "departure"]
        starts = [hhmm_to_minutes(i.start) for i in day.items if i.kind != "departure"]
        assert all(starts[k + 1] >= ends[k] for k in range(len(starts) - 1)), day.date


def test_closures_and_opening_hours_respected(plan: ItineraryPlan, candidates: list[Candidate]) -> None:
    by_id = {c.id: c for c in candidates}
    holidays = calendar_util.holidays_for("JP", [2026])
    for day in plan.days:
        for item in day.items:
            candidate = by_id.get(item.candidate_id or "")
            if candidate is None:
                continue
            assert not calendar_util.is_closed(candidate, day.date, holidays), (candidate.name, day.date)
            windows = calendar_util.open_windows(candidate, day.date, holidays)
            if windows:
                start, end = hhmm_to_minutes(item.start), hhmm_to_minutes(item.end)
                assert any(o <= start and end <= c + 10 for o, c in windows), (candidate.name, windows)
    tsukiji_days = [d.date for d in plan.days for i in d.items if i.title == "Tsukiji Outer Market"]
    assert date(2026, 11, 22) not in tsukiji_days  # closed on Sundays
    assert date(2026, 11, 23) not in tsukiji_days  # closed on public holidays


def test_route_is_grouped_by_area(plan: ItineraryPlan) -> None:
    metrics = plan.route_metrics
    assert metrics.backtrack_hops <= 1
    assert metrics.max_hop_km < 10
    assert metrics.avg_hop_km < 4
    assert metrics.total_travel_minutes > 0
    for day in plan.days:
        points = [(i.lat, i.lng) for i in day.items if i.kind == "activity"]
        if len(points) >= 2:
            centre = routing.centroid(points)  # type: ignore[arg-type]
            assert max(routing.distance_km(centre, p) for p in points) < 6.5, day.date  # type: ignore[arg-type]
    assert routing.route_metrics(plan) == metrics


def test_plan_is_deterministic(candidates: list[Candidate]) -> None:
    first = plan_itinerary(tokyo_inputs(), candidates)
    second = plan_itinerary(tokyo_inputs(), candidates)
    shuffled = list(candidates)
    random.Random(7).shuffle(shuffled)
    third = plan_itinerary(tokyo_inputs(), shuffled)
    assert first.model_dump() == second.model_dump() == third.model_dump()


def test_holiday_is_reported(plan: ItineraryPlan) -> None:
    assert plan.days[-1].holiday == "Labor Thanksgiving Day"  # type: ignore[attr-defined]
    assert any("public holiday" in w for w in plan.warnings)


def test_budget_summary(plan: ItineraryPlan) -> None:
    budget = plan.budget
    assert budget is not None and budget.currency == "KRW"
    lines = {line.category: line for line in budget.lines}
    assert lines["flight"].estimated == pytest.approx(300_000)  # 600k total / 2 adults
    assert lines["flight"].budget == 350_000
    assert lines["hotel"].estimated and lines["hotel"].budget is None
    assert lines["food"].estimated and lines["transport"].estimated and lines["activities"].estimated
    assert budget.within_budget is True
    assert budget.total_estimated == pytest.approx(sum(l.estimated or 0 for l in budget.lines), abs=2)
    assert budget.fx == {"JPY->KRW": "live"}  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- preferences


def test_scoring_follows_style_weights(candidates: list[Candidate]) -> None:
    by_name = {c.name: c for c in candidates}
    shopping = {k: 0.0 for k in ("sightseeing", "food", "relaxation", "culture")} | {"shopping": 1.0}
    culture = {"culture": 1.0, "shopping": 0.0}
    assert score_candidate(by_name["Ginza"], shopping) > score_candidate(by_name["Tokyo National Museum"], shopping)
    assert score_candidate(by_name["Tokyo National Museum"], culture) > score_candidate(by_name["Ginza"], culture)
    family = score_candidate(by_name["Shinjuku Golden Gai"], {"nightlife": 1.0}, "family")
    solo = score_candidate(by_name["Shinjuku Golden Gai"], {"nightlife": 1.0}, "solo")
    assert family < solo


def test_must_visit_and_avoid(candidates: list[Candidate]) -> None:
    plan = plan_itinerary(
        tokyo_inputs(must_visit=["teamLab Planets", "Atlantis Museum"], avoid=["Tokyo Tower", "shopping"]),
        candidates,
    )
    titles = {i.title for _, i in _items(plan, "activity")}
    assert "teamLab Planets TOKYO" in titles
    assert "Tokyo Tower" not in titles
    assert not any(i.category == "shopping" for _, i in _items(plan, "activity"))  # type: ignore[attr-defined]
    assert any("Atlantis Museum" in w for w in plan.warnings)


def test_exclude_ids(candidates: list[Candidate]) -> None:
    senso = next(c for c in candidates if c.name == "Senso-ji")
    plan = plan_itinerary(tokyo_inputs(), candidates, exclude_ids=[senso.id])
    assert senso.id not in {i.candidate_id for d in plan.days for i in d.items}


def test_restaurant_budget_limits_choices(candidates: list[Candidate]) -> None:
    plan = plan_itinerary(
        tokyo_inputs(restaurant_budget_max=1500, restaurant_budget_currency="JPY"), candidates
    )
    for _, item in _items(plan, "meal"):
        if item.candidate_id and getattr(item, "meal", "") != "breakfast":
            assert item.est_cost <= 1500 * 1.15, item.title


def test_unlimited_budget_uses_mid_range(candidates: list[Candidate]) -> None:
    plan = plan_itinerary(
        tokyo_inputs(budget_total=0, budget_flight=-1, budget_food=-1, budget_hotel=None), candidates
    )
    lines = {line.category: line for line in plan.budget.lines}  # type: ignore[union-attr]
    assert lines["flight"].unlimited and lines["flight"].budget is None
    assert lines["food"].unlimited and lines["food"].estimated
    assert not lines["hotel"].unlimited and lines["hotel"].budget is None
    assert plan.budget.total_budget is None and plan.budget.within_budget is None  # type: ignore[union-attr]
    assert plan.budget.unlimited_total is True  # type: ignore[union-attr]
    assert _items(plan, "meal")


def test_rain_keeps_outdoor_sights_off_the_wet_day(candidates: list[Candidate]) -> None:
    by_id = {c.id: c for c in candidates}
    wet = date(2026, 11, 21)
    weather = {d: {"summary": "Clear", "precip_prob": 5, "precip_mm": 0.0, "source": "forecast"} for d in TRIP_DATES}
    weather[wet] = {"summary": "Heavy rain", "precip_prob": 95, "precip_mm": 25.0, "source": "forecast"}
    dry_plan = plan_itinerary(tokyo_inputs(), candidates)
    wet_plan = plan_itinerary(tokyo_inputs(), candidates, weather=weather)

    def outdoor_on(plan: ItineraryPlan) -> int:
        day = next(d for d in plan.days if d.date == wet)
        return sum(1 for i in day.items if i.kind == "activity" and by_id[i.candidate_id].is_outdoor)  # type: ignore[index]

    assert outdoor_on(wet_plan) <= outdoor_on(dry_plan)
    assert outdoor_on(wet_plan) == 0
    assert wet_plan.days[1].weather["precip_prob"] == 95  # type: ignore[index]
    assert any("Open-Meteo" in a for a in wet_plan.attributions)  # type: ignore[attr-defined]


def test_hotel_assumed_when_location_unknown(candidates: list[Candidate]) -> None:
    plan = plan_itinerary(tokyo_inputs(hotel_lat=None, hotel_lon=None), candidates)
    assert plan.hotel["assumed"] is True  # type: ignore[attr-defined]
    assert any("Hotel location unknown" in w for w in plan.warnings)
    hotel = (plan.hotel["lat"], plan.hotel["lng"])  # type: ignore[attr-defined]
    assert routing.distance_km(hotel, SHINJUKU) < 2.5  # centroid of the "Shinjuku" candidates


def test_korean_texts(candidates: list[Candidate]) -> None:
    plan = plan_itinerary(tokyo_inputs(language="ko"), candidates)
    assert plan.title == "Tokyo 4일 여행"
    assert plan.days[0].items[0].title == "NRT 도착 (입국 심사·수하물)"
    assert any(i.title.startswith("점심") for _, i in _items(plan, "meal"))
    assert "1인 기준" in (plan.budget.notes or "")  # type: ignore[union-attr]


# --------------------------------------------------------------------------- edge cases


def test_no_candidates_still_gives_a_valid_plan() -> None:
    plan = plan_itinerary(tokyo_inputs(hotel_lat=None, hotel_lon=None), [])
    assert [d.date for d in plan.days] == TRIP_DATES
    assert not _items(plan, "activity")
    meals = _items(plan, "meal")
    assert meals and all("tabelog" in i.links for _, i in meals)  # type: ignore[attr-defined]
    assert _items(plan, "free")
    assert any("Only 0 sights" in w for w in plan.warnings)
    assert check_plan(plan, tokyo_inputs()) == []


def test_late_arrival_and_early_departure(candidates: list[Candidate]) -> None:
    inputs = tokyo_inputs(arrival_at=datetime(2026, 11, 20, 23, 30), departure_at=datetime(2026, 11, 22, 8, 0),
                          arrival_airport="HND", departure_airport="HND", pace="packed")
    plan = plan_itinerary(inputs, candidates)
    assert check_plan(plan, inputs) == []
    assert not [i for i in plan.days[0].items if i.kind == "activity"]
    assert [i.kind for i in plan.days[-1].items] == ["hotel", "transit", "departure"]
    middle = [i for i in plan.days[1].items if i.kind == "activity"]
    assert middle and hhmm_to_minutes(middle[0].start) >= 8 * 60


def test_same_day_trip(candidates: list[Candidate]) -> None:
    inputs = tokyo_inputs(arrival_at=datetime(2026, 11, 21, 7, 0), departure_at=datetime(2026, 11, 21, 22, 0),
                          arrival_airport="HND", departure_airport="HND")
    plan = plan_itinerary(inputs, candidates)
    assert len(plan.days) == 1
    kinds = [i.kind for i in plan.days[0].items]
    assert kinds[:3] == ["arrival", "transit", "hotel"] and kinds[-3:] == ["hotel", "transit", "departure"]
    assert "activity" in kinds
    assert check_plan(plan, inputs) == []


def test_other_city_without_tabelog() -> None:
    paris = [
        Candidate(name=n, category=c, lat=la, lon=lo, area=a, coord_source="nominatim")
        for n, c, la, lo, a in [
            ("Louvre Museum", "museum", 48.8606, 2.3376, "1er"), ("Eiffel Tower", "viewpoint", 48.8584, 2.2945, "7e"),
            ("Notre-Dame", "historic", 48.8530, 2.3499, "4e"), ("Musée d'Orsay", "museum", 48.8600, 2.3266, "7e"),
            ("Montmartre", "neighborhood", 48.8867, 2.3431, "18e"), ("Le Marais", "neighborhood", 48.8590, 2.3620, "3e"),
            ("Arc de Triomphe", "historic", 48.8738, 2.2950, "8e"), ("Jardin du Luxembourg", "park", 48.8462, 2.3372, "6e"),
            ("Bistrot Paul Bert", "restaurant", 48.8522, 2.3830, "11e"),
            ("Le Comptoir du Relais", "restaurant", 48.8521, 2.3389, "6e"),
        ]
    ]
    inputs = PlanInputs(
        destination="PAR", city="Paris", country="FR", currency="EUR", center_lat=48.8566, center_lon=2.3522,
        arrival_at="2026-05-01T08:30", departure_at="2026-05-03T18:00", arrival_airport="CDG",
        departure_airport="ORY", language="ko", budget_currency="KRW", budget_total=-1, budget_flight=-1,
        adults=2,
    )
    plan = plan_itinerary(inputs, paris)
    assert len(plan.days) == 3 and plan.currency_local == "EUR"
    activities = _items(plan, "activity")
    assert len(activities) == 8
    assert all("tabelog" not in getattr(i, "links", {}) for _, i in _items(plan, "meal"))
    assert all(i.cost_currency == "EUR" for _, i in activities)
    assert check_plan(plan, inputs) == []
    assert plan.budget is not None and plan.budget.currency == "KRW"


def test_finalize_plan_recomputes_derived_fields(plan: ItineraryPlan, candidates: list[Candidate]) -> None:
    inputs = tokyo_inputs()
    edited = plan.model_copy(deep=True)
    day = edited.days[2]
    activities = [k for k, i in enumerate(day.items) if i.kind == "activity"]
    a, b = activities[0], activities[-1]
    day.items[a], day.items[b] = day.items[b], day.items[a]  # an LLM reorder without fixing times
    for item in day.items:
        item.travel_from_prev = None
        item.map_url = None
        item.lat = item.lng = None
    final = finalize_plan(edited, inputs, candidates)
    located = [i for i in final.days[2].items if i.kind == "activity"]
    assert all(i.lat is not None and i.map_url for i in located)
    assert all(i.travel_from_prev is not None for i in located)
    assert final.route_metrics != plan.route_metrics
    assert any("Not enough time" in w or "returns to an earlier area" in w for w in check_plan(final, inputs))
    assert final.budget is not None


def test_check_plan_tolerates_bad_llm_times(plan: ItineraryPlan) -> None:
    broken = plan.model_copy(deep=True)
    broken.days[1].items[2].start = "noon"
    warnings = check_plan(broken, tokyo_inputs())
    assert any("Invalid time" in w for w in warnings)
