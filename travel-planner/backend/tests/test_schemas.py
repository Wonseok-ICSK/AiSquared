from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta, timezone
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app import schemas as s
from app.models import Trip


def trip(**overrides: Any) -> s.TripCreate:
    data: dict[str, Any] = {
        "origin": "ICN",
        "destination": "NRT",
        "depart_date": "2026-11-20",
        "return_date": "2026-11-24",
    }
    data.update(overrides)
    return s.TripCreate.model_validate(data)


def prefs(**overrides: Any) -> s.PreferencesIn:
    data: dict[str, Any] = {"budget_total": 1_500_000, "budget_flight": 600_000}
    data.update(overrides)
    return s.PreferencesIn.model_validate(data)


# ----------------------------------------------------------------------------- constants


def test_constants() -> None:
    assert s.STYLE_KEYS == [
        "sightseeing", "food", "relaxation", "hotel_resort", "shopping",
        "nightlife", "nature", "photography", "culture", "local_experience",
    ]
    assert s.CABINS == ["economy", "premium_economy", "business", "first"]
    assert s.TRIP_TYPES == ["round_trip", "one_way"]
    assert s.PRICE_BASES == ["per_person", "total"]
    assert s.ALERT_KINDS == ["below_target", "price_drop", "new_low"]
    assert s.NOTIFICATION_CHANNELS == ["inapp", "desktop", "telegram", "email", "ntfy", "kakao"]
    assert s.FLEX_DAYS == [0, 1, 3, 7]


# ----------------------------------------------------------------------------- trips


def test_trip_defaults_and_normalisation() -> None:
    t = trip(origin=" sel ", destination="tyo", currency="jpy", outbound_depart_from="8:05")
    assert (t.origin, t.destination, t.currency) == ("SEL", "TYO", "JPY")
    assert t.outbound_depart_from == "08:05"
    assert t.name == "SEL → TYO"
    assert t.schedule_times == ["08:30", "17:30"]


def test_round_trip_requires_return_date() -> None:
    with pytest.raises(ValidationError, match="return_date is required"):
        trip(return_date=None)


def test_round_trip_return_not_before_depart() -> None:
    with pytest.raises(ValidationError, match="on or after"):
        trip(return_date="2026-11-19")
    assert trip(return_date="2026-11-20").return_date == date(2026, 11, 20)


def test_one_way_forces_return_fields_to_none() -> None:
    t = trip(trip_type="one_way", return_date="2026-11-01", flex_return_days=3,
             return_depart_from="10:00")
    assert t.return_date is None and t.flex_return_days == 0 and t.return_depart_from is None


@pytest.mark.parametrize("adults", [0, 3])
def test_adults_range(adults: int) -> None:
    with pytest.raises(ValidationError):
        trip(adults=adults)


@pytest.mark.parametrize("value", ["25:00", "12:60", "8:5", "noon", "1230"])
def test_invalid_hhmm(value: str) -> None:
    with pytest.raises(ValidationError):
        trip(return_arrive_to=value)


def test_blank_time_window_is_none() -> None:
    assert trip(outbound_arrive_to="").outbound_arrive_to is None


@pytest.mark.parametrize("flex", [2, 5, -1])
def test_flex_days_restricted(flex: int) -> None:
    with pytest.raises(ValidationError):
        trip(flex_depart_days=flex)
    assert trip(flex_depart_days=7).flex_depart_days == 7


def test_schedule_times_validation() -> None:
    assert trip(schedule_times=["17:30", "8:30", "17:30"]).schedule_times == ["08:30", "17:30"]
    with pytest.raises(ValidationError):
        trip(schedule_times=[f"0{h}:00" for h in range(7)])
    with pytest.raises(ValidationError):
        trip(schedule_times=["08:30", "bad"])
    with pytest.raises(ValidationError, match="at least one"):
        trip(schedule_times=[])


def test_schedule_interval_bounds() -> None:
    with pytest.raises(ValidationError, match="schedule_interval_hours is required"):
        trip(schedule_mode="interval")
    for bad in (0.5, 169):
        with pytest.raises(ValidationError):
            trip(schedule_mode="interval", schedule_interval_hours=bad)
    assert trip(schedule_mode="interval", schedule_interval_hours=1).schedule_interval_hours == 1
    assert trip(schedule_mode="interval", schedule_interval_hours=168).schedule_interval_hours == 168


