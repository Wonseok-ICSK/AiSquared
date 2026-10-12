"""Nominatim geocoding with a permanent cache (DESIGN §13.4).

Usage policy (https://operations.osmfoundation.org/policies/nominatim/): at most 1 request/s
(enforced process-wide by a lock + spacing of :data:`MIN_INTERVAL_S`), an identifying
``User-Agent`` (stock library UAs get HTTP 403), results cached — :class:`app.models.GeocodeCache`
keeps hits **and misses** (``source="none"``) forever. Searches are bounded to a viewbox around
the city centre and hits farther than :data:`MAX_DISTANCE_KM` are rejected. Attribution:
:data:`ATTRIBUTION` (ODbL).

Cache keys: hits are stored under ``"name|city|country"`` (DESIGN §4). A miss of a bounded
search depends on the search area, so it is stored under that key plus ``"@<lat>,<lon>"`` (the
centre rounded to 0.1°): a miss caused by a wrong centre never hides the place from a later
search around the right centre.

LLM-provided coordinates are only a fallback (``coord_source="llm"``, approximate) and are
never written to the cache. :func:`resolve_plan_locations` fixes airport-only city centres and
locates the hotel area of a :class:`PlanInputs`.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import httpx
from sqlalchemy.orm import Session

from app.models import GeocodeCache
from app.services.itinerary.models import Candidate, PlanInputs, normalize_key
from app.services.itinerary.routing import Coord, distance_km

logger = logging.getLogger(__name__)

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "AiSquaredTravel/0.1 (personal use)"
ATTRIBUTION = "Geocoding: Nominatim, © OpenStreetMap contributors (ODbL)"
MIN_INTERVAL_S = 1.1
HTTP_TIMEOUT_S = 15.0
VIEWBOX_HALF_KM = 40.0
MAX_DISTANCE_KM = 60.0
MAX_RESULTS = 5
DEFAULT_MAX_REQUESTS = 60

_rate_lock = threading.Lock()
_last_request_at: float | None = None
_monotonic = time.monotonic
_sleep = time.sleep


class GeocodeUnavailable(Exception):
    """Nominatim refused or is unreachable (403/429/5xx/network): stop querying for now."""


@dataclass(frozen=True)
class GeoPoint:
    """A geocoding result."""

    lat: float
    lon: float
    display_name: str | None
    source: str


def cache_key(name: str, city: str | None, country: str | None) -> str:
    """Normalized cache key ``"name|city|country"`` (case/accents/whitespace-insensitive)."""
    return "|".join(normalize_key(part) for part in (name, city or "", country or ""))[:400]


def miss_key(key: str, center: Coord | None) -> str:
    """Cache key of a miss: ``key`` scoped to the search area (centre rounded to 0.1°) when the
    search was bounded around ``center``; ``key`` itself for an unbounded search."""
    if center is None:
        return key
    return f"{key[:380]}@{center[0]:.1f},{center[1]:.1f}"


def reset_rate_limiter() -> None:
    """Forget the last request time (tests)."""
    global _last_request_at
    with _rate_lock:
        _last_request_at = None


def _viewbox(center: Coord, half_km: float = VIEWBOX_HALF_KM) -> str:
    lat, lon = center
    dlat = half_km / 110.57
    dlon = half_km / max(1e-6, 111.32 * math.cos(math.radians(lat)))
    return f"{lon - dlon:.5f},{lat + dlat:.5f},{lon + dlon:.5f},{lat - dlat:.5f}"


def _request(params: dict[str, str | int]) -> list[dict]:
    """One rate-limited Nominatim request (the lock serializes all callers)."""
    global _last_request_at
    with _rate_lock:
        if _last_request_at is not None:
            wait = _last_request_at + MIN_INTERVAL_S - _monotonic()
            if wait > 0:
                _sleep(wait)
        try:
            with httpx.Client(timeout=HTTP_TIMEOUT_S, headers={"User-Agent": USER_AGENT}) as client:
                response = client.get(NOMINATIM_URL, params=params)
        except httpx.HTTPError as exc:
            raise GeocodeUnavailable(f"Nominatim unreachable: {exc}") from exc
        finally:
            _last_request_at = _monotonic()
    if response.status_code in (403, 429) or response.status_code >= 500:
        raise GeocodeUnavailable(f"Nominatim HTTP {response.status_code}")
    if response.status_code != 200:
        logger.info("Nominatim HTTP %s for %r", response.status_code, params.get("q"))
        return []
    data = response.json()
    return data if isinstance(data, list) else []


def _pick(results: list[dict], center: Coord | None) -> GeoPoint | None:
    for row in results:
        try:
            lat, lon = float(row["lat"]), float(row["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        if center is not None and distance_km(center, (lat, lon)) > MAX_DISTANCE_KM:
            continue
        return GeoPoint(lat, lon, row.get("display_name"), "nominatim")
    return None


def _from_cache(row: GeocodeCache) -> GeoPoint | None:
    if row.lat is None or row.lon is None:
        return None
    return GeoPoint(row.lat, row.lon, row.display_name, row.source)


def _store(db: Session, key: str, point: GeoPoint | None, commit: bool) -> None:
    row = db.get(GeocodeCache, key) or GeocodeCache(query=key)
    row.lat = point.lat if point else None
    row.lon = point.lon if point else None
    row.display_name = point.display_name if point else None
    row.source = point.source if point else "none"
    db.add(row)
    if commit:
        db.commit()
    else:
        db.flush()


class _Budget:
    """Request allowance shared by one batch; trips to ``exhausted`` on refusal/outage."""

    def __init__(self, max_requests: int) -> None:
        self.left = max_requests
        self.exhausted = False

    def take(self) -> bool:
        if self.exhausted or self.left <= 0:
            return False
        self.left -= 1
        return True


def _lookup(
    db: Session,
    query: str,
    city: str | None,
    country: str | None,
    center: Coord | None,
    language: str,
    budget: _Budget,
    commit: bool,
) -> tuple[GeoPoint | None, bool]:
    """``(point, known)``: ``known`` is False when the query was neither cached nor sent.

    A cached hit is always used; a cached miss only for the same search area (see
    :func:`miss_key`), so a miss recorded around a wrong centre is retried."""
    key = cache_key(query, city, country)
    row = db.get(GeocodeCache, key)
    if row is not None and (row.lat is not None or center is None):
        return _from_cache(row), True
    scoped = miss_key(key, center)
    if center is not None and db.get(GeocodeCache, scoped) is not None:
        return None, True
    if not budget.take():
        return None, False
    params: dict[str, str | int] = {
        "q": query, "format": "jsonv2", "limit": MAX_RESULTS, "accept-language": language,
    }
    if country and len(country) == 2:
        params["countrycodes"] = country.lower()
    if center is not None:
        params["viewbox"] = _viewbox(center)
        params["bounded"] = 1
    try:
        point = _pick(_request(params), center)
    except GeocodeUnavailable as exc:
        logger.warning("Geocoding paused: %s", exc)
        budget.exhausted = True
        return None, False
    _store(db, key if point is not None else scoped, point, commit)
    return point, True


def geocode(
    db: Session,
    name: str,
    city: str | None,
    country: str | None,
    center: Coord | None = None,
    *,
    language: str = "en",
    commit: bool = True,
) -> GeoPoint | None:
    """Geocode one place name near ``center`` (cached; ``None`` = not found or unavailable)."""
    point, _ = _lookup(db, name, city, country, center, language, _Budget(1), commit)
    return point


def geocode_city(
    db: Session, city: str, country: str | None, *, language: str = "en", commit: bool = True
) -> GeoPoint | None:
    """City centre point (cached under ``"|city|country"``)."""
    key = cache_key("", city, country)
    row = db.get(GeocodeCache, key)
    if row is not None:
        return _from_cache(row)
    params: dict[str, str | int] = {
        "city": city, "format": "jsonv2", "limit": 1, "accept-language": language,
    }
    if country and len(country) == 2:
        params["countrycodes"] = country.lower()
    try:
        point = _pick(_request(params), None)
    except GeocodeUnavailable as exc:
        logger.warning("City geocoding failed: %s", exc)
        return None
    _store(db, key, point, commit)
    return point


def _queries(candidate: Candidate) -> list[str]:
    names = [candidate.name_local, candidate.name]
    return [n for n in dict.fromkeys(n.strip() for n in names if n and n.strip())]


def geocode_candidates(
    db: Session,
    candidates: Sequence[Candidate],
    city: str,
    country: str,
    center: Coord | None,
    *,
    language: str = "en",
    max_requests: int = DEFAULT_MAX_REQUESTS,
    commit: bool = True,
) -> list[Candidate]:
    """Return copies of ``candidates`` with ``lat``/``lon``/``coord_source`` filled.

    Candidates already located by Nominatim/OSM are kept. Others are looked up by
    ``name_local`` then ``name`` (cache first; at most ``max_requests`` network requests, ~1.1 s
    each). On a hit → ``coord_source="nominatim"``; otherwise LLM coordinates (within
    :data:`MAX_DISTANCE_KM` of ``center``) are kept as ``"llm"``; else ``"none"``. New cache rows
    are committed when ``commit`` (so they survive a later failure of the caller).
    """
    budget = _Budget(max_requests)
    out: list[Candidate] = []
    for candidate in candidates:
        if candidate.has_coords and candidate.coord_source in ("nominatim", "osm"):
            out.append(candidate)
            continue
        found: GeoPoint | None = None
        for query in _queries(candidate):
            found, _ = _lookup(db, query, city, country, center, language, budget, commit)
            if found is not None:
                break
        if found is not None:
            out.append(candidate.model_copy(update={
                "lat": found.lat, "lon": found.lon, "coord_source": "nominatim",
            }))
            continue
        point = candidate.point
        if point is not None and (center is None or distance_km(center, point) <= MAX_DISTANCE_KM):
            out.append(candidate.model_copy(update={"coord_source": "llm"}))
        else:
            if point is not None:
                logger.info("Dropping implausible coordinates for %r", candidate.name)
            out.append(candidate.model_copy(update={"lat": None, "lon": None, "coord_source": "none"}))
    return out


CITY_FROM_AIRPORT_MAX_KM = 120.0
"""A geocoded city centre farther than this from the arrival airport is a namesake elsewhere."""


def resolve_plan_locations(db: Session, inputs: PlanInputs, *, commit: bool = True) -> PlanInputs:
    """Return ``inputs`` with better locations (a copy when anything changed; never raises).

    * ``center_source == "airport"`` (a plain airport without a known city centre): the city is
      geocoded (:func:`geocode_city`) and used when it lies within
      :data:`CITY_FROM_AIRPORT_MAX_KM` of the airport (``center_source="geocoded"``).
    * ``hotel_area`` without a hotel point: the area is geocoded around the centre
      (:func:`geocode`), giving ``hotel_lat``/``hotel_lon`` with ``hotel_approximate=True``.

    Both lookups are cached in ``GeocodeCache``; each costs at most one Nominatim request.
    """
    update: dict[str, object] = {}
    center = inputs.center
    if inputs.center_source == "airport":
        city = _safely(lambda: geocode_city(db, inputs.city, inputs.country, commit=commit))
        if city is not None and distance_km(center, (city.lat, city.lon)) <= CITY_FROM_AIRPORT_MAX_KM:
            center = (city.lat, city.lon)
            update.update(center_lat=city.lat, center_lon=city.lon, center_source="geocoded")
        else:
            logger.info("No city centre for %s; the airport location stays the reference", inputs.city)
    if inputs.hotel_area and inputs.hotel_point is None:
        hotel = _safely(lambda: geocode(db, inputs.hotel_area or "", inputs.city, inputs.country, center,
                                        commit=commit))
        if hotel is not None:
            update.update(hotel_lat=hotel.lat, hotel_lon=hotel.lon, hotel_approximate=True)
    return inputs.model_copy(update=update) if update else inputs


def _safely(lookup: Callable[[], GeoPoint | None]) -> GeoPoint | None:
    """Run one lookup; unexpected failures (bad JSON, DB errors) are logged, not raised."""
    try:
        return lookup()
    except Exception as exc:  # noqa: BLE001 - location hints must never break planning
        logger.warning("Location lookup failed: %s", exc)
        return None
