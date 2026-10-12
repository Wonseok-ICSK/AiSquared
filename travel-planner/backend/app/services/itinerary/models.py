"""Itinerary pipeline data objects (DESIGN §10): :class:`Candidate` and :class:`PlanInputs`.

* :class:`Candidate` is one place fact sheet (attraction, museum, restaurant, ...) produced
  by a :class:`~app.services.itinerary.candidates.CandidateSource` (Claude web research,
  OpenStreetMap, ...). Validation is deliberately lenient (LLM output): unknown categories
  fall back to ``attraction``, unknown style keys are dropped, enum casing is ignored and
  sentinel values (``-1`` cost, ``""`` strings) become ``None``.
* :class:`PlanInputs` is the planner's input snapshot (destination, local arrival/departure,
  preferences and budget mirroring ``TravelPreference``). ``model_dump(mode="json")`` is
  suitable for ``Itinerary.request``.
* The plan itself is the API schema: :class:`app.schemas.ItineraryPlan` and its nested
  models are re-exported here and never redefined.

Conventions: weekdays are Python ``date.weekday()`` numbers (0 = Monday); times are local
``"HH:MM"``; money is per person in major units of ``cost_currency`` (local currency when
``None``).
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.airports import METRO_CODES, city_info, metro_for_airport
from app.schemas import (
    DEFAULT_STYLE_SCORES,
    DEFAULT_STYLE_WEIGHTS,
    HHMM,
    STYLE_KEYS,
    BudgetLine,
    BudgetSummary,
    Companion,
    ItineraryPlan,
    Language,
    Pace,
    PlanDay,
    PlanItem,
    PriceBasis,
    RouteMetrics,
    SourceRef,
    StyleMode,
    TravelHop,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from app.models import TravelPreference, Trip

__all__ = [
    "BEST_TIMES",
    "CATEGORIES",
    "CATEGORY_STYLES",
    "CITY_CENTER_OVERRIDES",
    "CITY_NAME_OVERRIDES",
    "COORD_SOURCES",
    "CUISINE_SYNONYMS",
    "DEFAULT_VISIT_MINUTES",
    "FOOD_CATEGORIES",
    "BestTime",
    "BudgetLine",
    "BudgetSummary",
    "Candidate",
    "CandidateCategory",
    "CenterSource",
    "CoordSource",
    "ItineraryPlan",
    "OpeningPeriod",
    "PlanDay",
    "PlanInputs",
    "PlanItem",
    "RouteMetrics",
    "SourceRef",
    "TravelHop",
    "build_plan_inputs",
    "canonical_cuisine",
    "city_center_for",
    "hhmm_to_minutes",
    "make_candidate_id",
    "minutes_to_hhmm",
    "normalize_key",
    "normalize_text",
]

# --------------------------------------------------------------------------- vocabularies

CandidateCategory = Literal[
    "attraction",
    "museum",
    "historic",
    "viewpoint",
    "shopping",
    "market",
    "nature",
    "park",
    "theme_park",
    "nightlife",
    "restaurant",
    "cafe",
    "experience",
    "neighborhood",
]
CoordSource = Literal["nominatim", "llm", "osm", "none"]
BestTime = Literal["morning", "afternoon", "evening", "night", "any"]

CATEGORIES: list[str] = list(get_args(CandidateCategory))
COORD_SOURCES: list[str] = list(get_args(CoordSource))
BEST_TIMES: list[str] = list(get_args(BestTime))
FOOD_CATEGORIES: frozenset[str] = frozenset({"restaurant", "cafe"})

CATEGORY_STYLES: dict[str, tuple[str, ...]] = {
    "attraction": ("sightseeing", "photography"),
    "museum": ("culture", "sightseeing"),
    "historic": ("culture", "sightseeing", "photography"),
    "viewpoint": ("sightseeing", "photography"),
    "shopping": ("shopping",),
    "market": ("food", "local_experience", "shopping"),
    "nature": ("nature", "photography", "relaxation"),
    "park": ("nature", "relaxation"),
    "theme_park": ("sightseeing",),
    "nightlife": ("nightlife",),
    "restaurant": ("food",),
    "cafe": ("food", "relaxation"),
    "experience": ("local_experience", "culture"),
    "neighborhood": ("local_experience", "sightseeing", "shopping"),
}
"""Styles a category serves when a candidate lists none."""

DEFAULT_VISIT_MINUTES: dict[str, int] = {
    "attraction": 60,
    "museum": 90,
    "historic": 50,
    "viewpoint": 50,
    "shopping": 75,
    "market": 60,
    "nature": 90,
    "park": 60,
    "theme_park": 300,
    "nightlife": 90,
    "restaurant": 60,
    "cafe": 40,
    "experience": 120,
    "neighborhood": 75,
}
MIN_VISIT_MINUTES = 10
MAX_VISIT_MINUTES = 600

_CATEGORY_ALIASES: dict[str, str] = {
    "sight": "attraction",
    "sightseeing": "attraction",
    "landmark": "attraction",
    "temple": "historic",
    "shrine": "historic",
    "castle": "historic",
    "palace": "historic",
    "monument": "historic",
    "heritage": "historic",
    "gallery": "museum",
    "art": "museum",
    "observation_deck": "viewpoint",
    "observatory": "viewpoint",
    "view": "viewpoint",
    "tower": "viewpoint",
    "garden": "park",
    "beach": "nature",
    "mountain": "nature",
    "hike": "nature",
    "zoo": "theme_park",
    "aquarium": "theme_park",
    "amusement_park": "theme_park",
    "mall": "shopping",
    "store": "shopping",
    "shop": "shopping",
    "department_store": "shopping",
    "street": "neighborhood",
    "district": "neighborhood",
    "area": "neighborhood",
    "bar": "nightlife",
    "pub": "nightlife",
    "club": "nightlife",
    "izakaya": "restaurant",
    "food": "restaurant",
    "meal": "restaurant",
    "dining": "restaurant",
    "coffee": "cafe",
    "bakery": "cafe",
    "dessert": "cafe",
    "activity": "experience",
    "tour": "experience",
    "workshop": "experience",
    "onsen": "experience",
    "spa": "experience",
}

_STYLE_ALIASES: dict[str, str] = {
    "local": "local_experience",
    "photo": "photography",
    "hotel": "hotel_resort",
    "resort": "hotel_resort",
    "relax": "relaxation",
    "cultural": "culture",
    "gourmet": "food",
}

CUISINE_SYNONYMS: dict[str, tuple[str, ...]] = {
    "sushi": ("sushi", "스시", "초밥", "寿司", "鮨", "すし", "omakase", "오마카세"),
    "ramen": ("ramen", "라멘", "라면", "ラーメン", "らーめん", "tsukemen", "츠케멘", "つけ麺"),
    "udon": ("udon", "우동", "うどん"),
    "soba": ("soba", "소바", "そば", "蕎麦"),
    "tempura": ("tempura", "덴뿌라", "텐동", "튀김", "天ぷら", "天丼", "tendon"),
    "tonkatsu": ("tonkatsu", "돈카츠", "돈가스", "とんかつ", "katsu"),
    "yakiniku": ("yakiniku", "야키니쿠", "焼肉", "wagyu", "와규"),
    "yakitori": ("yakitori", "야키토리", "꼬치", "焼き鳥", "焼鳥"),
    "izakaya": ("izakaya", "이자카야", "居酒屋"),
    "okonomiyaki": ("okonomiyaki", "오코노미야키", "お好み焼き", "monjayaki", "몬자야키", "もんじゃ"),
    "takoyaki": ("takoyaki", "타코야키", "たこ焼き"),
    "unagi": ("unagi", "장어", "우나기", "うなぎ", "鰻"),
    "kaiseki": ("kaiseki", "가이세키", "懐石", "会席"),
    "sukiyaki": ("sukiyaki", "스키야키", "すき焼き", "shabu", "샤브샤브", "しゃぶしゃぶ"),
    "gyudon": ("gyudon", "규동", "牛丼", "beef_bowl"),
    "curry": ("curry", "카레", "カレー"),
    "japanese": ("japanese", "일식", "和食", "washoku"),
    "korean": ("korean", "한식", "한국", "韓国料理", "bibimbap", "비빔밥"),
    "korean_bbq": ("korean_bbq", "삼겹살", "갈비", "고기", "bbq", "barbecue", "바베큐"),
    "chinese": ("chinese", "중식", "중국", "中華", "dim_sum", "딤섬", "dimsum"),
    "thai": ("thai", "태국", "타이"),
    "vietnamese": ("vietnamese", "베트남", "pho", "쌀국수"),
    "indian": ("indian", "인도"),
    "italian": ("italian", "이탈리안", "파스타", "pasta", "pizza", "피자"),
    "french": ("french", "프렌치", "프랑스"),
    "seafood": ("seafood", "해산물", "海鮮", "kaisen", "회", "sashimi", "사시미"),
    "steak": ("steak", "steak_house", "스테이크"),
    "burger": ("burger", "버거", "햄버거"),
    "street_food": ("street_food", "길거리", "포장마차", "yatai", "屋台", "hawker", "night_market"),
    "dessert": ("dessert", "디저트", "sweets", "스위츠", "cake", "케이크", "パフェ", "wagashi"),
    "cafe": ("cafe", "카페", "coffee", "coffee_shop", "커피", "喫茶"),
    "bakery": ("bakery", "베이커리", "빵", "パン"),
    "noodle": ("noodle", "noodles", "국수", "면"),
    "vegetarian": ("vegetarian", "vegan", "채식", "비건"),
    "local": ("local", "regional", "향토", "현지", "郷土料理"),
}
"""Canonical cuisine key -> lower-case synonyms (English / Korean / Japanese / OSM values)."""


# --------------------------------------------------------------------------- text helpers

_SPACE_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[\s\-_'’`\".,·・()\[\]{}/&!?:;~|+]+")
_SLUG_RE = re.compile(r"[^a-z0-9]+")
_HHMM_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


def _fold_latin(text: str) -> str:
    """Strip accents from Latin letters only (``Sensō-ji`` -> ``Senso-ji``); CJK untouched."""
    out: list[str] = []
    for ch in text:
        if ch.isascii() or "LATIN" not in unicodedata.name(ch, ""):
            out.append(ch)
        else:
            out.extend(c for c in unicodedata.normalize("NFKD", ch) if not unicodedata.combining(c))
    return "".join(out)


def normalize_key(text: str | None) -> str:
    """Readable normalized form: NFKC, case-folded, accents stripped, whitespace collapsed."""
    if not text:
        return ""
    folded = _fold_latin(unicodedata.normalize("NFKC", text).casefold())
    return _SPACE_RE.sub(" ", folded).strip()


def normalize_text(text: str | None) -> str:
    """Matching form: :func:`normalize_key` without spaces and punctuation (``"senso ji"`` ->
    ``"sensoji"``). Used for de-duplication and must-visit / avoid matching."""
    return _PUNCT_RE.sub("", normalize_key(text))


def make_candidate_id(
    name: str, category: str = "", source: str = "", point: tuple[float, float] | None = None
) -> str:
    """Deterministic candidate id: ASCII slug of ``name`` + 8 hex chars of a hash over name,
    category and (when known) the location rounded to ~100 m, so chain branches differ."""
    where = f"{point[0]:.3f},{point[1]:.3f}" if point is not None else ""
    digest = hashlib.sha1(
        f"{source}|{normalize_text(name)}|{category}|{where}".encode()
    ).hexdigest()[:8]
    slug = _SLUG_RE.sub("-", normalize_key(name)).strip("-")[:32].strip("-")
    return f"{slug}-{digest}" if slug else f"c-{digest}"


_TOKEN_SPLIT_RE = re.compile(r"[^0-9a-z]+")


def _synonym_in(synonym: str, key: str, tokens: set[str]) -> bool:
    """Whether ``synonym`` occurs in the normalized term ``key`` (tokens = its Latin words).

    Latin synonyms must match whole words (``"tonkotsu ramen"`` contains ``ramen``, but
    ``"tea"`` does not contain ``steak``); CJK synonyms of 2+ characters match as substrings
    because CJK compounds have no spaces (``"초밥집"`` contains ``초밥``)."""
    if synonym.isascii():
        parts = [p for p in _TOKEN_SPLIT_RE.split(synonym) if p]
        return bool(parts) and all(p in tokens for p in parts)
    return len(synonym) >= 2 and synonym in key


def canonical_cuisine(term: str | None) -> str | None:
    """Canonical cuisine key for a free-text food term (``"초밥"`` -> ``"sushi"``), else None.

    Exact synonym first, then a synonym contained in the term as a whole word (Latin) or
    substring (CJK). A term is never matched by being part of a longer synonym, so ordinary
    words do not hit unrelated cuisines (``"bar"`` is not ``barbecue``, ``"beef"`` is not
    ``beef_bowl``)."""
    key = normalize_key(term).replace(" ", "_")
    if not key:
        return None
    for canonical, synonyms in CUISINE_SYNONYMS.items():
        if key == canonical or key in synonyms:
            return canonical
    tokens = {t for t in _TOKEN_SPLIT_RE.split(key) if t}
    for canonical, synonyms in CUISINE_SYNONYMS.items():
        if any(_synonym_in(s, key, tokens) for s in (canonical, *synonyms)):
            return canonical
    return None


def hhmm_to_minutes(value: str) -> int:
    """``"09:30"`` -> ``570`` (minutes after midnight). Raises ``ValueError``."""
    match = _HHMM_RE.match(value.strip())
    if not match:
        raise ValueError(f"time must be HH:MM, got {value!r}")
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 24 or minute > 59 or (hour == 24 and minute):
        raise ValueError(f"time out of range: {value!r}")
    return hour * 60 + minute


def minutes_to_hhmm(minutes: float) -> str:
    """``570`` -> ``"09:30"``; values past midnight wrap (``1470`` -> ``"00:30"``)."""
    total = int(round(minutes)) % (24 * 60)
    return f"{total // 60:02d}:{total % 60:02d}"


def _blank_to_none(value: Any) -> Any:
    if isinstance(value, str) and not value.strip():
        return None
    return value


# --------------------------------------------------------------------------- candidate


class OpeningPeriod(BaseModel):
    """One opening period on a weekday. ``close <= open`` means the period ends after midnight
    (e.g. a bar open ``18:00``-``02:00``); ``00:00``-``00:00`` is open all day."""

    model_config = ConfigDict(frozen=True)

    weekday: int = Field(ge=0, le=6)
    """0 = Monday ... 6 = Sunday (Python ``date.weekday()``)."""
    open: HHMM
    close: HHMM

    @field_validator("open", "close", mode="before")
    @classmethod
    def _midnight(cls, value: Any) -> Any:
        return "00:00" if isinstance(value, str) and value.strip() == "24:00" else value

    def minutes(self) -> tuple[int, int]:
        """``(open, close)`` in minutes after midnight; ``close > open`` (may exceed 1440)."""
        start, end = hhmm_to_minutes(self.open), hhmm_to_minutes(self.close)
        return start, end + 24 * 60 if end <= start else end


class Candidate(BaseModel):
    """A place that may appear in a plan (sight, activity, restaurant, cafe, ...).

    ``est_cost`` is per person in ``cost_currency`` (the destination currency when ``None``);
    ``rating`` keeps the scale of ``rating_source`` (e.g. Google 1-5, Tabelog 1-5 where 3.5 is
    good). ``closed_on_holidays``: closed on every public holiday. ``holiday_shift``: when a
    regular closing day falls on a public holiday the place opens and closes on the next
    non-holiday day instead (typical for Japanese museums). Coordinates given without a
    ``coord_source`` are treated as approximate LLM coordinates (``"llm"``).
    """

    model_config = ConfigDict(extra="ignore")

    id: str = ""
    name: str = Field(min_length=1, max_length=300)
    name_local: str | None = None
    category: CandidateCategory = "attraction"
    styles: list[str] = Field(default_factory=list)
    area: str | None = None
    lat: float | None = Field(None, ge=-90, le=90)
    lon: float | None = Field(None, ge=-180, le=180)
    coord_source: CoordSource = "none"
    visit_minutes: int = 0
    """Typical time on site; ``0`` = category default (:data:`DEFAULT_VISIT_MINUTES`)."""
    opening_hours: list[OpeningPeriod] | None = None
    """Weekly hours; ``None`` = unknown (planner assumes open, warns "unverified")."""
    opening_hours_text: str | None = None
    closed_weekdays: list[int] = Field(default_factory=list)
    closed_on_holidays: bool = False
    holiday_shift: bool = False
    best_time: BestTime = "any"
    is_outdoor: bool = False
    est_cost: float | None = None
    cost_currency: str | None = None
    rating: float | None = None
    rating_source: str | None = None
    cuisine: str | None = None
    """Free text, several values separated by ``;`` or ``,`` (OSM style ``"sushi;japanese"``)."""
    price_level: int | None = Field(None, ge=0, le=4)
    """0 free ... 4 very expensive (Google scale)."""
    description: str | None = None
    booking_required: bool = False
    source_urls: list[str] = Field(default_factory=list)
    sources_verified: bool = False

    # ------------------------------------------------------------------ validators

    @field_validator(
        "name_local", "area", "opening_hours_text", "rating_source", "cuisine", "description",
        "cost_currency", mode="before",
    )
    @classmethod
    def _blank(cls, value: Any) -> Any:
        return _blank_to_none(value)

    @field_validator("name", mode="before")
    @classmethod
    def _strip_name(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value

    @field_validator("category", mode="before")
    @classmethod
    def _category(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        key = normalize_key(value).replace(" ", "_").replace("-", "_")
        if key in CATEGORIES:
            return key
        return _CATEGORY_ALIASES.get(key, "attraction")

    @field_validator("styles", mode="before")
    @classmethod
    def _styles(cls, value: Any) -> Any:
        if value is None:
            return []
        if isinstance(value, str):
            value = re.split(r"[,;/]", value)
        out: list[str] = []
        for raw in value:
            key = normalize_key(str(raw)).replace(" ", "_").replace("-", "_")
            key = _STYLE_ALIASES.get(key, key)
            if key in STYLE_KEYS and key not in out:
                out.append(key)
        return out

    @field_validator("coord_source", "best_time", mode="before")
    @classmethod
    def _lower_enum(cls, value: Any, info: Any) -> Any:
        if not isinstance(value, str):
            return value
        key = normalize_key(value)
        allowed = COORD_SOURCES if info.field_name == "coord_source" else BEST_TIMES
        return key if key in allowed else ("none" if info.field_name == "coord_source" else "any")

    @field_validator("closed_weekdays", mode="before")
    @classmethod
    def _weekdays(cls, value: Any) -> Any:
        if value is None:
            return []
        days = {int(v) for v in value if isinstance(v, int | float) and 0 <= int(v) <= 6}
        return sorted(days)

    @field_validator("est_cost", "rating", mode="before")
    @classmethod
    def _sentinel(cls, value: Any) -> Any:
        if isinstance(value, int | float) and value < 0:
            return None
        return _blank_to_none(value)

    @field_validator("cost_currency", mode="after")
    @classmethod
    def _upper(cls, value: str | None) -> str | None:
        return value.strip().upper() if value else value

    @field_validator("opening_hours", mode="before")
    @classmethod
    def _empty_hours(cls, value: Any) -> Any:
        return None if value == [] else value

    @field_validator("source_urls", mode="before")
    @classmethod
    def _urls(cls, value: Any) -> Any:
        if value is None:
            return []
        urls = [u.strip() for u in value if isinstance(u, str) and u.strip().startswith("http")]
        return list(dict.fromkeys(urls))

    @model_validator(mode="after")
    def _defaults(self) -> Candidate:
        if self.lat is None or self.lon is None:
            self.lat = self.lon = None
        if not self.id:
            self.id = make_candidate_id(self.name, self.category, point=self.point)
        if self.visit_minutes <= 0:
            self.visit_minutes = DEFAULT_VISIT_MINUTES[self.category]
        self.visit_minutes = max(MIN_VISIT_MINUTES, min(MAX_VISIT_MINUTES, self.visit_minutes))
        if self.lat is None or self.lon is None:
            self.lat = self.lon = None
            self.coord_source = "none"
        elif self.coord_source == "none":
            self.coord_source = "llm"
        if self.rating is not None and self.rating == 0:
            self.rating = None
        return self

    # ------------------------------------------------------------------ helpers

    @property
    def has_coords(self) -> bool:
        """True when ``lat``/``lon`` are set."""
        return self.lat is not None and self.lon is not None

    @property
    def point(self) -> tuple[float, float] | None:
        """``(lat, lon)`` or ``None``."""
        return (self.lat, self.lon) if self.lat is not None and self.lon is not None else None

    @property
    def is_food(self) -> bool:
        """Restaurants and cafes (used for meals, not as sightseeing stops)."""
        return self.category in FOOD_CATEGORIES

    @property
    def effective_styles(self) -> list[str]:
        """``styles`` or, when empty, the category's default styles."""
        return self.styles or list(CATEGORY_STYLES[self.category])

    @property
    def cuisines(self) -> list[str]:
        """Canonical cuisine keys from ``cuisine`` (falls back to ``name``/``description``)."""
        found: list[str] = []
        for part in re.split(r"[;,/]", self.cuisine or ""):
            key = canonical_cuisine(part)
            if key and key not in found:
                found.append(key)
        if not found:
            for text in (self.name, self.name_local, self.description):
                for canonical, synonyms in CUISINE_SYNONYMS.items():
                    folded = normalize_key(text)
                    if folded and any(len(s) >= 2 and s in folded for s in synonyms):
                        if canonical not in found:
                            found.append(canonical)
        return found

    @property
    def hours_known(self) -> bool:
        """True when structured weekly opening hours are available."""
        return bool(self.opening_hours)

    def search_terms(self) -> list[str]:
        """Normalized names for matching (``name`` and ``name_local``)."""
        return [t for t in dict.fromkeys((normalize_text(self.name), normalize_text(self.name_local))) if t]