@pytest.mark.parametrize("code", ["IC", "ICNN", "1CN", ""])
def test_invalid_iata(code: str) -> None:
    with pytest.raises(ValidationError):
        trip(origin=code)


@pytest.mark.parametrize("currency", ["KR", "KRWW", "12$"])
def test_invalid_currency(currency: str) -> None:
    with pytest.raises(ValidationError):
        trip(currency=currency)


def test_same_origin_destination_rejected() -> None:
    with pytest.raises(ValidationError):
        trip(destination="ICN")


def test_alt_destinations_normalised() -> None:
    t = trip(alt_destinations=["kix", "KIX", "nrt", "HND"])
    assert t.alt_destinations == ["KIX", "HND"]


def test_trip_update_merge() -> None:
    current = trip().model_dump()
    merged = s.TripUpdate(trip_type="one_way").merged_with(current)
    assert merged.return_date is None
    with pytest.raises(ValidationError):
        s.TripUpdate(return_date=date(2026, 11, 1)).merged_with(current)
    with pytest.raises(ValidationError):
        s.TripUpdate(adults=5)


def test_trip_out_from_orm(db: Session) -> None:
    row = Trip(name="x", origin="ICN", destination="NRT", depart_date=date(2026, 11, 20),
               return_date=date(2026, 11, 24))
    db.add(row)
    db.commit()
    out = s.TripOut.model_validate(row)
    payload = json.loads(out.model_dump_json())
    assert payload["created_at"].endswith("Z")
    assert payload["latest_min_price"] is None and payload["unread_alerts"] == 0


# ----------------------------------------------------------------------------- datetimes


def test_utc_datetime_serialisation() -> None:
    naive = datetime(2026, 10, 6, 7, 5, 9, 123456)
    aware = datetime(2026, 10, 6, 16, 5, 9, tzinfo=timezone(timedelta(hours=9)))
    a = s.PricePoint(run_id=1, checked_at=naive)
    b = s.PricePoint(run_id=1, checked_at=aware)
    assert json.loads(a.model_dump_json())["checked_at"] == "2026-10-06T07:05:09Z"
    assert json.loads(b.model_dump_json())["checked_at"] == "2026-10-06T07:05:09Z"
    assert a.model_dump()["checked_at"] == naive  # python mode keeps datetimes
    out = s.ProviderTestOut(key="mock", status="running", started_at=datetime.now(UTC))
    assert json.loads(out.model_dump_json())["finished_at"] is None


# ----------------------------------------------------------------------------- preferences


def test_preferences_require_budgets() -> None:
    with pytest.raises(ValidationError):
        s.PreferencesIn.model_validate({"budget_flight": 1})
    with pytest.raises(ValidationError):
        s.PreferencesIn.model_validate({"budget_total": 1})
    with pytest.raises(ValidationError):
        prefs(budget_total=-5)
    assert prefs(budget_total=-1, budget_flight=0).budget_total == -1


def test_style_weights_keys() -> None:
    with pytest.raises(ValidationError, match="unknown style keys"):
        prefs(style_weights={"food": 50, "karaoke": 10})
    filled = prefs(style_weights={"food": 60, "culture": 40}).style_weights
    assert list(filled) == s.STYLE_KEYS
    assert filled["food"] == 60 and filled["nature"] == 0


def test_style_weights_percent_mode() -> None:
    with pytest.raises(ValidationError, match="sum to 100"):
        prefs(style_weights={"food": 60, "culture": 50})
    with pytest.raises(ValidationError):
        prefs(style_weights={"food": 101})
    with pytest.raises(ValidationError):
        prefs(style_weights={"food": -1})
    assert sum(prefs().style_weights.values()) == 100


def test_style_weights_score_mode() -> None:
    assert prefs(style_mode="score", style_weights={"food": 5, "culture": 4}).style_weights["food"] == 5
    with pytest.raises(ValidationError, match="between 0 and 5"):
        prefs(style_mode="score", style_weights={"food": 6})


def test_restaurant_budget_order() -> None:
    with pytest.raises(ValidationError):
        prefs(restaurant_budget_min=5000, restaurant_budget_max=1000, restaurant_budget_currency="jpy")
    assert prefs(restaurant_budget_currency="jpy").restaurant_budget_currency == "JPY"


# ----------------------------------------------------------------------------- alerts, itineraries, settings


