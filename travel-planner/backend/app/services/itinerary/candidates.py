"""Candidate sources and merging (DESIGN §10 step 2).

* :class:`CandidateSource` — interface every source implements (Claude web research,
  OpenStreetMap, future Google Places): ``fetch(inputs, db) -> list[Candidate]``.
* :class:`OverpassSource` — keyless OpenStreetMap fallback: at most two small bounding-box
  queries (city centre, plus the hotel area when it lies elsewhere) for notable attractions,
  museums, viewpoints, parks, markets and restaurants/cafes filtered by the traveller's
  cuisines. Polite: identifying ``User-Agent``, server-side timeout, no retries beyond the
  request allowance. Data © OpenStreetMap contributors (ODbL) — show :data:`OSM_ATTRIBUTION`.
* :func:`merge_candidates` — de-duplicates across sources by normalized name + 150 m.
"""

from __future__ import annotations

import logging
import math
import re
from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import Any, ClassVar

import httpx
from sqlalchemy.orm import Session

from app.services.itinerary.models import (
    CUISINE_SYNONYMS,
    Candidate,
    OpeningPeriod,
    PlanInputs,
    canonical_cuisine,
    normalize_text,
)
from app.services.itinerary.geocode import resolve_plan_locations
from app.services.itinerary.routing import Coord, distance_km

logger = logging.getLogger(__name__)

OSM_ATTRIBUTION = "Map data © OpenStreetMap contributors (ODbL)"
OVERPASS_URLS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
)
USER_AGENT = "AiSquaredTravel/0.1 (personal use)"
MERGE_DISTANCE_KM = 0.15


class CandidateSource(ABC):
    """A provider of :class:`Candidate` places for a trip.

    Implementations should never raise for ordinary outages: log, set :attr:`last_error` and
    return what they have (possibly ``[]``). ``attribution`` is shown with the plan.
    """

    key: ClassVar[str]
    display_name: ClassVar[str] = ""
    attribution: ClassVar[str | None] = None

    def __init__(self) -> None:
        self.last_error: str | None = None

    @abstractmethod
    def fetch(self, inputs: PlanInputs, db: Session | None = None) -> list[Candidate]:
        """Candidates for ``inputs`` (``db`` for sources that cache, e.g. geocoding)."""


class StaticSource(CandidateSource):
    """Fixed candidate list (tests, user-supplied places)."""

    key = "static"
    display_name = "Static list"

    def __init__(self, candidates: Sequence[Candidate]) -> None:
        super().__init__()
        self._candidates = list(candidates)

    def fetch(self, inputs: PlanInputs, db: Session | None = None) -> list[Candidate]:
        """Return the fixed list."""
        return list(self._candidates)


# --------------------------------------------------------------------------- OSM opening hours

_DAYS = ("mo", "tu", "we", "th", "fr", "sa", "su")
_DAY_RE = re.compile(r"^(mo|tu|we|th|fr|sa|su)(?:-(mo|tu|we|th|fr|sa|su))?$")
_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})$")


def _parse_days(text: str) -> list[int] | None:
    days: list[int] = []
    for part in text.split(","):
        match = _DAY_RE.match(part.strip())
        if not match:
            return None
        first = _DAYS.index(match.group(1))
        last = _DAYS.index(match.group(2)) if match.group(2) else first
        span = (last - first) % 7
        days.extend((first + i) % 7 for i in range(span + 1))
    return days


def _parse_times(text: str) -> list[tuple[str, str]] | None:
    spans: list[tuple[str, str]] = []
    for part in text.split(","):
        match = _TIME_RE.match(part.strip())
        if not match:
            return None
        h1, m1, h2, m2 = (int(g) for g in match.groups())
        if h1 > 24 or h2 > 48 or m1 > 59 or m2 > 59:
            return None
        spans.append((f"{h1 % 24:02d}:{m1:02d}", f"{h2 % 24:02d}:{m2:02d}"))
    return spans