# --------------------------------------------------------------------------- plan inputs

CITY_CENTER_OVERRIDES: dict[str, tuple[float, float]] = {
    # Keyed by metro code (applies to all of its airports) or single-airport code. Metros whose
    # airports.py centroid is off the tourist core, and popular single-airport destinations
    # whose airport lies far from the centre (an airport is never used as a city centre when
    # avoidable: the planner and OverpassSource work around the centre).
    "TYO": (35.6812, 139.7671),  # Tokyo Station (metro centroid is ~10 km west, in Suginami)
    "SIN": (1.2903, 103.8520),  # Singapore, City Hall (Changi is ~17 km east)
    "HKT": (7.8842, 98.3881),  # Phuket Old Town (airport ~24 km north)
    "CXR": (12.2451, 109.1943),  # Nha Trang (Cam Ranh airport ~27 km south)
    "DAD": (16.0611, 108.2274),  # Da Nang, Dragon Bridge
    "PQC": (10.2167, 103.9600),  # Phu Quoc, Duong Dong
    "BKI": (5.9788, 116.0753),  # Kota Kinabalu city
    "CNX": (18.7877, 98.9933),  # Chiang Mai, Tha Phae Gate
    "MFM": (22.1937, 113.5399),  # Macau, Senado Square
    "HNL": (21.2793, -157.8293),  # Honolulu, Waikiki
    "LAX": (34.0522, -118.2437),  # Los Angeles downtown
    "SFO": (37.7880, -122.4075),  # San Francisco, Union Square
    "SEA": (47.6062, -122.3321),  # Seattle downtown
    "LAS": (36.1147, -115.1728),  # Las Vegas Strip
    "YVR": (49.2827, -123.1207),  # Vancouver downtown
    "SYD": (-33.8688, 151.2093),  # Sydney CBD
    "MEL": (-37.8136, 144.9631),  # Melbourne CBD
    "BNE": (-27.4698, 153.0251),  # Brisbane CBD
    "AKL": (-36.8485, 174.7633),  # Auckland CBD
    "IST": (41.0082, 28.9784),  # Istanbul, Sultanahmet (IST airport ~36 km north-west)
    "SAW": (41.0082, 28.9784),  # Istanbul, Sultanahmet
    "DXB": (25.2048, 55.2708),  # Dubai downtown
    "MUC": (48.1374, 11.5755),  # Munich, Marienplatz (airport ~29 km)
    "FRA": (50.1109, 8.6821),  # Frankfurt Römerberg area
    "AMS": (52.3676, 4.9041),  # Amsterdam centre
    "BCN": (41.3874, 2.1686),  # Barcelona, Plaça de Catalunya
    "MAD": (40.4168, -3.7038),  # Madrid, Puerta del Sol
    "PRG": (50.0875, 14.4213),  # Prague Old Town
    "VIE": (48.2082, 16.3738),  # Vienna, Stephansplatz
    "BUD": (47.4979, 19.0402),  # Budapest centre
    "ATH": (37.9838, 23.7275),  # Athens, Syntagma (airport ~30 km)
    "ZRH": (47.3769, 8.5417),  # Zurich centre
    "OSL": (59.9139, 10.7522),  # Oslo centre (airport ~47 km)
    "ARN": (59.3293, 18.0686),  # Stockholm centre (airport ~40 km)
    "HEL": (60.1699, 24.9384),  # Helsinki centre
    "VVO": (43.1155, 131.8855),  # Vladivostok centre (airport ~44 km)
    "UBN": (47.9189, 106.9176),  # Ulaanbaatar, Sükhbaatar Square (airport ~50 km)
    "KOJ": (31.5966, 130.5571),  # Kagoshima centre (airport ~30 km)
    "CMB": (6.9271, 79.8612),  # Colombo centre (airport ~30 km)
}
"""Known city centres ``(lat, lon)`` by metro or airport code (see :func:`city_center_for`)."""

