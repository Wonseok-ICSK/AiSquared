"""OSM opening-hours parser, Overpass source and candidate merging."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from app.services.itinerary import candidates as cands
from app.services.itinerary.candidates import (
    OverpassSource,
    StaticSource,
    build_overpass_query,
    cuisine_regex,
    merge_candidates,
    name_matches,
    parse_osm_opening_hours,
)
from app.services.itinerary.models import Candidate, PlanInputs

FIXTURE = Path(__file__).parent / "fixtures" / "itinerary" / "overpass_asakusa.json"
PRIMARY, MIRROR = cands.OVERPASS_URLS


def inputs(**overrides: Any) -> PlanInputs:
    data: dict[str, Any] = {
        "destination": "TYO", "city": "Tokyo", "country": "JP", "currency": "JPY",
        "center_lat": 35.7148, "center_lon": 139.7967,
        "arrival_at": datetime(2026, 11, 20, 14), "departure_at": datetime(2026, 11, 23, 19),
        "food_wishlist": ["초밥", "ramen"], "language": "ko",
    }
    data.update(overrides)
    return PlanInputs(**data)


def payload() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- opening hours


def test_parse_osm_opening_hours() -> None:
    periods, closed = parse_osm_opening_hours("Tu-Su 09:30-17:00; PH 09:30-17:00")  # type: ignore[misc]
    assert closed == [0] and len(periods) == 6 and periods[0].open == "09:30"
    periods, closed = parse_osm_opening_hours("Mo-Fr 11:00-14:00, 17:00-22:00; Sa 11:00-22:00")  # type: ignore[misc]
    assert closed == [6] and len([p for p in periods if p.weekday == 0]) == 2
    periods, closed = parse_osm_opening_hours("24/7")  # type: ignore[misc]
    assert closed == [] and periods[0].minutes() == (0, 1440)
    periods, closed = parse_osm_opening_hours("10:00-20:00; We off")  # type: ignore[misc]
    assert closed == [2] and len(periods) == 6
    periods, _ = parse_osm_opening_hours("Fr-Sa 18:00-02:00")  # type: ignore[misc]
    assert periods[0].minutes() == (1080, 1560)
    periods, closed = parse_osm_opening_hours("Su-Tu 10:00-12:00")  # type: ignore[misc]
    assert sorted(p.weekday for p in periods) == [0, 1, 6] and closed == [2, 3, 4, 5]
    for unsupported in ("sunrise-sunset", "Apr-Oct Mo-Fr 09:00-17:00", "Mo-Fr 09:00+", "", None, "PH off"):
        assert parse_osm_opening_hours(unsupported) is None


# --------------------------------------------------------------------------- overpass


def test_cuisine_regex_and_query() -> None:
    assert cuisine_regex(inputs()) == "sushi|ramen"
    assert cuisine_regex(inputs(food_wishlist=[], preferred_cuisines=[])).startswith("japanese|sushi")
    assert cuisine_regex(inputs(country="TH", food_wishlist=[])) == "thai"
    assert cuisine_regex(inputs(country="XX", food_wishlist=["삼겹살"])) == "korean|barbecue"
    query = build_overpass_query((35.70, 139.78, 35.73, 139.81), "sushi|ramen", timeout_s=30)
    assert query.startswith("[out:json][timeout:30][bbox:35.70000,139.78000,35.73000,139.81000];")
    assert '["cuisine"~"sushi|ramen",i]' in query and ".poi out center tags" in query


def test_overpass_fetch_parses_and_ranks() -> None:
    with respx.mock as router:
        route = router.post(PRIMARY).mock(return_value=httpx.Response(200, json=payload()))
        source = OverpassSource()
        found = source.fetch(inputs())
    assert route.call_count == 1 and source.requests_made == 1
    request = route.calls.last.request
    assert request.headers["User-Agent"] == cands.USER_AGENT
    assert "bbox:" in httpx.QueryParams(request.content.decode()).get("data", "")
    by_id = {c.id: c for c in found}
    assert "osm:node/1009" not in by_id and "osm:relation/3011" not in by_id  # no name / no geometry
    senso = by_id["osm:node/1001"]
    assert senso.name == "센소지" and senso.name_local == "浅草寺"  # language "ko" preferred
    assert senso.category == "historic" and senso.coord_source == "osm"
    assert senso.opening_hours and senso.opening_hours[0].open == "06:00"
    assert "https://www.openstreetmap.org/node/1001" in senso.source_urls
    assert "https://ja.wikipedia.org/wiki/浅草寺" in senso.source_urls
    museum = by_id["osm:node/1003"]
    assert museum.category == "museum" and museum.closed_weekdays == [0]
    assert by_id["osm:way/2002"].category == "park" and by_id["osm:way/2002"].is_outdoor
    assert by_id["osm:node/1008"].opening_hours is None
    assert by_id["osm:node/1008"].opening_hours_text == "sunrise-sunset"
    assert by_id["osm:node/1005"].cuisines == ["sushi"] and by_id["osm:node/1005"].is_food
    pois = [c for c in found if not c.is_food]
    assert pois[0].id in {"osm:node/1001", "osm:node/1003"}  # notable (wikidata + wikipedia) first
    assert pois[-1].id in {"osm:node/1004", "osm:node/1008"}  # no wikidata/wikipedia: ranked last


def test_overpass_two_areas_and_mirror_fallback() -> None:
    far_hotel = {"hotel_lat": 35.6896, "hotel_lon": 139.7006}
    with respx.mock as router:
        router.post(PRIMARY).mock(return_value=httpx.Response(200, json=payload()))
        source = OverpassSource()
        source.fetch(inputs(**far_hotel))
        assert router.calls.call_count == 2 and source.requests_made == 2
    with respx.mock as router:
        primary = router.post(PRIMARY).mock(return_value=httpx.Response(429))
        mirror = router.post(MIRROR).mock(return_value=httpx.Response(200, json=payload()))
        source = OverpassSource()
        found = source.fetch(inputs(**far_hotel))
        assert primary.call_count == 1 and mirror.call_count == 1  # never more than 2 requests
        assert found and source.last_error and "429" in source.last_error


def test_overpass_failure_returns_empty() -> None:
    with respx.mock as router:
        router.post(PRIMARY).mock(side_effect=httpx.ConnectTimeout("slow"))
        router.post(MIRROR).mock(return_value=httpx.Response(504))
        source = OverpassSource()
        assert source.fetch(inputs()) == []
        assert source.requests_made == 2 and source.last_error


def test_static_source() -> None:
    items = [Candidate(name="A"), Candidate(name="B")]
    assert StaticSource(items).fetch(inputs()) == items


# --------------------------------------------------------------------------- merging


def test_merge_deduplicates_and_fills_gaps() -> None:
    llm = [
        Candidate(name="Senso-ji Temple", name_local="浅草寺", category="historic", lat=35.7150, lon=139.7968,
                  description="Tokyo's oldest temple", source_urls=["https://a.example"], sources_verified=True,
                  styles=["culture"]),
        Candidate(name="Ichiran", category="restaurant", cuisine="ramen", lat=35.6610, lon=139.7004),
        Candidate(name="Tokyo National Museum", category="museum"),  # no coordinates
    ]
    osm = [
        Candidate(id="osm:node/1001", name="Senso-ji", name_local="浅草寺", category="historic", lat=35.71477,
                  lon=139.79666, coord_source="osm", opening_hours_text="06:00-17:00",
                  source_urls=["https://www.openstreetmap.org/node/1001"]),
        Candidate(id="osm:node/1006", name="Ichiran Asakusa", name_local="一蘭 浅草店", category="restaurant",
                  cuisine="ramen", lat=35.7120, lon=139.7955, coord_source="osm"),
        Candidate(id="osm:node/1003", name="Tokyo National Museum", category="museum", lat=35.7188, lon=139.7765,
                  coord_source="osm", closed_weekdays=[0]),
    ]
    merged = merge_candidates(llm, osm)
    assert len(merged) == 4
    senso = merged[0]
    assert senso.id == llm[0].id and senso.description == "Tokyo's oldest temple"
    assert senso.coord_source == "osm" and senso.lat == pytest.approx(35.71477)
    assert senso.opening_hours_text == "06:00-17:00" and senso.sources_verified
    assert senso.source_urls == ["https://a.example", "https://www.openstreetmap.org/node/1001"]
    museum = next(c for c in merged if c.name == "Tokyo National Museum")
    assert museum.point == (35.7188, 139.7765) and museum.closed_weekdays == [0]
    assert sum(1 for c in merged if "Ichiran" in c.name) == 2  # same chain, 6 km apart


def test_merge_same_name_far_apart_and_ids() -> None:
    a = Candidate(name="Starbucks", category="cafe", lat=35.66, lon=139.70)
    b = Candidate(name="Starbucks", category="cafe", lat=35.71, lon=139.79)
    c = Candidate(name="Starbucks", category="cafe", lat=35.6601, lon=139.7001)
    merged = merge_candidates([a], [b, c])
    assert len(merged) == 2
    assert merge_candidates([a, a]) == [a]


def test_name_matches() -> None:
    teamlab = Candidate(name="teamLab Planets TOKYO", name_local="チームラボプラネッツ")
    assert name_matches("teamLab Planets", teamlab)
    assert name_matches("チームラボプラネッツ", teamlab)
    assert not name_matches("Tokyo Tower", teamlab)
    assert not name_matches("t", teamlab)


@pytest.mark.live
def test_live_overpass_small_bbox() -> None:
    source = OverpassSource(radius_km=0.6, timeout_s=30)  # one area; the mirror only on failure
    found = source.fetch(inputs(food_wishlist=["tempura"]))
    assert 1 <= source.requests_made <= 2
    if not found:  # public instances are often overloaded (504/timeouts): degrade, don't crash
        assert source.last_error
        pytest.skip(f"Overpass unavailable: {source.last_error}")
    assert any(c.name_local == "浅草寺" or "Senso" in c.name for c in found)
    assert all(c.coord_source == "osm" and c.point is not None for c in found)


def test_overpass_searches_the_city_not_the_airport(db) -> None:  # type: ignore[no-untyped-def]
    """An airport-only centre (plain airport, no known city centre) is replaced by the
    geocoded city before the bounding boxes are built."""
    from app.services.itinerary import geocode

    singapore_airport = inputs(destination="SIN", city="Singapore", country="SG", currency="SGD",
                               center_lat=1.3502, center_lon=103.994, center_source="airport")
    city = [{"lat": "1.2903", "lon": "103.8520", "display_name": "Singapore"}]
    with respx.mock as router:
        nominatim = router.get(geocode.NOMINATIM_URL).mock(return_value=httpx.Response(200, json=city))
        overpass = router.post(PRIMARY).mock(return_value=httpx.Response(200, json={"elements": []}))
        OverpassSource().fetch(singapore_airport, db)
    assert nominatim.call_count == 1 and nominatim.calls.last.request.url.params["city"] == "Singapore"
    from urllib.parse import parse_qs

    query = parse_qs(overpass.calls.last.request.content.decode())["data"][0]
    south, west, north, east = (float(v) for v in query.split("[bbox:")[1].split("]")[0].split(","))
    assert south < 1.2903 < north and west < 103.8520 < east  # Marina Bay, not Changi (103.99)
    assert not (west < 103.994 < east)