def parse_osm_opening_hours(text: str | None) -> tuple[list[OpeningPeriod], list[int]] | None:
    """Parse the common subset of OSM ``opening_hours``: ``"Mo-Fr 09:00-17:00; Sa,Su
    10:00-18:00"``, ``"Tu-Su 09:30-17:00"``, ``"Mo off"``, ``"10:00-20:00"``, ``"24/7"``.

    Returns ``(periods, closed_weekdays)`` or ``None`` when the text uses unsupported syntax
    (months, week numbers, sunrise, ...). ``PH``/``SH`` rules are ignored. Later rules
    override earlier ones for the same days (OSM semantics).
    """
    if not text or not text.strip():
        return None
    raw = text.strip()
    if raw == "24/7":
        return [OpeningPeriod(weekday=d, open="00:00", close="00:00") for d in range(7)], []
    schedule: dict[int, list[tuple[str, str]]] = {}
    for rule in (r.strip() for r in raw.split(";")):
        if not rule:
            continue
        lowered = rule.lower()
        if lowered.startswith(("ph", "sh")):
            continue
        tokens = lowered.split(None, 1)
        if _TIME_RE.match(tokens[0].split(",")[0]):
            days, spec = list(range(7)), lowered
        else:
            days = _parse_days(tokens[0])  # type: ignore[assignment]
            if days is None or len(tokens) < 2:
                return None
            spec = tokens[1].strip()
        if spec in ("off", "closed"):
            for day in days:
                schedule[day] = []
            continue
        spans = _parse_times(spec.replace(" ", ""))
        if spans is None:
            return None
        for day in days:
            schedule[day] = spans
    if not schedule:
        return None
    periods = [
        OpeningPeriod(weekday=day, open=o, close=c)
        for day in sorted(schedule)
        for o, c in schedule[day]
    ]
    closed = [d for d in range(7) if not schedule.get(d)]
    return periods, closed


# --------------------------------------------------------------------------- Overpass source

_LOCAL_CUISINES: dict[str, tuple[str, ...]] = {
    "JP": ("japanese", "sushi", "ramen", "soba", "udon", "tempura", "yakitori", "tonkatsu"),
    "KR": ("korean", "korean_bbq"),
    "CN": ("chinese",),
    "TW": ("chinese", "taiwanese"),
    "HK": ("chinese", "cantonese"),
    "TH": ("thai",),
    "VN": ("vietnamese",),
    "IT": ("italian", "pizza"),
    "FR": ("french",),
    "ES": ("spanish", "tapas"),
}
_OSM_CUISINE_VALUES: dict[str, tuple[str, ...]] = {
    "korean_bbq": ("korean", "barbecue"),
    "street_food": ("street_food",),
    "dessert": ("dessert", "cake", "ice_cream"),
    "cafe": ("coffee_shop",),
    "steak": ("steak_house",),
    "sukiyaki": ("sukiyaki", "shabu-shabu"),
    "gyudon": ("beef_bowl",),
    "unagi": ("unagi", "eel"),
    "local": ("regional",),
    "noodle": ("noodle",),
}


def cuisine_regex(inputs: PlanInputs) -> str:
    """OSM ``cuisine`` regex for the traveller's wishes, else the country's local cuisines."""
    keys: list[str] = []
    for term in [*inputs.food_wishlist, *inputs.preferred_cuisines]:
        key = canonical_cuisine(term)
        if key and key not in keys:
            keys.append(key)
    values: list[str] = []
    for key in keys:
        values.extend(_OSM_CUISINE_VALUES.get(key, (key,)))
    if not values:
        values = list(_LOCAL_CUISINES.get(inputs.country, ("regional", "local")))
    safe = [re.sub(r"[^a-z0-9_\-]", "", v) for v in dict.fromkeys(values)]
    return "|".join(v for v in safe if v)


def _bbox(center: Coord, radius_km: float) -> tuple[float, float, float, float]:
    lat, lon = center
    dlat = radius_km / 110.57
    dlon = radius_km / max(1e-6, 111.32 * math.cos(math.radians(lat)))
    return (lat - dlat, lon - dlon, lat + dlat, lon + dlon)