CITY_NAME_OVERRIDES: dict[str, str] = {
    # airports.json names the airport's municipality, not the destination city
    "CXR": "Nha Trang",
    "MFM": "Macau",
    "ATH": "Athens",
    "VVO": "Vladivostok",
    "SAW": "Istanbul",
    "PQC": "Phu Quoc",
    "HNL": "Honolulu",
}
"""Destination city names for codes whose ``city_info`` city is a suburb or garbled."""

CenterSource = Literal["given", "known", "metro", "geocoded", "airport"]
"""Where :attr:`PlanInputs.center_lat`/``center_lon`` came from: ``given`` by the caller,
``known`` (:data:`CITY_CENTER_OVERRIDES`), ``metro`` (city centre of ``app.core.airports``),
``geocoded`` (Nominatim city search) or ``airport`` (last resort — the arrival airport's
location, which can be tens of km from the centre; :func:`geocode.resolve_plan_locations`
replaces it when it can)."""


def city_center_for(code: str, info: dict[str, Any] | None = None) -> tuple[tuple[float, float], CenterSource] | None:
    """City centre for an airport or metro ``code`` and where it came from.

    The override table is looked up by the code's own metro (``NRT``/``HND`` -> ``TYO``) and
    by the code itself, so an override is never applied to another metro's airport. Codes of
    a metro (or single-airport city entry) use that city's centre; a plain airport only has
    its own location (``"airport"``). ``None`` for an unknown code.
    """
    normalized = code.strip().upper()
    info = info if info is not None else city_info(normalized)
    if info is None:
        return None
    metro = normalized if normalized in METRO_CODES else metro_for_airport(normalized)
    for key in (metro, normalized):
        if key and key in CITY_CENTER_OVERRIDES:
            return CITY_CENTER_OVERRIDES[key], "known"
    return (float(info["lat"]), float(info["lon"])), "metro" if metro else "airport"


