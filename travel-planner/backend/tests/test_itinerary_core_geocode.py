"""Nominatim geocoding: cache (hits and misses), rate limit, plausibility, LLM fallback."""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
import respx
from sqlalchemy.orm import Session

from app.models import GeocodeCache
from app.services.itinerary import geocode
from app.services.itinerary.models import Candidate

TOKYO = (35.6812, 139.7671)
SENSOJI = {"lat": "35.7148", "lon": "139.7967", "display_name": "浅草寺, 台東区, 東京都, 日本"}
OSAKA = {"lat": "34.6937", "lon": "135.5023", "display_name": "Osaka"}
OSAKA_CENTRE = (34.6937, 135.5023)
OSAKA_CASTLE = {"lat": "34.6873", "lon": "135.5262", "display_name": "大阪城"}


class FakeClock:
    """Monotonic clock that only advances when someone sleeps."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture(autouse=True)
def clock(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeClock]:
    fake = FakeClock()
    monkeypatch.setattr(geocode, "_monotonic", fake.monotonic)
    monkeypatch.setattr(geocode, "_sleep", fake.sleep)
    geocode.reset_rate_limiter()
    yield fake
    geocode.reset_rate_limiter()


def _by_query(mapping: dict[str, list[dict]]):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=mapping.get(request.url.params.get("q", ""), []))
    return handler


def test_cache_key_normalization() -> None:
    assert geocode.cache_key("  Sensō-ji ", "Tokyo", "JP") == geocode.cache_key("senso-ji", "tokyo", "jp")
    assert geocode.cache_key("浅草寺", "Tokyo", "JP") == "浅草寺|tokyo|jp"


def test_geocode_hit_request_shape_and_cache(db: Session, clock: FakeClock) -> None:
    with respx.mock as router:
        route = router.get(geocode.NOMINATIM_URL).mock(return_value=httpx.Response(200, json=[SENSOJI]))
        point = geocode.geocode(db, "浅草寺", "Tokyo", "JP", TOKYO, language="ko")
        again = geocode.geocode(db, " 浅草寺 ", "tokyo", "jp", TOKYO)
    assert point is not None and point.source == "nominatim"
    assert (point.lat, point.lon) == (35.7148, 139.7967) and again == point
    assert route.call_count == 1  # second lookup served from GeocodeCache
    request = route.calls.last.request
    assert request.headers["User-Agent"] == "AiSquaredTravel/0.1 (personal use)"
    params = request.url.params
    assert params["q"] == "浅草寺" and params["countrycodes"] == "jp" and params["bounded"] == "1"
    assert params["format"] == "jsonv2" and params["accept-language"] == "ko"
    west, north, east, south = (float(v) for v in params["viewbox"].split(","))
    assert west < TOKYO[1] < east and south < TOKYO[0] < north
    row = db.get(GeocodeCache, geocode.cache_key("浅草寺", "Tokyo", "JP"))
    assert row is not None and row.source == "nominatim" and row.lat == 35.7148


def test_misses_and_far_hits_are_cached_as_none(db: Session) -> None:
    with respx.mock as router:
        route = router.get(geocode.NOMINATIM_URL).mock(side_effect=_by_query({"Osaka Castle": [OSAKA]}))
        assert geocode.geocode(db, "Nowhere Cafe", "Tokyo", "JP", TOKYO) is None
        assert geocode.geocode(db, "Osaka Castle", "Tokyo", "JP", TOKYO) is None  # 400 km away
        assert geocode.geocode(db, "Nowhere Cafe", "Tokyo", "JP", TOKYO) is None
    assert route.call_count == 2
    key = geocode.cache_key("Nowhere Cafe", "Tokyo", "JP")
    assert db.get(GeocodeCache, key) is None  # a bounded miss is scoped to its search area
    row = db.get(GeocodeCache, geocode.miss_key(key, TOKYO))
    assert row is not None and row.source == "none" and row.lat is None
    assert geocode.miss_key(key, TOKYO) == "nowhere cafe|tokyo|jp@35.7,139.8"
    assert geocode.miss_key(key, None) == key


def test_miss_around_a_wrong_centre_is_retried(db: Session) -> None:
    """A miss recorded while searching around the wrong city (e.g. Tokyo for an Osaka trip)
    must not hide the place from a later search around the right centre."""
    with respx.mock as router:
        route = router.get(geocode.NOMINATIM_URL).mock(
            side_effect=lambda request: httpx.Response(
                200, json=[OSAKA_CASTLE] if float(request.url.params["viewbox"].split(",")[1]) < 36 else []
            )
        )
        assert geocode.geocode(db, "Osaka Castle", "Osaka", "JP", (35.6812, 139.7671)) is None  # bbox ~Tokyo
        found = geocode.geocode(db, "Osaka Castle", "Osaka", "JP", OSAKA_CENTRE)
        assert found is not None and found.lat == pytest.approx(34.6873)
        again = geocode.geocode(db, "Osaka Castle", "Osaka", "JP", (35.6812, 139.7671))
    assert route.call_count == 2 and again == found  # hits are reused from any centre


def test_unbounded_misses_use_the_plain_key(db: Session) -> None:
    with respx.mock as router:
        route = router.get(geocode.NOMINATIM_URL).mock(return_value=httpx.Response(200, json=[]))
        assert geocode.geocode(db, "Nowhere", "Tokyo", "JP") is None
        assert geocode.geocode(db, "Nowhere", "Tokyo", "JP") is None
    assert route.call_count == 1 and "viewbox" not in route.calls.last.request.url.params
    assert db.get(GeocodeCache, geocode.cache_key("Nowhere", "Tokyo", "JP")).source == "none"  # type: ignore[union-attr]


def test_rate_limit_spacing(db: Session, clock: FakeClock) -> None:
    with respx.mock as router:
        router.get(geocode.NOMINATIM_URL).mock(return_value=httpx.Response(200, json=[]))
        for name in ("a1", "b2", "c3"):
            geocode.geocode(db, name, "Tokyo", "JP", TOKYO)
    assert clock.sleeps == [pytest.approx(geocode.MIN_INTERVAL_S)] * 2


def test_geocode_candidates(db: Session) -> None:
    candidates = [
        Candidate(name="Senso-ji", name_local="浅草寺", category="historic", lat=35.0, lon=139.0),
        Candidate(name="Ueno Park", category="park", lat=35.71, lon=139.77, coord_source="osm"),
        Candidate(name="Secret Bar", category="nightlife", lat=35.69, lon=139.70),
        Candidate(name="Moon Base", category="attraction", lat=10.0, lon=10.0),
        Candidate(name="Ghost Cafe", category="cafe"),
    ]
    mapping = {"浅草寺": [SENSOJI]}
    with respx.mock as router:
        route = router.get(geocode.NOMINATIM_URL).mock(side_effect=_by_query(mapping))
        located = geocode.geocode_candidates(db, candidates, "Tokyo", "JP", TOKYO)
    queries = [call.request.url.params["q"] for call in route.calls]
    assert queries == ["浅草寺", "Secret Bar", "Moon Base", "Ghost Cafe"]  # name_local first; OSM skipped
    senso, ueno, bar, moon, ghost = located
    assert senso.coord_source == "nominatim" and senso.point == (35.7148, 139.7967)
    assert ueno is candidates[1]
    assert bar.coord_source == "llm" and bar.point == (35.69, 139.70)
    assert moon.coord_source == "none" and moon.point is None  # implausible LLM coordinates dropped
    assert ghost.coord_source == "none"
    assert candidates[0].lat == 35.0  # inputs are not mutated


def test_refusal_pauses_the_batch(db: Session) -> None:
    candidates = [Candidate(name=f"Place {i}") for i in range(4)]
    with respx.mock as router:
        route = router.get(geocode.NOMINATIM_URL).mock(return_value=httpx.Response(429))
        located = geocode.geocode_candidates(db, candidates, "Tokyo", "JP", TOKYO)
    assert route.call_count == 1
    assert all(c.coord_source == "none" for c in located)
    assert db.query(GeocodeCache).count() == 0  # refusals are not cached


def test_request_budget(db: Session) -> None:
    candidates = [Candidate(name=f"Spot {i}") for i in range(5)]
    with respx.mock as router:
        route = router.get(geocode.NOMINATIM_URL).mock(return_value=httpx.Response(200, json=[]))
        geocode.geocode_candidates(db, candidates, "Tokyo", "JP", TOKYO, max_requests=3)
    assert route.call_count == 3


def test_geocode_city(db: Session) -> None:
    with respx.mock as router:
        route = router.get(geocode.NOMINATIM_URL).mock(
            return_value=httpx.Response(200, json=[{"lat": "35.6769", "lon": "139.7639", "display_name": "東京都"}])
        )
        point = geocode.geocode_city(db, "Tokyo", "JP")
        assert geocode.geocode_city(db, "tokyo", "jp") == point
    assert route.call_count == 1 and route.calls.last.request.url.params["city"] == "Tokyo"
    assert point is not None and point.lat == pytest.approx(35.6769)


@pytest.mark.live
def test_live_nominatim(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(geocode, "_monotonic", __import__("time").monotonic)
    monkeypatch.setattr(geocode, "_sleep", __import__("time").sleep)
    point = geocode.geocode(db, "浅草寺", "Tokyo", "JP", TOKYO, language="ko")
    assert point is not None and abs(point.lat - 35.7148) < 0.01 and abs(point.lon - 139.7967) < 0.01


def test_resolve_plan_locations_geocodes_airport_centres_and_hotel_areas(db: Session) -> None:
    from datetime import date

    from app.models import Trip
    from app.services.itinerary.models import build_plan_inputs

    daegu = {"lat": "35.8714", "lon": "128.6014", "display_name": "대구광역시"}
    dongseongno = {"lat": "35.8690", "lon": "128.5960", "display_name": "동성로"}

    def handler(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        if params.get("city") == "Daegu":
            return httpx.Response(200, json=[daegu])
        return httpx.Response(200, json=[dongseongno] if params.get("q") == "동성로" else [])

    trip = Trip(name="t", origin="ICN", destination="TAE", depart_date=date(2026, 11, 20), currency="KRW")
    with respx.mock as router:
        route = router.get(geocode.NOMINATIM_URL).mock(side_effect=handler)
        inputs = build_plan_inputs(trip, None, arrival_at="2026-11-20T10:00", departure_at="2026-11-22T18:00",
                                   hotel_area="동성로", db=db)
        assert route.call_count == 2
        hotel_params = route.calls.last.request.url.params
        west, north, east, south = (float(v) for v in hotel_params["viewbox"].split(","))
        assert west < 128.6014 < east and south < 35.8714 < north  # searched around the city, not the airport
        again = build_plan_inputs(trip, None, arrival_at="2026-11-20T10:00", departure_at="2026-11-22T18:00",
                                  hotel_area="동성로", db=db)
        assert route.call_count == 2  # both cached
    assert inputs.center == (35.8714, 128.6014) and inputs.center_source == "geocoded"
    assert inputs.hotel_point == (35.869, 128.596) and inputs.hotel_approximate is True
    assert again == inputs


def test_resolve_plan_locations_rejects_namesakes_and_survives_outages(db: Session) -> None:
    from datetime import datetime

    from app.services.itinerary.models import PlanInputs

    inputs = PlanInputs(destination="TAE", city="Daegu", country="KR", currency="KRW", center_lat=35.8944,
                        center_lon=128.657, center_source="airport", arrival_at=datetime(2026, 11, 20, 10),
                        departure_at=datetime(2026, 11, 22, 18), hotel_area="Nowhere")
    with respx.mock as router:
        router.get(geocode.NOMINATIM_URL).mock(return_value=httpx.Response(200, json=[OSAKA]))  # 300 km away
        far = geocode.resolve_plan_locations(db, inputs)
    assert far.center_source == "airport" and far.hotel_point is None
    with respx.mock as router:
        router.get(geocode.NOMINATIM_URL).mock(return_value=httpx.Response(200, text="<html>oops"))
        assert geocode.resolve_plan_locations(db, inputs.model_copy(update={"city": "Busan"})).center_source == "airport"
    given = inputs.model_copy(update={"center_source": "given", "hotel_area": None})
    assert geocode.resolve_plan_locations(db, given) is given  # nothing to resolve, no request
