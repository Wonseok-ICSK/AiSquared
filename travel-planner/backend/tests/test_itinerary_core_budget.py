"""Budget semantics, cost defaults, spending caps and the budget summary."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime
from typing import Any

import httpx
import pytest
import respx

from app.core import fx
from app.schemas import ItineraryPlan, PlanDay, PlanItem, TravelHop
from app.services.itinerary import budget
from app.services.itinerary.models import PlanInputs

JPY_KRW = 9.0


def _ok_handler(request: httpx.Request) -> httpx.Response:
    base, quote = request.url.params["base"], request.url.params["quotes"]
    rate = JPY_KRW if (base, quote) == ("JPY", "KRW") else fx.static_rate(base, quote)
    return httpx.Response(200, json=[{"base": base, "quote": quote, "rate": rate}])


@pytest.fixture
def live_fx() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as router:
        router.get(fx.FRANKFURTER_V2_URL).mock(side_effect=_ok_handler)
        yield router


@pytest.fixture
def offline_fx() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as router:
        router.get(fx.FRANKFURTER_V2_URL).mock(side_effect=httpx.ConnectError("offline"))
        router.get(fx.FRANKFURTER_LEGACY_URL).mock(side_effect=httpx.ConnectError("offline"))
        yield router


def inputs(**overrides: Any) -> PlanInputs:
    data: dict[str, Any] = {
        "destination": "TYO", "city": "Tokyo", "country": "JP", "currency": "JPY",
        "center_lat": 35.68, "center_lon": 139.77,
        "arrival_at": datetime(2026, 11, 20, 14), "departure_at": datetime(2026, 11, 23, 19),
        "adults": 2, "language": "en", "budget_currency": "KRW", "budget_basis": "total",
        "budget_total": 1_500_000, "budget_flight": 600_000,
    }
    data.update(overrides)
    return PlanInputs(**data)


def plan() -> ItineraryPlan:
    hop = TravelHop(mode="transit", minutes=20, distance_km=4)
    items = [
        PlanItem(start="15:30", end="17:18", kind="transit", title="to hotel", est_cost=3000,
                 cost_currency="JPY", travel_from_prev=TravelHop(mode="transit", minutes=108, distance_km=63)),
        PlanItem(start="18:00", end="19:00", kind="activity", title="Skytree", est_cost=2600,
                 cost_currency="JPY", travel_from_prev=hop),
        PlanItem(start="19:20", end="20:30", kind="meal", title="Dinner", est_cost=3000,
                 cost_currency="JPY", travel_from_prev=hop),
        PlanItem(start="21:00", end="21:00", kind="meal", title="Snack", est_cost=10, cost_currency="USD"),
    ]
    return ItineraryPlan(title="t", destination="Tokyo", currency_local="JPY",
                         days=[PlanDay(date=date(2026, 11, 20), items=items)])


def test_budget_value_semantics() -> None:
    assert budget.budget_value(None) == (None, False)
    assert budget.budget_value(0) == (None, True)
    assert budget.budget_value(-1) == (None, True)
    assert budget.budget_value(1500) == (1500.0, False)


def test_local_cost_defaults_are_round_and_country_specific() -> None:
    tokyo = budget.local_cost_defaults("JP", "JPY")
    assert tokyo.currency == "JPY"
    assert 1500 <= tokyo.lunch <= 3500 and tokyo.lunch == budget.nice_amount(tokyo.lunch)
    assert tokyo.dinner > tokyo.lunch > tokyo.breakfast
    assert tokyo.activity("park") == 0 and tokyo.activity("theme_park") > tokyo.activity("museum") > 0
    assert 2500 <= tokyo.airport_transfer(63) <= 4500
    bangkok = budget.local_cost_defaults("TH", "THB")
    assert bangkok.lunch < budget.static_convert(tokyo.lunch, "JPY", "THB")  # type: ignore[operator]
    unknown = budget.local_cost_defaults("XX", "XXX")
    assert unknown.currency == "USD"


def test_summary_total_basis(live_fx: respx.MockRouter) -> None:
    summary = budget.build_budget_summary(inputs(), plan(), flight_cost=560_000, flight_currency="KRW")
    lines = {line.category: line for line in summary.lines}
    assert [line.category for line in summary.lines] == list(budget.BUDGET_CATEGORIES)
    assert lines["flight"].estimated == 560_000 and lines["flight"].budget == 600_000
    defaults = budget.local_cost_defaults("JP", "JPY")
    assert lines["hotel"].estimated == pytest.approx(defaults.hotel_night * 3 * JPY_KRW)  # 1 room x 3 nights
    usd_krw = fx.static_rate("USD", "KRW")
    assert lines["food"].estimated == pytest.approx((3000 * JPY_KRW + 10 * usd_krw) * 2, abs=1)
    expected_transport = (3000 + defaults.transit_ride * 2) * 2 * JPY_KRW
    assert lines["transport"].estimated == pytest.approx(expected_transport, abs=1)
    assert lines["activities"].estimated == pytest.approx(2600 * 2 * JPY_KRW)
    assert lines["shopping"].estimated is None and not lines["shopping"].unlimited
    assert summary.total_budget == 1_500_000
    assert summary.within_budget is True
    assert summary.basis == "total" and summary.adults == 2  # type: ignore[attr-defined]
    assert summary.remaining_after_flight == 940_000  # type: ignore[attr-defined]
    assert "static" not in (summary.notes or "")


def test_summary_per_person_and_over_budget(live_fx: respx.MockRouter) -> None:
    summary = budget.build_budget_summary(
        inputs(budget_basis="per_person", budget_total=300_000, budget_flight=250_000,
               budget_hotel=150_000, budget_shopping=50_000),
        plan(), flight_cost=560_000, flight_currency="KRW",
    )
    lines = {line.category: line for line in summary.lines}
    assert lines["flight"].estimated == 280_000  # per person
    assert lines["hotel"].estimated == 150_000  # the user's hotel budget
    assert lines["shopping"].estimated == 50_000 and lines["shopping"].budget == 50_000
    assert summary.within_budget is False
    assert set(budget.over_budget_lines(summary)) == {"flight", "total"}
    assert "per person" in (summary.notes or "")


def test_unlimited_and_unspecified(live_fx: respx.MockRouter) -> None:
    summary = budget.build_budget_summary(
        inputs(budget_total=0, budget_flight=-1, budget_food=0, budget_hotel=None), plan()
    )
    lines = {line.category: line for line in summary.lines}
    assert lines["flight"].unlimited and lines["flight"].budget is None
    assert lines["flight"].estimated is None  # flight price unknown
    assert lines["food"].unlimited and lines["food"].estimated
    assert not lines["hotel"].unlimited and lines["hotel"].budget is None
    assert summary.total_budget is None and summary.within_budget is None
    assert summary.unlimited_total is True  # type: ignore[attr-defined]
    assert "Flight price unknown" in (summary.notes or "")
    assert "mid-range" in (summary.notes or "")


def test_static_fx_fallback_is_noted(offline_fx: respx.MockRouter) -> None:
    summary = budget.build_budget_summary(inputs(), plan(), flight_cost=560_000)
    lines = {line.category: line for line in summary.lines}
    assert lines["activities"].estimated == pytest.approx(2600 * 2 * fx.static_rate("JPY", "KRW"), abs=1)
    assert summary.fx["JPY->KRW"] == "static"  # type: ignore[attr-defined]
    assert "Approximate offline exchange rates" in (summary.notes or "")


def test_unknown_currency_is_excluded(live_fx: respx.MockRouter) -> None:
    live_fx.get(fx.FRANKFURTER_LEGACY_URL).mock(return_value=httpx.Response(404))
    odd = plan()
    odd.days[0].items.append(PlanItem(start="21:00", end="21:30", kind="activity", title="x",
                                      est_cost=5, cost_currency="XXX"))
    summary = budget.build_budget_summary(inputs(language="ko"), odd, flight_cost=1)
    assert "XXX" in (summary.notes or "")
    assert "합계" in (summary.notes or "")


def test_spending_caps() -> None:
    defaults = budget.local_cost_defaults("JP", "JPY")
    restaurant = budget.spending_caps(
        inputs(restaurant_budget_max=5000, restaurant_budget_currency="JPY"), defaults
    )
    assert restaurant.source == "restaurant_budget" and restaurant.meal["dinner"] == 5000
    food = budget.spending_caps(inputs(budget_food=400_000, budget_basis="total"), defaults)
    assert food.source == "food_budget"
    per_day = 400_000 * fx.static_rate("KRW", "JPY") / 2 / 4  # type: ignore[operator]
    assert food.meal["dinner"] == pytest.approx(per_day * 0.5)
    total = budget.spending_caps(inputs(), defaults)
    assert total.source == "total_budget" and total.activities_total and total.meal["lunch"]
    unlimited = budget.spending_caps(inputs(budget_total=-1, budget_flight=0), defaults)
    assert unlimited.source == "none" and unlimited.activities_total is None
    assert all(v is None for v in unlimited.meal.values())
    broke = budget.spending_caps(inputs(budget_total=500_000, budget_flight=600_000), defaults)
    assert broke.source == "none"  # nothing left after flight + hotel: mid-range, reported over budget


def test_rooms_needed() -> None:
    assert budget.rooms_needed(1) == 1 and budget.rooms_needed(2) == 1 and budget.rooms_needed(3) == 2