class PlanInputs(BaseModel):
    """Everything the planner needs (snapshot stored in ``Itinerary.request``).

    ``arrival_at`` = arrival at the destination airport, ``departure_at`` = departure from the
    destination airport, both destination-local naive datetimes. Budget fields follow the spec
    semantics: ``None`` = not specified, ``0``/``-1`` = unlimited, positive = cap, expressed in
    ``budget_currency`` per ``budget_basis``. ``flight_price_total`` is the flight price for
    all travellers in ``flight_currency`` (default ``budget_currency``).
    """

    destination: str
    """IATA airport or metro code of the destination (``TYO``, ``NRT``)."""
    city: str
    country: str
    """ISO-3166 alpha-2."""
    currency: str
    """Local currency (ISO-4217)."""
    timezone: str | None = None
    """IANA zone; ``None`` lets Open-Meteo use ``auto``."""
    center_lat: float = Field(ge=-90, le=90)
    center_lon: float = Field(ge=-180, le=180)
    center_source: CenterSource = "given"
    """Provenance of the centre (see :data:`CenterSource`); ``"airport"`` = unreliable."""
    arrival_at: datetime
    departure_at: datetime
    arrival_airport: str | None = None
    departure_airport: str | None = None
    adults: int = Field(1, ge=1, le=9)

    style_mode: StyleMode = "percent"
    style_weights: dict[str, float] = Field(default_factory=lambda: dict(DEFAULT_STYLE_WEIGHTS))
    companion: Companion = "solo"
    pace: Pace = "moderate"
    food_wishlist: list[str] = Field(default_factory=list)
    preferred_cuisines: list[str] = Field(default_factory=list)
    avoid_foods: list[str] = Field(default_factory=list)
    restaurant_budget_min: float | None = None
    restaurant_budget_max: float | None = None
    restaurant_budget_currency: str | None = None
    must_visit: list[str] = Field(default_factory=list)
    avoid: list[str] = Field(default_factory=list)
    hotel_area: str | None = None
    hotel_lat: float | None = Field(None, ge=-90, le=90)
    hotel_lon: float | None = Field(None, ge=-180, le=180)
    hotel_approximate: bool = False
    """True when the hotel point is only the geocoded ``hotel_area`` (not the hotel itself)."""
    language: Language = "ko"
    notes: str | None = None

    budget_basis: PriceBasis = "total"
    budget_currency: str = "KRW"
    budget_total: float | None = None
    budget_flight: float | None = None
    budget_hotel: float | None = None
    budget_food: float | None = None
    budget_transport: float | None = None
    budget_activities: float | None = None
    budget_shopping: float | None = None
    budget_other: float | None = None

    flight_price_total: float | None = None
    flight_currency: str | None = None

    @field_validator("arrival_at", "departure_at", mode="after")
    @classmethod
    def _naive_minutes(cls, value: datetime) -> datetime:
        return value.replace(tzinfo=None, second=0, microsecond=0)

    @field_validator(
        "currency", "country", "budget_currency", "restaurant_budget_currency", "flight_currency",
        "destination", "arrival_airport", "departure_airport", mode="before",
    )
    @classmethod
    def _upper(cls, value: Any) -> Any:
        value = _blank_to_none(value)
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("hotel_area", "notes", mode="before")
    @classmethod
    def _blank(cls, value: Any) -> Any:
        return _blank_to_none(value)

    @field_validator("style_weights", mode="before")
    @classmethod
    def _weights(cls, value: Any) -> Any:
        if not value:
            return {}
        return {k: float(v) for k, v in dict(value).items() if k in STYLE_KEYS}

    @model_validator(mode="before")
    @classmethod
    def _default_weights(cls, data: Any) -> Any:
        if isinstance(data, dict) and not data.get("style_weights"):
            data = dict(data)
            mode = data.get("style_mode", "percent")
            data["style_weights"] = dict(
                DEFAULT_STYLE_SCORES if mode == "score" else DEFAULT_STYLE_WEIGHTS
            )
        return data

    @model_validator(mode="after")
    def _check(self) -> PlanInputs:
        if self.departure_at <= self.arrival_at:
            raise ValueError("departure_at must be after arrival_at")
        if (self.hotel_lat is None) != (self.hotel_lon is None):
            self.hotel_lat = self.hotel_lon = None
        return self

    @property
    def dates(self) -> list[date]:
        """Every local calendar date from arrival to departure (inclusive)."""
        first, last = self.arrival_at.date(), self.departure_at.date()
        return [first + timedelta(days=i) for i in range((last - first).days + 1)]

    @property
    def nights(self) -> int:
        """Hotel nights (calendar days between arrival and departure, at least 0)."""
        return max(0, (self.departure_at.date() - self.arrival_at.date()).days)

    @property
    def center(self) -> tuple[float, float]:
        """City centre ``(lat, lon)``."""
        return (self.center_lat, self.center_lon)

    @property
    def hotel_point(self) -> tuple[float, float] | None:
        """Hotel ``(lat, lon)`` when known."""
        if self.hotel_lat is None or self.hotel_lon is None:
            return None
        return (self.hotel_lat, self.hotel_lon)


