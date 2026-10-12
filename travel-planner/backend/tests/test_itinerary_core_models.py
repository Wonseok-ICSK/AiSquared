"""Candidate / PlanInputs validation and helpers."""

from __future__ import annotations

from datetime import date, datetime

import pytest
from pydantic import ValidationError

from app.models import TravelPreference, Trip
from app.services.itinerary.models import (
    DEFAULT_VISIT_MINUTES,
    Candidate,
    OpeningPeriod,
    PlanInputs,
    build_plan_inputs,
    canonical_cuisine,
    hhmm_to_minutes,
    make_candidate_id,
    minutes_to_hhmm,
    normalize_key,
    normalize_text,
)


def test_candidate_is_lenient_with_llm_output() -> None:
    c = Candidate(
        name="  Sensō-ji ", category="Temple", styles=["Culture", "photo", "unknown", "culture"],
        best_time="MORNING", coord_source="Nominatim", est_cost=-1, rating=0, area="",
        closed_weekdays=[0, 0, 9, 2.0], source_urls=["https://a.example", "ftp://x", "https://a.example", ""],
        lat=35.71, lon=139.79, opening_hours=[],
    )
    assert c.name == "Sensō-ji"
    assert c.category == "historic"
    assert c.styles == ["culture", "photography"]
    assert c.best_time == "morning" and c.coord_source == "nominatim"
    assert c.est_cost is None and c.rating is None and c.area is None
    assert c.closed_weekdays == [0, 2]
    assert c.source_urls == ["https://a.example"]
    assert c.opening_hours is None
    assert c.visit_minutes == DEFAULT_VISIT_MINUTES["historic"]
    assert c.id == make_candidate_id("Sensō-ji", "historic", point=(35.71, 139.79)) and c.id.startswith("senso-ji-")
    assert c.id != make_candidate_id("Sensō-ji", "historic")


def test_candidate_defaults_and_coordinates() -> None:
    unknown = Candidate(name="Somewhere", category="spaceport", visit_minutes=5000)
    assert unknown.category == "attraction" and unknown.visit_minutes == 600
    assert unknown.coord_source == "none" and unknown.point is None and not unknown.has_coords
    llm = Candidate(name="X", lat=35.0, lon=139.0)
    assert llm.coord_source == "llm" and llm.point == (35.0, 139.0)
    half = Candidate(name="Y", lat=35.0, coord_source="nominatim")
    assert half.lat is None and half.coord_source == "none"
    assert Candidate(name="浅草寺").id.startswith("c-")
    with pytest.raises(ValidationError):
        Candidate(name="")


def test_candidate_helpers() -> None:
    ramen = Candidate(name="Ichiran", category="Restaurant", cuisine="ramen;japanese")
    assert ramen.is_food and ramen.cuisines == ["ramen", "japanese"]
    assert Candidate(name="스시 오마카세 긴자", category="restaurant").cuisines == ["sushi"]
    park = Candidate(name="Ueno Park", category="park")
    assert park.effective_styles == ["nature", "relaxation"] and not park.is_food
    assert Candidate(name="Ueno Park", name_local="上野公園").search_terms() == ["uenopark", "上野公園"]


def test_opening_period() -> None:
    assert OpeningPeriod(weekday=0, open="9:30", close="17:00").minutes() == (570, 1020)
    assert OpeningPeriod(weekday=4, open="19:00", close="02:00").minutes() == (1140, 1560)
    assert OpeningPeriod(weekday=4, open="00:00", close="24:00").minutes() == (0, 1440)
    with pytest.raises(ValidationError):
        OpeningPeriod(weekday=7, open="09:00", close="10:00")


def test_text_helpers() -> None:
    assert normalize_key("  Sensō-ji   TEMPLE ") == "senso-ji temple"
    assert normalize_text("Sensō-ji Temple") == "sensojitemple"
    assert normalize_text("センソウジ") == "センソウジ"  # kana voicing marks are kept
    assert canonical_cuisine("초밥") == "sushi" and canonical_cuisine("Tonkotsu ramen") == "ramen"
    assert canonical_cuisine("yakitori") == "yakitori" and canonical_cuisine("") is None
    assert canonical_cuisine("quantum food") is None
    assert hhmm_to_minutes("09:05") == 545 and minutes_to_hhmm(545) == "09:05"
    assert minutes_to_hhmm(1470) == "00:30"
    with pytest.raises(ValueError):
        hhmm_to_minutes("25:00")


def test_plan_inputs_validation() -> None:
    inputs = PlanInputs(
        destination="tyo", city="Tokyo", country="jp", currency="jpy", center_lat=35.68, center_lon=139.77,
        arrival_at="2026-11-20T14:00:30", departure_at="2026-11-23T19:00", style_mode="score",
        hotel_lat=35.69,
    )
    assert inputs.destination == "TYO" and inputs.country == "JP" and inputs.currency == "JPY"
    assert inputs.arrival_at == datetime(2026, 11, 20, 14, 0)
    assert inputs.style_weights["food"] == 3.0  # score-mode default
    assert inputs.dates == [date(2026, 11, 20), date(2026, 11, 21), date(2026, 11, 22), date(2026, 11, 23)]
    assert inputs.nights == 3 and inputs.hotel_point is None
    with pytest.raises(ValidationError):
        PlanInputs(destination="TYO", city="Tokyo", country="JP", currency="JPY", center_lat=0, center_lon=0,
                   arrival_at="2026-11-23T19:00", departure_at="2026-11-20T14:00")
    dumped = inputs.model_dump(mode="json")
    assert dumped["arrival_at"] == "2026-11-20T14:00:00"
    assert PlanInputs.model_validate(dumped) == inputs