def build_overpass_query(
    bbox: tuple[float, float, float, float], cuisines: str, *, timeout_s: int = 45,
    poi_limit: int = 400, food_limit: int = 250,
) -> str:
    """Overpass QL for notable POIs and cuisine-filtered restaurants/cafes in ``bbox``."""
    s, w, n, e = bbox
    food_filter = f'["cuisine"~"{cuisines}",i]' if cuisines else '["cuisine"]'
    return (
        f"[out:json][timeout:{timeout_s}][bbox:{s:.5f},{w:.5f},{n:.5f},{e:.5f}];\n"
        "(\n"
        '  nwr["tourism"~"^(attraction|museum|gallery|viewpoint|theme_park|zoo|aquarium)$"]["name"];\n'
        '  nwr["historic"~"^(castle|palace|monument|ruins|archaeological_site|city_gate|fort|temple|shrine)$"]["name"]["wikidata"];\n'
        '  nwr["amenity"="place_of_worship"]["name"]["wikidata"];\n'
        '  nwr["leisure"~"^(park|garden)$"]["name"]["wikidata"];\n'
        '  nwr["amenity"="marketplace"]["name"];\n'
        '  nwr["shop"~"^(mall|department_store)$"]["name"]["wikidata"];\n'
        ")->.poi;\n"
        f'nwr["amenity"~"^(restaurant|cafe)$"]["name"]{food_filter}->.food;\n'
        f".poi out center tags {poi_limit};\n"
        f".food out center tags {food_limit};\n"
    )


def _osm_category(tags: dict[str, str]) -> str | None:
    tourism, amenity = tags.get("tourism", ""), tags.get("amenity", "")
    if amenity == "restaurant":
        return "restaurant"
    if amenity == "cafe":
        return "cafe"
    if tourism in ("museum", "gallery"):
        return "museum"
    if tourism == "viewpoint":
        return "viewpoint"
    if tourism in ("theme_park", "zoo", "aquarium"):
        return "theme_park"
    if amenity == "marketplace":
        return "market"
    if tags.get("shop") in ("mall", "department_store"):
        return "shopping"
    if tags.get("leisure") in ("park", "garden"):
        return "park"
    if tags.get("historic") or amenity == "place_of_worship":
        return "historic"
    if tourism == "attraction":
        return "attraction"
    return None


def _notability(tags: dict[str, str]) -> float:
    score = 0.0
    score += 2.0 if "wikidata" in tags else 0.0
    score += 1.0 if "wikipedia" in tags else 0.0
    score += 1.0 if tags.get("tourism") in ("attraction", "museum", "viewpoint", "theme_park") else 0.0
    score += 0.5 if any(k.startswith("name:") for k in tags) else 0.0
    score += 0.3 if "opening_hours" in tags else 0.0
    score += 0.2 if "website" in tags else 0.0
    return score


def _wikipedia_url(value: str | None) -> str | None:
    if not value or ":" not in value:
        return None
    lang, title = value.split(":", 1)
    return f"https://{lang}.wikipedia.org/wiki/{title.replace(' ', '_')}"