def _as_local(value: datetime | str) -> datetime:
    return datetime.fromisoformat(value) if isinstance(value, str) else value


def build_plan_inputs(
    trip: Trip,
    preferences: TravelPreference | None,
    *,
    arrival_at: datetime | str,
    departure_at: datetime | str,
    arrival_airport: str | None = None,
    departure_airport: str | None = None,
    hotel_area: str | None = None,
    hotel_point: tuple[float, float] | None = None,
    center: tuple[float, float] | None = None,
    notes: str | None = None,
    language: Language = "ko",
    flight_price_total: float | None = None,
    flight_currency: str | None = None,
    db: Session | None = None,
) -> PlanInputs:
    """Build :class:`PlanInputs` from ORM rows (``preferences`` may be ``None`` → defaults).

    City/country/currency come from ``app.core.airports.city_info`` for the arrival airport
    (else the trip destination); the centre from :func:`city_center_for` for that same code.
    ``center`` overrides the city centre, ``hotel_point`` the hotel location. With ``db``,
    :func:`app.services.itinerary.geocode.resolve_plan_locations` then geocodes (Nominatim,
    cached) the city when the centre would only be the airport, and ``hotel_area`` when no
    ``hotel_point`` is given. Raises ``ValueError`` for an unknown code.
    """
    from app.services.itinerary.calendar_util import guess_timezone  # local: avoid cycle

    code, info = next(
        ((c.upper(), i) for c in (arrival_airport, trip.destination) if c and (i := city_info(c)) is not None),
        (None, None),
    )
    if code is None or info is None:
        raise ValueError(f"unknown destination code {trip.destination!r}")
    if center is not None:
        (lat, lon), source = center, "given"
    else:
        (lat, lon), source = city_center_for(code, info)  # type: ignore[misc]
    data: dict[str, Any] = {
        "destination": trip.destination,
        "city": CITY_NAME_OVERRIDES.get(code, info["city"]),
        "country": info["country"],
        "currency": info["currency"] or "USD",
        "timezone": guess_timezone(info["country"], lon),
        "center_lat": lat,
        "center_lon": lon,
        "center_source": source,
        "arrival_at": _as_local(arrival_at),
        "departure_at": _as_local(departure_at),
        "arrival_airport": arrival_airport,
        "departure_airport": departure_airport,
        "adults": trip.adults or 1,
        "language": language,
        "notes": notes,
        "flight_price_total": flight_price_total,
        "flight_currency": flight_currency,
        "budget_currency": trip.currency or "KRW",
    }
    if preferences is not None:
        for name in (
            "style_mode", "style_weights", "companion", "pace", "food_wishlist",
            "preferred_cuisines", "avoid_foods", "restaurant_budget_min", "restaurant_budget_max",
            "restaurant_budget_currency", "must_visit", "avoid", "budget_basis", "budget_currency",
            "budget_total", "budget_flight", "budget_hotel", "budget_food", "budget_transport",
            "budget_activities", "budget_shopping", "budget_other",
        ):
            data[name] = getattr(preferences, name)
        data["hotel_area"] = preferences.hotel_area
        if preferences.notes and not notes:
            data["notes"] = preferences.notes
    if hotel_area:
        data["hotel_area"] = hotel_area
    if hotel_point:
        data["hotel_lat"], data["hotel_lon"] = hotel_point
    inputs = PlanInputs.model_validate(data)
    if db is not None:
        from app.services.itinerary.geocode import resolve_plan_locations  # local: avoid cycle

        inputs = resolve_plan_locations(db, inputs)
    return inputs