def test_alert_rule_validation() -> None:
    with pytest.raises(ValidationError, match="drop_percent"):
        s.AlertRuleCreate(kind="price_drop")
    assert s.AlertRuleCreate(kind="price_drop", drop_percent=10).drop_percent == 10
    with pytest.raises(ValidationError):
        s.AlertRuleCreate(kind="price_drop", drop_percent=0)
    with pytest.raises(ValidationError):
        s.AlertRuleCreate(kind="sometimes")


def test_itinerary_create_sources() -> None:
    assert s.ItineraryCreate(offer_id=3).use_ai is True
    manual = s.ItineraryCreate(arrival_at="2026-11-20T11:25:00", departure_at="2026-11-24 18:00")
    assert (manual.arrival_at, manual.departure_at) == ("2026-11-20T11:25", "2026-11-24T18:00")
    with pytest.raises(ValidationError):
        s.ItineraryCreate()
    with pytest.raises(ValidationError):
        s.ItineraryCreate(arrival_at="2026-11-20T11:25")
    with pytest.raises(ValidationError):
        s.ItineraryCreate(arrival_at="2026-11-24T11:25", departure_at="2026-11-20T18:00")
    with pytest.raises(ValidationError):
        s.ItineraryCreate(arrival_at="2026-11-20T11:25+09:00", departure_at="2026-11-24T18:00")
    with pytest.raises(ValidationError):
        s.ItineraryRefine(feedback="   ")


def test_itinerary_plan_shape() -> None:
    plan = s.ItineraryPlan.model_validate(
        {
            "title": "Tokyo 4 days",
            "destination": "Tokyo",
            "currency_local": "JPY",
            "days": [
                {
                    "date": "2026-11-20",
                    "items": [
                        {"start": "12:00", "end": "13:00", "kind": "meal", "title": "Ramen",
                         "travel_from_prev": {"mode": "walk", "minutes": 8, "distance_km": 0.6}}
                    ],
                }
            ],
        }
    )
    assert plan.days[0].items[0].travel_from_prev.minutes == 8
    with pytest.raises(ValidationError):
        s.PlanItem(start="1", end="2", kind="party", title="x")


def test_runtime_settings_in_partial() -> None:
    update = s.RuntimeSettingsIn.model_validate({"api_keys": {"serpapi": ""}})
    assert update.model_dump(exclude_unset=True) == {"api_keys": {"serpapi": ""}}
    with pytest.raises(ValidationError):
        s.RuntimeSettingsIn(llm_effort="extreme")


def test_meta_out_defaults() -> None:
    meta = s.MetaOut(version="0.1.0", defaults=s.MetaDefaults(currency="KRW", timezone="Asia/Seoul"))
    assert meta.style_keys == s.STYLE_KEYS
    assert meta.defaults.style_weights["food"] == 10
    assert meta.defaults.score_style_weights == s.DEFAULT_STYLE_SCORES


# ----------------------------------------------------------------------------- review fixes


def test_score_mode_without_weights_uses_score_defaults() -> None:
    p = prefs(style_mode="score")
    assert p.style_weights == s.DEFAULT_STYLE_SCORES
    assert all(0 <= v <= s.STYLE_SCORE_MAX for v in p.style_weights.values())
    assert prefs().style_weights == s.DEFAULT_STYLE_WEIGHTS  # percent default unchanged
    with pytest.raises(ValidationError, match="between 0 and 5"):  # explicit weights still checked
        prefs(style_mode="score", style_weights=s.DEFAULT_STYLE_WEIGHTS)


@pytest.mark.parametrize("value", [-0.5, -0.01, -1.5])
def test_fractional_negative_budget_rejected(value: float) -> None:
    with pytest.raises(ValidationError):
        prefs(budget_total=value)
    with pytest.raises(ValidationError):
        prefs(budget_hotel=value)
    assert prefs(budget_hotel=-1).budget_hotel == -1 and prefs(budget_hotel=0).budget_hotel == 0