def _osm_candidate(element: dict[str, Any], language: str) -> Candidate | None:
    tags: dict[str, str] = element.get("tags") or {}
    category = _osm_category(tags)
    lat = element.get("lat", (element.get("center") or {}).get("lat"))
    lon = element.get("lon", (element.get("center") or {}).get("lon"))
    local_name = tags.get("name")
    if category is None or lat is None or lon is None or not local_name:
        return None
    display = tags.get(f"name:{language}") or tags.get("name:en") or local_name
    hours = parse_osm_opening_hours(tags.get("opening_hours"))
    osm_id = f"{element.get('type', 'node')}/{element.get('id')}"
    urls = [f"https://www.openstreetmap.org/{osm_id}"]
    for url in (tags.get("website"), tags.get("contact:website"), _wikipedia_url(tags.get("wikipedia"))):
        if url and url.startswith("http"):
            urls.append(url)
    area = (tags.get("addr:suburb") or tags.get("addr:quarter") or tags.get("addr:neighbourhood")
            or tags.get("addr:district"))
    return Candidate(
        id=f"osm:{osm_id}",
        name=display,
        name_local=local_name if local_name != display else None,
        category=category,
        area=area,
        lat=float(lat),
        lon=float(lon),
        coord_source="osm",
        opening_hours=hours[0] if hours else None,
        closed_weekdays=hours[1] if hours else [],
        opening_hours_text=tags.get("opening_hours"),
        is_outdoor=category in ("park", "nature", "viewpoint") and tags.get("indoor") != "yes",
        est_cost=0.0 if tags.get("fee") == "no" else None,
        cuisine=tags.get("cuisine"),
        source_urls=urls,
        sources_verified=False,
        description=tags.get(f"description:{language}") or tags.get("description"),
    )


class OverpassSource(CandidateSource):
    """OpenStreetMap candidates via the Overpass API (keyless fallback, ≤ 2 requests)."""

    key = "overpass"
    display_name = "OpenStreetMap (Overpass)"
    attribution = OSM_ATTRIBUTION

    def __init__(
        self,
        *,
        radius_km: float = 3.0,
        hotel_radius_km: float = 1.5,
        max_requests: int = 2,
        timeout_s: int = 45,
        max_pois: int = 40,
        max_food: int = 30,
        urls: Sequence[str] = OVERPASS_URLS,
    ) -> None:
        super().__init__()
        self.radius_km = radius_km
        self.hotel_radius_km = hotel_radius_km
        self.max_requests = max(1, min(2, max_requests))
        self.timeout_s = timeout_s
        self.max_pois = max_pois
        self.max_food = max_food
        self.urls = tuple(urls)
        self.requests_made = 0

    def _areas(self, inputs: PlanInputs) -> list[tuple[Coord, float]]:
        areas: list[tuple[Coord, float]] = [(inputs.center, self.radius_km)]
        hotel = inputs.hotel_point
        if hotel is not None and distance_km(hotel, inputs.center) > self.radius_km * 0.8:
            areas.append((hotel, self.hotel_radius_km))
        return areas

    def _post(self, client: httpx.Client, url: str, query: str) -> list[dict[str, Any]]:
        self.requests_made += 1
        response = client.post(url, data={"data": query})
        if response.status_code != 200:
            raise httpx.HTTPStatusError(
                f"Overpass HTTP {response.status_code}", request=response.request, response=response
            )
        payload = response.json()
        if payload.get("remark") and not payload.get("elements"):
            raise ValueError(f"Overpass remark: {payload['remark'][:200]}")
        return list(payload.get("elements") or [])

    def _query_area(self, client: httpx.Client, query: str) -> list[dict[str, Any]] | None:
        """Run one query, falling back to the mirror while the request allowance lasts."""
        for url in self.urls:
            if self.requests_made >= self.max_requests:
                return None
            try:
                return self._post(client, url, query)
            except (httpx.HTTPError, ValueError) as exc:
                self.last_error = str(exc)
                logger.warning("Overpass query failed at %s: %s", url, exc)
        return None

    def fetch(self, inputs: PlanInputs, db: Session | None = None) -> list[Candidate]:
        """Notable POIs + cuisine-matched restaurants around the centre (and hotel area).

        With ``db``, an airport-only centre is first replaced by the geocoded city and the
        hotel area is located (:func:`geocode.resolve_plan_locations`, cached), so the boxes
        cover the city rather than the airport."""
        self.last_error = None
        self.requests_made = 0
        if db is not None:
            inputs = resolve_plan_locations(db, inputs)
        if inputs.center_source == "airport":
            logger.warning("Overpass: city centre of %s unknown, searching around the airport", inputs.city)
        cuisines = cuisine_regex(inputs)
        elements: dict[str, dict[str, Any]] = {}
        headers = {"User-Agent": USER_AGENT}
        with httpx.Client(timeout=self.timeout_s + 15, headers=headers) as client:
            for center, radius in self._areas(inputs):
                query = build_overpass_query(_bbox(center, radius), cuisines, timeout_s=self.timeout_s)
                found = self._query_area(client, query)
                if found is None:
                    break
                for element in found:
                    elements.setdefault(f"{element.get('type')}/{element.get('id')}", element)
        candidates = [
            c for el in elements.values() if (c := _osm_candidate(el, inputs.language)) is not None
        ]
        ranked = sorted(
            candidates,
            key=lambda c: (-_notability(elements[c.id.removeprefix("osm:")].get("tags") or {}), c.id),
        )
        pois = [c for c in ranked if not c.is_food][: self.max_pois]
        food = [c for c in ranked if c.is_food][: self.max_food]
        logger.info("Overpass: %d POIs, %d restaurants/cafes (%d requests)", len(pois), len(food),
                    self.requests_made)
        return merge_candidates(pois + food)