def test_build_plan_inputs_from_orm_rows() -> None:
    trip = Trip(name="t", origin="ICN", destination="TYO", depart_date=date(2026, 11, 20),
                return_date=date(2026, 11, 23), adults=2, currency="KRW")
    prefs = TravelPreference(
        style_mode="percent", style_weights={"food": 60, "sightseeing": 40}, companion="couple", pace="packed",
        food_wishlist=["sushi"], preferred_cuisines=[], avoid_foods=[], must_visit=["Senso-ji"], avoid=[],
        hotel_area="Shinjuku", budget_basis="per_person", budget_currency="KRW", budget_total=1_500_000,
        budget_flight=350_000, budget_food=-1,
    )
    inputs = build_plan_inputs(trip, prefs, arrival_at="2026-11-20T14:00", departure_at="2026-11-23T19:00",
                               arrival_airport="NRT", departure_airport="NRT", flight_price_total=560_000)
    assert (inputs.city, inputs.country, inputs.currency, inputs.timezone) == ("Tokyo", "JP", "JPY", "Asia/Tokyo")
    assert inputs.center == (35.6812, 139.7671)
    assert inputs.adults == 2 and inputs.pace == "packed" and inputs.companion == "couple"
    assert inputs.style_weights["food"] == 60 and inputs.must_visit == ["Senso-ji"]
    assert inputs.hotel_area == "Shinjuku" and inputs.budget_food == -1
    assert inputs.flight_price_total == 560_000
    bare = build_plan_inputs(trip, None, arrival_at=datetime(2026, 11, 20, 14), departure_at=datetime(2026, 11, 23, 19),
                             hotel_point=(35.69, 139.70), language="en")
    assert bare.budget_total is None and bare.budget_currency == "KRW" and bare.hotel_point == (35.69, 139.70)
    seoul_trip = Trip(name="t", origin="NRT", destination="ICN", depart_date=date(2026, 11, 20), currency="KRW")
    seoul = build_plan_inputs(seoul_trip, None, arrival_at="2026-11-20T10:00", departure_at="2026-11-21T10:00")
    assert seoul.city == "Seoul" and seoul.currency == "KRW"
    with pytest.raises(ValueError):
        build_plan_inputs(Trip(name="t", origin="ICN", destination="QQQ", depart_date=date(2026, 1, 1)), None,
                          arrival_at="2026-01-01T10:00", departure_at="2026-01-02T10:00")


def test_canonical_cuisine_never_matches_inside_longer_synonyms() -> None:
    # ordinary words used to hit unrelated cuisines ("tea" -> steak, "bar" -> barbecue, ...)
    for word in ("tea", "bar", "beef", "pan", "steakhouse", "ice"):
        assert canonical_cuisine(word) is None, word
    assert canonical_cuisine("beef bowl") == "gyudon" and canonical_cuisine("korean bbq") == "korean_bbq"
    assert canonical_cuisine("sushi restaurant") == "sushi" and canonical_cuisine("라멘맛집") == "ramen"
    assert canonical_cuisine("dim sum") == "chinese" and canonical_cuisine("pho bo") == "vietnamese"


def _trip(destination: str) -> Trip:
    return Trip(name="t", origin="ICN", destination=destination, depart_date=date(2026, 11, 20), currency="KRW")


def test_city_centre_follows_the_airport_actually_used() -> None:
    times = {"arrival_at": "2026-11-20T14:00", "departure_at": "2026-11-23T19:00"}
    for code in ("NRT", "HND"):  # the Tokyo override applies to every airport of the metro
        inputs = build_plan_inputs(_trip(code), None, **times)
        assert inputs.center == (35.6812, 139.7671) and inputs.center_source == "known"
    osaka = build_plan_inputs(_trip("TYO"), None, arrival_airport="KIX", **times)  # alternative destination
    assert osaka.city == "Osaka" and osaka.center == (34.6937, 135.5023) and osaka.center_source == "metro"
    istanbul = build_plan_inputs(_trip("IST"), None, **times)
    assert istanbul.center == (41.0082, 28.9784) and istanbul.center_source == "known"
    assert build_plan_inputs(_trip("ATH"), None, **times).city == "Athens"  # not the airport's suburb
    daegu = build_plan_inputs(_trip("TAE"), None, **times)  # plain airport without a known centre
    assert daegu.center_source == "airport" and daegu.center == (35.8944, 128.657)
    given = build_plan_inputs(_trip("TAE"), None, center=(35.87, 128.6), **times)
    assert given.center == (35.87, 128.6) and given.center_source == "given"