def test_alert_rule_update_merge_revalidates() -> None:
    below = s.AlertRuleCreate(kind="below_target", threshold=250000).model_dump()
    with pytest.raises(ValidationError, match="drop_percent"):
        s.AlertRuleUpdate(kind="price_drop").merged_with(below)
    merged = s.AlertRuleUpdate(kind="price_drop", drop_percent=15).merged_with(below)
    assert (merged.kind, merged.drop_percent, merged.threshold) == ("price_drop", 15, 250000)

    drop = s.AlertRuleCreate(kind="price_drop", drop_percent=10).model_dump()
    with pytest.raises(ValidationError, match="drop_percent"):
        s.AlertRuleUpdate(drop_percent=None).merged_with(drop)
    update = s.AlertRuleUpdate(enabled=False, armed=True)
    assert update.merged_with({**drop, "armed": False, "id": 7}).enabled is False


def test_out_models_validate_from_dataclasses_and_attributes() -> None:
    from app.providers.flights.base import FlightLeg, FlightSegment

    segment = FlightSegment("KE", "Korean Air", "KE703", "ICN", "NRT",
                            "2026-11-20T09:05", "2026-11-20T11:25", 140)
    leg = FlightLeg("ICN", "NRT", "2026-11-20T09:05", "2026-11-20T11:25", 140, 0,
                    ["Korean Air"], ["KE703"], [segment])
    out = s.FlightLegOut.model_validate(leg)
    assert out.segments[0].flight_number == "KE703"
    assert s.AirportOut.model_validate(
        type("A", (), {"code": "ICN", "name": "Incheon", "city": "Seoul", "country": "KR",
                       "lat": 37.46, "lon": 126.44})()
    ).code == "ICN"
    response_models = [
        obj for obj in vars(s).values()
        if isinstance(obj, type) and issubclass(obj, s.BaseModel)
        and (obj.__name__.endswith("Out") or obj.__name__ in {"ProviderInfo", "ItineraryPlan"})
    ]
    assert response_models
    for model in response_models:
        assert model.model_config.get("from_attributes") is True, model.__name__


def test_trip_currency_defaults_to_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import get_settings

    assert trip().currency == "KRW"
    monkeypatch.setenv("DEFAULT_CURRENCY", "usd")
    get_settings.cache_clear()
    assert get_settings().default_currency == "USD"
    assert trip().currency == "USD"
    assert trip(currency="JPY").currency == "JPY"
    assert prefs().budget_currency == "USD"


@pytest.mark.parametrize(
    ("origin", "destination"), [("SEL", "ICN"), ("GMP", "SEL"), ("TYO", "NRT"), ("osa", "KIX")]
)
def test_same_metro_origin_destination_rejected(origin: str, destination: str) -> None:
    with pytest.raises(ValidationError, match="must differ"):
        trip(origin=origin, destination=destination)


def test_alt_destinations_drop_origin_and_covered_codes() -> None:
    t = trip(origin="SEL", destination="TYO", alt_destinations=["GMP", "ICN", "NRT", "HND", "OSA", "FUK"])
    assert t.alt_destinations == ["OSA", "FUK"]
    t = trip(origin="ICN", destination="NRT", alt_destinations=["SEL", "TYO", "HND"])
    assert t.alt_destinations == ["TYO", "HND"]  # TYO adds HND; SEL overlaps the origin


def test_trip_update_regenerates_auto_name() -> None:
    current = trip().model_dump()
    assert current["name"] == "ICN → NRT"
    assert s.TripUpdate(destination="KIX").merged_with(current).name == "ICN → KIX"
    assert s.TripUpdate(destination="KIX", name="Osaka").merged_with(current).name == "Osaka"
    custom = trip(name="Tokyo autumn").model_dump()
    assert s.TripUpdate(destination="KIX").merged_with(custom).name == "Tokyo autumn"
    assert s.TripUpdate(destination="KIX", name="").merged_with(custom).name == "ICN → KIX"


def test_runtime_settings_llm_model_blank_means_reset() -> None:
    assert s.RuntimeSettingsIn(llm_model="  ").llm_model is None
    assert s.RuntimeSettingsIn(llm_model=" claude-opus-5-5 ").llm_model == "claude-opus-5-5"
    assert s.RuntimeSettingsIn.model_validate({"llm_model": ""}).model_dump(exclude_unset=True) == {
        "llm_model": None
    }


def test_provider_info_enabled_flags() -> None:
    info = s.ProviderInfo(key="mock", display_name="Mock", configured=True,
                          enabled_by_default=False, enabled=False)
    assert info.enabled is False and info.enabled_by_default is False
    assert s.ProviderInfo(key="x", display_name="X", configured=True).enabled is True