# --------------------------------------------------------------------------- merging

_COORD_RANK = {"nominatim": 3, "osm": 3, "llm": 1, "none": 0}


def _names_match(a: Candidate, b: Candidate, close: bool) -> bool:
    for x in a.search_terms():
        for y in b.search_terms():
            if x == y:
                return True
            if close and min(len(x), len(y)) >= 4 and (x in y or y in x):
                return True
    return False


def _is_duplicate(a: Candidate, b: Candidate) -> bool:
    if a.point is not None and b.point is not None:
        if distance_km(a.point, b.point) > MERGE_DISTANCE_KM:
            return False
        return _names_match(a, b, close=True)
    return _names_match(a, b, close=False)


def _merge_pair(primary: Candidate, other: Candidate) -> Candidate:
    """Keep ``primary``'s values, fill gaps from ``other``; best coordinates win."""
    update: dict[str, Any] = {}
    for name in Candidate.model_fields:
        mine, theirs = getattr(primary, name), getattr(other, name)
        if mine in (None, [], "") and theirs not in (None, [], ""):
            update[name] = theirs
    if _COORD_RANK[other.coord_source] > _COORD_RANK[primary.coord_source] and other.point is not None:
        update.update(lat=other.lat, lon=other.lon, coord_source=other.coord_source)
    update["source_urls"] = list(dict.fromkeys([*primary.source_urls, *other.source_urls]))
    update["styles"] = list(dict.fromkeys([*primary.styles, *other.styles]))
    update["sources_verified"] = primary.sources_verified or other.sources_verified
    update["id"] = primary.id
    return primary.model_copy(update=update)


def merge_candidates(*lists: Sequence[Candidate]) -> list[Candidate]:
    """Merge candidate lists (earlier lists take precedence), de-duplicating places whose
    normalized names match (equal, or containment when within 150 m) and whose coordinates —
    when both have them — are within :data:`MERGE_DISTANCE_KM`. Order of first appearance is
    kept; ids stay unique."""
    merged: list[Candidate] = []
    for candidates in lists:
        for candidate in candidates:
            for index, existing in enumerate(merged):
                if existing.id == candidate.id or _is_duplicate(existing, candidate):
                    merged[index] = _merge_pair(existing, candidate)
                    break
            else:
                merged.append(candidate)
    return merged


def known_cuisine_values() -> list[str]:
    """All canonical cuisine keys (for prompts / UI hints)."""
    return list(CUISINE_SYNONYMS)


def name_matches(term: str, candidate: Candidate) -> bool:
    """True when free text ``term`` names ``candidate`` (normalized containment either way)."""
    needle = normalize_text(term)
    if len(needle) < 2:
        return False
    return any(needle == t or (len(needle) >= 3 and (needle in t or (len(t) >= 4 and t in needle)))
               for t in candidate.search_terms())
