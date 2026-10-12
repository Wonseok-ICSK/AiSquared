"""Deterministic rule-based itinerary planner (DESIGN §10 step 3, §13.4).

:func:`plan_itinerary` turns :class:`PlanInputs` + :class:`Candidate` facts (+ optional weather
and holidays) into a complete :class:`~app.schemas.ItineraryPlan` (``generator="rule_based"``):

1. **Filter & score** candidates: ``avoid`` / ``avoid_foods`` excluded, places without
   coordinates or > 30 km from every reference point (city centre, hotel, medoid of the
   candidates — robust to an airport-only centre) skipped, ``must_visit`` matched and forced;
   score = style affinity (percent or 1-5 score weights, normalised to the strongest style) +
   rating + companion adjustments.
2. **Day windows**: arrival day starts after immigration/baggage (90 min) + transfer + hotel
   check-in (30 min); the departure block (collect bags 20 min, transfer, flight) starts when
   leaving for the airport (in town until at the latest departure − 3 h, earlier when the
   transfer is long: at the airport 2 h before) and sits on the date the bags are collected —
   an overnight flight's block starts the evening before and a date after it is omitted. That
   day's stops and meals must be back at the hotel by the pickup. Other days follow the pace
   (relaxed / moderate / packed). Pace and free minutes give each day an activity count. A
   stay too short to reach the hotel and come back becomes a single airport (layover) day.
3. **Select** a pool of about twice as many sights as there are slots (category diversity,
   activity budget), **cluster** the pool into day areas (balanced k-means; short arrival /
   departure days anchored at the hotel; penalties for closures, opening hours, best time of
   day and outdoor sights on wet days), **pick** each day's stops from its area (score minus
   penalties minus spread), **order** each day (branch-and-bound / 2-opt with time windows and
   meal breaks), drop the weakest stop while a day overruns, retry dropped must-visits on any
   day, and use idle time for the area's reserve or nearby unused sights (never outdoor ones on
   a wet day). Stops dropped while building the final timeline (restaurant detours, cut-off,
   closing times) are reported like any other drop.
4. **Meals**: lunch (~11:30-13:30) and dinner (~18:00-20:00) at restaurants near the route
   that are open for the whole meal at the real arrival time (within the restaurant budget,
   preferring food wishes / preferred cuisines, no repeats); generic "sushi near Asakusa"
   suggestions with map/Tabelog links when no data is nearby; breakfast only when a café is
   within walking distance of the hotel (a must-visit café counts as visited there).
5. :func:`finalize_plan` (run it on Claude-composed plans too) rebuilds everything factual in
   code — coordinates, source URLs and links from the candidates, travel hops, per-leg
   direction links, day costs, sources, route metrics and the budget summary;
   :func:`check_plan` lists timing/route problems and, given the candidates, closures,
   opening-hours violations and outdoor stops on wet days.

Plan item costs are per person (``ItineraryPlan.cost_basis == "per_person"``).
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

from app.core.airports import get_airport
from app.schemas import STYLE_KEYS, ItineraryPlan, PlanDay, PlanItem, SourceRef, TravelHop
from app.services.itinerary import budget as budget_mod
from app.services.itinerary import calendar_util, links, routing
from app.services.itinerary import weather as weather_mod
from app.services.itinerary.candidates import OSM_ATTRIBUTION, name_matches
from app.services.itinerary.geocode import ATTRIBUTION as GEOCODE_ATTRIBUTION
from app.services.itinerary.geocode import MAX_DISTANCE_KM as GEOCODE_MAX_KM
from app.services.itinerary.models import (
    CATEGORY_STYLES,
    Candidate,
    PlanInputs,
    canonical_cuisine,
    hhmm_to_minutes,
    minutes_to_hhmm,
    normalize_key,
    normalize_text,
)
from app.services.itinerary.routing import Break, Coord, DaySchedule, Stop

logger = logging.getLogger(__name__)

GENERATOR = "rule_based"
IMMIGRATION_MIN = 90
CHECKIN_MIN = 30
BAG_PICKUP_MIN = 20
AIRPORT_ARRIVE_BEFORE_MIN = 120
AIRPORT_CUTOFF_MIN = 180
"""Nothing in town later than departure − 3 h."""
RETURN_ALLOWANCE_MIN = 20
SLOT_TRAVEL_MIN = 20
DAY_MIN_WINDOW = 45
MAX_CITY_RADIUS_KM = 30.0
LONG_HOP_MIN = 45
MEAL_SEARCH_KM = 2.0
BREAKFAST_RADIUS_KM = 0.8
BREAKFAST_MIN = 40
BREAKFAST_WALK_MIN = 15
"""Time allowed to walk to a breakfast café (≥ the walk over :data:`BREAKFAST_RADIUS_KM`)."""
MEAL_MAX_WAIT_MIN = 30
"""Longest wait for a restaurant to open before a meal."""
MIN_MEAL_MIN = 30
"""A meal squeezed by the airport cut-off is dropped when shorter than this."""
REVISIT_PENALTY = 0.5
"""Score removed from a breakfast café already visited (prefers variety, allows repeats)."""
FREE_GAP_MIN = 30
MEAL_ALLOWANCE_MIN = 15
LATE_TOLERANCE_MIN = 10
OVERRUN_TOLERANCE_MIN = 10
INFEASIBLE = 50.0
FILL_RADIUS_KM = 2.0
FILL_MAX_PER_DAY = 3
FILL_MIN_IDLE = 60
POOL_FACTOR = 2
"""The candidate pool clustered into day areas holds about this many sights per slot."""
DAY_PENALTY_WEIGHT = 0.1
"""Score points per km-equivalent day penalty when picking a day's stops."""
DIVERSITY_STEP = 0.2
"""Score points removed per already chosen sight of the same category."""
CATEGORY_STYLE_FACTOR = 0.8
GENERIC_MEAL_FLOOR = 0.5
"""A generic meal is never estimated below this share of the mid-range price."""
PICK_DISTANCE_WEIGHT = 0.08
"""Score points per km from the day's already picked stops (keeps each day compact)."""
BEST_TIME_MISS_KM = 1.0
BEST_TIME_PARTIAL_KM = 0.5
NIGHTLIFE_MISS_KM = 4.0
RAIN_OUTDOOR_KM = 4.0
WET_DAY_RAIN = 0.5
"""Rain factor from which a day counts as wet (outdoor sights avoided)."""
WET_OUTDOOR_PICK_PENALTY = 1.0
CLUSTER_SLACK = 1.25
"""Cluster capacities exceed the pool by this factor so outliers need not join a far day."""
RESERVE_RADIUS_KM = 3.0
DEFAULT_TRANSFER = TravelHop(mode="transit", minutes=60.0, distance_km=30.0)
LUNCH = (690, 810)
DINNER = (1080, 1200)
BEST_TIME_WINDOWS: dict[str, tuple[int, int] | None] = {
    "morning": (480, 720),
    "afternoon": (720, 1020),
    "evening": (1020, 1260),
    "night": (1140, 1440),
    "any": None,
}
COMPANION_BONUS: dict[str, dict[str, float]] = {
    "family": {"theme_park": 0.3, "park": 0.15, "nature": 0.1, "museum": 0.05, "nightlife": -0.8},
    "couple": {"viewpoint": 0.1, "nightlife": 0.05, "cafe": 0.05},
    "solo": {},
}


@dataclass(frozen=True)
class PaceProfile:
    """Day shape for a pace (minutes after midnight)."""

    start: int
    end: int
    slot_minutes: int
    max_per_day: int
    lunch: int
    dinner: int
    fill_extra: int
    """Short stops that may be added beyond ``max_per_day`` when the day has idle time."""


PACES: dict[str, PaceProfile] = {
    "relaxed": PaceProfile(start=600, end=1230, slot_minutes=130, max_per_day=3, lunch=75, dinner=90,
                           fill_extra=0),
    "moderate": PaceProfile(start=540, end=1290, slot_minutes=105, max_per_day=4, lunch=60, dinner=75,
                            fill_extra=1),
    "packed": PaceProfile(start=480, end=1350, slot_minutes=90, max_per_day=6, lunch=50, dinner=70,
                          fill_extra=2),
}

TEXTS: dict[str, dict[str, str]] = {
    "en": {
        "title": "{city} · {n} days",
        "summary": "{n}-day {city} plan ({first} – {last}): {acts} sights and {meals} meals, "
                   "grouped by area to keep travel short.",
        "arrival": "Arrive at {airport} (immigration & baggage)",
        "to_hotel": "Transfer to the hotel ({area})",
        "checkin": "Hotel check-in",
        "dropbags": "Drop bags at the hotel",
        "back_hotel": "Back to the hotel",
        "pickup_bags": "Collect bags / check out",
        "to_airport": "Transfer to {airport}",
        "departure": "Flight departs {airport} at {time} (check-in & security)",
        "departure_when": "{time} on {date}",
        "layover_wait": "Wait at {airport} (lounge / rest)",
        "breakfast": "Breakfast",
        "lunch": "Lunch",
        "dinner": "Dinner",
        "meal_near": "{meal}: {food} near {area}",
        "food_any": "local food",
        "free": "Free time around {area}",
        "theme_arrival": "Arrival",
        "theme_departure": "Departure",
        "theme_layover": "Airport layover",
        "w_hours": "Opening hours not verified for: {names} — check before visiting.",
        "w_long": "Long transfer on {date}: {a} → {b} (~{min} min).",
        "w_overlap": "Overlapping items on {date}: {a} / {b}.",
        "w_bad_time": "Invalid time for {a} on {date} (expected HH:MM).",
        "w_tight": "Not enough time to travel {a} → {b} on {date} (~{min} min needed).",
        "w_before_arrival": "{a} is scheduled before arrival on {date}.",
        "w_after_cutoff": "{a} ends after the airport cut-off ({time}) on {date}.",
        "w_backtrack": "The route returns to an earlier area {n} time(s).",
        "w_over_budget": "Estimated costs exceed the budget ({lines}).",
        "w_sparse": "Only {n} sights fit the data and budget for {slots} slots — some time is left free.",
        "w_no_coords": "{n} place(s) without coordinates were skipped: {names}.",
        "w_too_far": "Skipped (more than {km} km from the centre): {names}.",
        "w_must_missing": "Must-visit not found in the place data: {names}.",
        "w_must_dropped": "Could not fit must-visit: {names} (closed or no time).",
        "w_closed": "Skipped (closed or no time on the available days): {names}.",
        "w_hotel": "Hotel location unknown — assumed near {area}.",
        "w_hotel_area": "Could not locate the hotel area “{wanted}” — assumed near {area}.",
        "w_layover": "Only {hours} h between arrival and departure — not enough to reach the hotel "
                     "and come back, so the plan stays at the airport.",
        "w_item_closed": "{a} is closed on {date}.",
        "w_item_hours": "{a} on {date} is outside its opening hours ({hours}).",
        "w_holiday": "{date} is a public holiday ({name}): expect crowds and changed hours.",
        "w_generic_meals": "No restaurant data near some meals — map/guide search links are given instead.",
        "w_rain": "Rain likely on {date} ({prob}%): consider indoor options instead of {names}.",
        "tip_book": "Reservation recommended.",
        "tip_hours": "Hours not verified — check before going.",
        "tip_rain": "Rain likely — bring an umbrella.",
        "tip_holiday_shift": "Open on this holiday (usually closed this weekday).",
    },
    "ko": {
        "title": "{city} {n}일 여행",
        "summary": "{city} {n}일 일정 ({first} – {last}): 명소 {acts}곳, 식사 {meals}회 — "
                   "이동이 짧도록 지역별로 묶었습니다.",
        "arrival": "{airport} 도착 (입국 심사·수하물)",
        "to_hotel": "호텔로 이동 ({area})",
        "checkin": "호텔 체크인",
        "dropbags": "호텔에 짐 맡기기",
        "back_hotel": "호텔 복귀",
        "pickup_bags": "짐 찾기 / 체크아웃",
        "to_airport": "{airport}(으)로 이동",
        "departure": "{airport} {time} 출발 (체크인·보안 검색)",
        "departure_when": "{date} {time}",
        "layover_wait": "{airport}에서 대기 (라운지·휴식)",
        "breakfast": "아침",
        "lunch": "점심",
        "dinner": "저녁",
        "meal_near": "{meal}: {area} 근처 {food}",
        "food_any": "현지 음식",
        "free": "{area} 주변 자유 시간",
        "theme_arrival": "도착",
        "theme_departure": "출국",
        "theme_layover": "공항 대기",
        "w_hours": "운영 시간이 확인되지 않은 곳: {names} — 방문 전 확인하세요.",
        "w_long": "{date} 긴 이동: {a} → {b} (약 {min}분).",
        "w_overlap": "{date} 일정이 겹칩니다: {a} / {b}.",
        "w_bad_time": "{date} {a}의 시간 형식이 잘못되었습니다 (HH:MM).",
        "w_tight": "{date} {a} → {b} 이동 시간이 부족합니다 (약 {min}분 필요).",
        "w_before_arrival": "{date} {a} 일정이 도착 전입니다.",
        "w_after_cutoff": "{date} {a} 일정이 공항 출발 기준 시각({time}) 이후에 끝납니다.",
        "w_backtrack": "동선이 이전 지역으로 {n}번 되돌아갑니다.",
        "w_over_budget": "예상 비용이 예산을 초과합니다 ({lines}).",
        "w_sparse": "데이터와 예산에 맞는 명소가 {n}곳뿐이라({slots}개 슬롯) 자유 시간이 포함되었습니다.",
        "w_no_coords": "좌표가 없는 {n}곳은 제외했습니다: {names}.",
        "w_too_far": "중심에서 {km}km 이상 떨어져 제외: {names}.",
        "w_must_missing": "꼭 가고 싶은 곳을 장소 데이터에서 찾지 못했습니다: {names}.",
        "w_must_dropped": "꼭 가고 싶은 곳을 일정에 넣지 못했습니다: {names} (휴무 또는 시간 부족).",
        "w_closed": "제외 (가능한 날짜에 휴무 또는 시간 부족): {names}.",
        "w_hotel": "호텔 위치를 몰라 {area} 근처로 가정했습니다.",
        "w_hotel_area": "호텔 지역 “{wanted}”의 위치를 찾지 못해 {area} 근처로 가정했습니다.",
        "w_layover": "도착과 출발 사이가 {hours}시간뿐이라 호텔에 다녀올 수 없어 공항에서 대기하는 일정입니다.",
        "w_item_closed": "{date} {a}은(는) 휴무입니다.",
        "w_item_hours": "{date} {a} 일정이 운영 시간({hours})을 벗어납니다.",
        "w_holiday": "{date}은(는) 공휴일({name})입니다: 혼잡하거나 운영 시간이 바뀔 수 있습니다.",
        "w_generic_meals": "일부 식사 주변에 식당 데이터가 없어 지도/맛집 검색 링크로 대신했습니다.",
        "w_rain": "{date} 비 예보 ({prob}%): {names} 대신 실내 일정을 고려하세요.",
        "tip_book": "예약을 권장합니다.",
        "tip_hours": "운영 시간 미확인 — 방문 전 확인하세요.",
        "tip_rain": "비 예보 — 우산을 챙기세요.",
        "tip_holiday_shift": "이번 공휴일에는 정상 운영 (평소 이 요일 휴무).",
    },
}


# --------------------------------------------------------------------------- small helpers


def _texts(language: str) -> dict[str, str]:
    return TEXTS.get(language, TEXTS["en"])


def _names(items: Sequence[str], limit: int = 6) -> str:
    unique = list(dict.fromkeys(items))
    shown = ", ".join(unique[:limit])
    return shown + (f" (+{len(unique) - limit})" if len(unique) > limit else "")


def _minute_of(dt: datetime, day: date) -> int:
    return int((dt - datetime.combine(day, time())).total_seconds() // 60)


def _date_label(day: date, language: str) -> str:
    if language == "ko":
        return f"{day.month}/{day.day}({'월화수목금토일'[day.weekday()]})"
    return day.strftime("%a %d %b")


def normalized_weights(inputs: PlanInputs) -> dict[str, float]:
    """Style weights scaled so the strongest style is 1.0 (all-zero → neutral 1.0 each)."""
    values = {k: max(0.0, float(inputs.style_weights.get(k, 0.0))) for k in STYLE_KEYS}
    peak = max(values.values())
    if peak <= 0:
        return dict.fromkeys(STYLE_KEYS, 1.0)
    return {k: v / peak for k, v in values.items()}


def normalized_rating(candidate: Candidate) -> float | None:
    """Rating on a 1-5 scale (Tabelog's compressed scale stretched, 10-point scales halved)."""
    rating = candidate.rating
    if rating is None:
        return None
    if "tabelog" in (candidate.rating_source or "").lower():
        return max(1.0, min(5.0, 3.0 + (rating - 3.0) * 2.0))
    return rating / 2.0 if rating > 5.0 else rating


def _rating_bonus(candidate: Candidate) -> float:
    rating = normalized_rating(candidate)
    if rating is None:
        return 0.0
    return max(-0.3, min(0.5, (rating - 3.8) * 0.4))


def score_candidate(candidate: Candidate, weights: Mapping[str, float], companion: str = "solo") -> float:
    """Preference score: best style match + 15 % of the other styles + rating + companion.

    Explicit ``styles`` count fully; the category's default styles count at
    :data:`CATEGORY_STYLE_FACTOR` (a temple tagged only "culture" still serves sightseeing).
    """
    factors = dict.fromkeys(CATEGORY_STYLES[candidate.category], CATEGORY_STYLE_FACTOR)
    factors.update(dict.fromkeys(candidate.styles, 1.0))
    values = [weights.get(style, 0.0) * factor for style, factor in factors.items()]
    top = max(values, default=0.0)
    affinity = top + 0.15 * (sum(values) - top)
    bonus = COMPANION_BONUS.get(companion, {}).get(candidate.category, 0.0)
    verified = 0.05 if candidate.sources_verified else 0.0
    return round(affinity + _rating_bonus(candidate) + bonus + verified, 6)


# --------------------------------------------------------------------------- context


@dataclass
class _Day:
    index: int
    date: date
    start: int | None
    end: int | None
    deadline: int | None = None
    """Latest return to the hotel (minutes after this date's midnight)."""
    hard_end: bool = False
    """``deadline`` is the airport cut-off: meals must fit before it too."""
    activity_minutes: int = 0
    slots: int = 0
    breaks: list[Break] = field(default_factory=list)
    anchored: bool = False
    weather: dict[str, Any] | None = None
    holiday: str | None = None
    rain: float = 0.0
    stops: list[Candidate] = field(default_factory=list)
    reserve: list[Candidate] = field(default_factory=list)
    schedule: DaySchedule | None = None


@dataclass
class _Ctx:
    inputs: PlanInputs
    texts: dict[str, str]
    pace: PaceProfile
    weights: dict[str, float]
    holidays: Mapping[date, str]
    weather: Mapping[date, dict[str, Any]]
    defaults: budget_mod.CostDefaults
    caps: budget_mod.SpendingCaps
    hotel: Coord
    hotel_label: str
    airport_in: Coord | None
    airport_out: Coord | None
    scores: dict[str, float] = field(default_factory=dict)
    forced: set[str] = field(default_factory=set)
    restaurants: list[Candidate] = field(default_factory=list)
    used_restaurants: set[str] = field(default_factory=set)
    eaten: list[str] = field(default_factory=list)
    wishes: list[str] = field(default_factory=list)
    preferred: list[str] = field(default_factory=list)
    generic_meals: int = 0


def _cuisine_keys(terms: Sequence[str]) -> list[str]:
    keys: list[str] = []
    for term in terms:
        key = canonical_cuisine(term)
        if key and key not in keys:
            keys.append(key)
    return keys


def _matches_term(term: str, candidate: Candidate) -> bool:
    folded = normalize_key(term).replace(" ", "_")
    return (
        name_matches(term, candidate)
        or folded == candidate.category
        or (bool(candidate.area) and normalize_key(term) == normalize_key(candidate.area))
    )


def medoid(points: Sequence[Coord]) -> Coord | None:
    """The point with the smallest total distance to all others (robust "middle" of a set)."""
    unique = sorted(set(points))
    if not unique:
        return None
    return min(unique, key=lambda p: (sum(routing.distance_km(p, q) for q in points), p))


def _reference_points(candidates: Sequence[Candidate], inputs: PlanInputs) -> list[Coord]:
    """Points a usable candidate lies near: the city centre, the hotel and the medoid of the
    located candidates (keeps the plan working when the centre is wrong, e.g. an airport)."""
    refs = [inputs.center]
    if inputs.hotel_point is not None:
        refs.append(inputs.hotel_point)
    middle = medoid([c.point for c in candidates if c.point is not None])  # type: ignore[misc]
    if middle is not None:
        refs.append(middle)
    return refs


def _filter_candidates(
    candidates: Sequence[Candidate], inputs: PlanInputs, exclude_ids: set[str], texts: dict[str, str]
) -> tuple[list[Candidate], list[str]]:
    """Drop excluded/avoided/unlocated/far candidates; returns ``(pool, warnings)``.

    "Far" = more than :data:`MAX_CITY_RADIUS_KM` from every reference point
    (:func:`_reference_points`); must-visit places are never dropped for distance."""
    avoid_food = _cuisine_keys(inputs.avoid_foods)
    must_terms = list(inputs.must_visit)
    refs = _reference_points(candidates, inputs)
    pool: list[Candidate] = []
    no_coords: list[str] = []
    too_far: list[str] = []
    seen: set[str] = set()
    for candidate in sorted(candidates, key=lambda c: c.id):
        if candidate.id in seen or candidate.id in exclude_ids:
            continue
        seen.add(candidate.id)
        if any(_matches_term(term, candidate) for term in inputs.avoid):
            continue
        if candidate.is_food and (
            set(candidate.cuisines) & set(avoid_food)
            or any(name_matches(term, candidate) for term in inputs.avoid_foods)
        ):
            continue
        point = candidate.point
        if point is None:
            no_coords.append(candidate.name)
            continue
        must = any(name_matches(term, candidate) for term in must_terms)
        near = any(routing.distance_km(ref, point) <= MAX_CITY_RADIUS_KM for ref in refs)
        if not near and not must:
            too_far.append(candidate.name)
            continue
        pool.append(candidate)
    warnings: list[str] = []
    if no_coords:
        warnings.append(texts["w_no_coords"].format(n=len(no_coords), names=_names(no_coords)))
    if too_far:
        warnings.append(texts["w_too_far"].format(km=int(MAX_CITY_RADIUS_KM), names=_names(too_far)))
    return pool, warnings


def _match_must_visit(
    pool: Sequence[Candidate], inputs: PlanInputs, scores: Mapping[str, float]
) -> tuple[set[str], list[str]]:
    """Forced candidate ids and the must-visit terms that matched nothing."""
    forced: set[str] = set()
    missing: list[str] = []
    for term in inputs.must_visit:
        matches = [c for c in pool if name_matches(term, c)]
        if not matches:
            missing.append(term)
            continue
        exact = [c for c in matches if normalize_text(term) in c.search_terms()]
        best = max(exact or matches, key=lambda c: (scores.get(c.id, 0.0), c.id))
        forced.add(best.id)
    return forced, missing


@dataclass(frozen=True)
class _Hotel:
    point: Coord
    label: str
    assumed: bool
    """Not the hotel's own location (area centroid, geocoded area or a guess)."""
    area_found: bool = True
    """False when ``hotel_area`` was given but could not be located (the label then names
    the place actually assumed, never the user's area)."""


def _hotel_point(
    inputs: PlanInputs, activities: Sequence[Candidate], scores: Mapping[str, float]
) -> _Hotel:
    """The hotel: given/geocoded point, else the centroid of candidates in the hotel area,
    else the medoid of the best-scored sights, else the city centre."""
    if inputs.hotel_point is not None:
        return _Hotel(inputs.hotel_point, inputs.hotel_area or inputs.city, inputs.hotel_approximate)
    if inputs.hotel_area:
        wanted = normalize_key(inputs.hotel_area)
        in_area = [
            c.point for c in activities
            if c.point is not None and c.area and (wanted in normalize_key(c.area) or normalize_key(c.area) in wanted)
        ]
        centre = routing.centroid(in_area)  # type: ignore[arg-type]
        if centre is not None:
            return _Hotel(centre, inputs.hotel_area, True)
    found = not inputs.hotel_area
    top = sorted(activities, key=lambda c: (-scores.get(c.id, 0.0), c.id))[:12]
    points = [c for c in top if c.point is not None]
    if not points:
        return _Hotel(inputs.center, inputs.city, True, found)
    middle = min(
        points,
        key=lambda c: (sum(routing.distance_km(c.point, o.point) for o in points), c.id),  # type: ignore[arg-type]
    )
    return _Hotel(middle.point, middle.area or middle.name, True, found)  # type: ignore[arg-type]


def _airport_point(code: str | None) -> Coord | None:
    airport = get_airport(code) if code else None
    return (airport.lat, airport.lon) if airport else None


# --------------------------------------------------------------------------- days


@dataclass(frozen=True)
class _Timing:
    ready: datetime
    """Free in town (after immigration, transfer and check-in)."""
    leave: datetime
    """Leave the hotel for the airport."""
    cutoff: datetime
    """Back at the hotel to collect the bags (``leave`` − :data:`BAG_PICKUP_MIN`)."""
    transfer_in: TravelHop
    transfer_out: TravelHop

    @property
    def layover(self) -> bool:
        """Too short a stay to reach the hotel and come back: less than
        :data:`DAY_MIN_WINDOW` between being ready in town and collecting the bags."""
        return self.ready + timedelta(minutes=DAY_MIN_WINDOW) > self.cutoff


def _timing(inputs: PlanInputs, ctx_hotel: Coord, airport_in: Coord | None, airport_out: Coord | None) -> _Timing:
    transfer_in = routing.travel_estimate(airport_in, ctx_hotel) if airport_in else DEFAULT_TRANSFER
    transfer_out = routing.travel_estimate(ctx_hotel, airport_out) if airport_out else DEFAULT_TRANSFER
    ready = inputs.arrival_at + timedelta(minutes=IMMIGRATION_MIN + transfer_in.minutes + CHECKIN_MIN)
    leave = min(
        inputs.departure_at - timedelta(minutes=AIRPORT_CUTOFF_MIN),
        inputs.departure_at - timedelta(minutes=AIRPORT_ARRIVE_BEFORE_MIN + transfer_out.minutes),
    )
    return _Timing(ready, leave, leave - timedelta(minutes=BAG_PICKUP_MIN), transfer_in, transfer_out)


def _meal_breaks(start: int, end: int, pace: PaceProfile) -> list[Break]:
    breaks: list[Break] = []
    if start <= LUNCH[1] and end >= LUNCH[0] + pace.lunch:
        breaks.append(Break("lunch", LUNCH[0], LUNCH[1], pace.lunch + MEAL_ALLOWANCE_MIN))
    if start <= DINNER[1] and end >= DINNER[0] + pace.dinner:
        breaks.append(Break("dinner", DINNER[0], DINNER[1], pace.dinner + MEAL_ALLOWANCE_MIN))
    return breaks


def _build_days(ctx: _Ctx, timing: _Timing, block_date: date) -> list[_Day]:
    """Day windows. Every day ends by ``cutoff`` − :data:`OVERRUN_TOLERANCE_MIN` (the
    ordering may overrun ``end`` by that tolerance, which then lands on the cutoff); the
    departure-block day gets ``hard_end`` when the cutoff is what limits it."""
    days: list[_Day] = []
    dates = ctx.inputs.dates
    limit = timing.cutoff - timedelta(minutes=OVERRUN_TOLERANCE_MIN)
    for index, day in enumerate(dates):
        midnight = datetime.combine(day, time())
        start_dt = max(midnight + timedelta(minutes=ctx.pace.start), timing.ready)
        end_dt = min(midnight + timedelta(minutes=ctx.pace.end), limit)
        weather = ctx.weather.get(day)
        entry = _Day(
            index=index, date=day, start=None, end=None,
            weather=dict(weather) if weather else None,
            holiday=ctx.holidays.get(day) if ctx.holidays else None,
            rain=weather_mod.rain_factor(weather),
        )
        if (end_dt - start_dt).total_seconds() / 60 >= DAY_MIN_WINDOW:
            entry.start, entry.end = _minute_of(start_dt, day), _minute_of(end_dt, day)
            entry.deadline = entry.end + OVERRUN_TOLERANCE_MIN + RETURN_ALLOWANCE_MIN
            if day == block_date:
                cutoff = _minute_of(timing.cutoff, day)
                entry.hard_end = cutoff <= entry.deadline
                entry.deadline = min(entry.deadline, cutoff)
            entry.breaks = _meal_breaks(entry.start, entry.end, ctx.pace)
            meal_minutes = sum(b.duration for b in entry.breaks)
            entry.activity_minutes = max(0, entry.end - entry.start - meal_minutes - RETURN_ALLOWANCE_MIN)
            entry.slots = min(ctx.pace.max_per_day, entry.activity_minutes // ctx.pace.slot_minutes)
            short_day = start_dt > midnight + timedelta(minutes=ctx.pace.start) or end_dt < midnight + timedelta(
                minutes=ctx.pace.end
            )
            entry.anchored = short_day and (index == 0 or day == block_date)
        days.append(entry)
    return days


# --------------------------------------------------------------------------- selection & clustering


def _activity_cost(ctx: _Ctx, candidate: Candidate) -> tuple[float, str, bool]:
    """``(cost, currency, estimated)`` per person."""
    if candidate.est_cost is not None:
        return candidate.est_cost, candidate.cost_currency or ctx.inputs.currency, False
    return ctx.defaults.activity(candidate.category), ctx.defaults.currency, True


def _activity_cost_local(ctx: _Ctx, candidate: Candidate) -> float:
    amount, currency, _ = _activity_cost(ctx, candidate)
    return budget_mod.item_cost_local(amount, currency, ctx.inputs.currency)


def _select(
    ctx: _Ctx, activities: Sequence[Candidate], max_count: int, minutes_budget: int
) -> tuple[list[Candidate], list[Candidate]]:
    """Greedy pick: forced first, then best adjusted score (category diversity, time and
    activity-budget limits). Returns ``(selected, forced_overflow)``."""
    chosen: list[Candidate] = []
    used, spend = 0, 0.0
    counts: Counter[str] = Counter()
    by_score = sorted(activities, key=lambda c: (-ctx.scores[c.id], c.id))
    forced = [c for c in by_score if c.id in ctx.forced]
    overflow = forced[max_count:]
    for candidate in forced[:max_count]:
        chosen.append(candidate)
        used += candidate.visit_minutes + SLOT_TRAVEL_MIN
        spend += _activity_cost_local(ctx, candidate)
        counts[candidate.category] += 1
    chosen_ids = {c.id for c in chosen} | {c.id for c in overflow}
    cap = ctx.caps.activities_total
    while len(chosen) < max_count:
        best: tuple[float, str] | None = None
        pick: Candidate | None = None
        for candidate in by_score:
            if candidate.id in chosen_ids:
                continue
            if chosen and used + candidate.visit_minutes + SLOT_TRAVEL_MIN > minutes_budget * 1.1:
                continue
            cost = _activity_cost_local(ctx, candidate)
            if cap is not None and cost > 0 and spend + cost > cap:
                continue
            adjusted = ctx.scores[candidate.id] - DIVERSITY_STEP * counts[candidate.category]
            key = (adjusted, candidate.id)
            if best is None or key[0] > best[0] + 1e-12 or (abs(key[0] - best[0]) <= 1e-12 and key[1] < best[1]):
                best, pick = key, candidate
        if pick is None:
            break
        chosen.append(pick)
        chosen_ids.add(pick.id)
        used += pick.visit_minutes + SLOT_TRAVEL_MIN
        spend += _activity_cost_local(ctx, pick)
        counts[pick.category] += 1
    return chosen, overflow


def _stop_for(ctx: _Ctx, candidate: Candidate, day: _Day) -> Stop:
    windows = calendar_util.open_windows(candidate, day.date, ctx.holidays)
    return Stop(
        key=candidate.id,
        point=candidate.point,  # type: ignore[arg-type]
        duration=candidate.visit_minutes,
        windows=tuple(windows) if windows is not None else None,
        preferred=BEST_TIME_WINDOWS.get(candidate.best_time),
    )


def _day_penalty(ctx: _Ctx, candidate: Candidate, day: _Day) -> float:
    """km-equivalent cost of visiting ``candidate`` on ``day`` (≥ INFEASIBLE = impossible)."""
    if day.start is None or day.end is None:
        return INFEASIBLE
    windows = calendar_util.open_windows(candidate, day.date, ctx.holidays)
    if windows == []:
        return INFEASIBLE
    if windows and not any(
        min(close, day.end) - max(opening, day.start) >= candidate.visit_minutes for opening, close in windows
    ):
        return INFEASIBLE
    cost = 0.0
    preferred = BEST_TIME_WINDOWS.get(candidate.best_time)
    if preferred is not None:
        overlap = min(preferred[1], day.end) - max(preferred[0], day.start)
        if overlap <= 0:
            cost += NIGHTLIFE_MISS_KM if candidate.category == "nightlife" else BEST_TIME_MISS_KM
        elif overlap < candidate.visit_minutes:
            cost += BEST_TIME_PARTIAL_KM
    if candidate.is_outdoor:
        cost += RAIN_OUTDOOR_KM * day.rain
    return cost


def _pick_day_stops(ctx: _Ctx, day: _Day, members: list[tuple[Candidate, float]]) -> None:
    """Choose the day's stops from its area: forced first, then the best value of score minus
    day penalty minus distance from the stops picked so far (the hotel for anchored days), with
    category diversity. The rest become the day's reserve."""
    forced = sorted((c for c, _ in members if c.id in ctx.forced), key=lambda c: (-ctx.scores[c.id], c.id))
    picks: list[Candidate] = list(forced)
    counts: Counter[str] = Counter(c.category for c in picks)
    pool = sorted(((c, p) for c, p in members if c.id not in ctx.forced), key=lambda cp: cp[0].id)
    while len(picks) < day.slots and pool:
        best = pool[0]
        best_value = None
        anchor_points = [c.point for c in picks] or ([ctx.hotel] if day.anchored else [])
        centre = routing.centroid(anchor_points)  # type: ignore[arg-type]
        for cand, penalty in pool:
            spread = routing.distance_km(centre, cand.point) if centre is not None else 0.0  # type: ignore[arg-type]
            wet = WET_OUTDOOR_PICK_PENALTY if cand.is_outdoor and day.rain >= WET_DAY_RAIN else 0.0
            value = (
                ctx.scores[cand.id] - DAY_PENALTY_WEIGHT * penalty - PICK_DISTANCE_WEIGHT * spread - wet
                - DIVERSITY_STEP * counts[cand.category]
            )
            if best_value is None or value > best_value + 1e-12:
                best, best_value = (cand, penalty), value
        pool.remove(best)
        picks.append(best[0])
        counts[best[0].category] += 1
    day.stops = picks
    day.reserve = [c for c, _ in pool]


def _assign_days(ctx: _Ctx, pool: list[Candidate], days: list[_Day], seed: int) -> list[Candidate]:
    """Cluster the candidate pool into day areas (one per day with activity time) and pick each
    day's stops. Returns pool members infeasible on their area's day (spare for other days)."""
    active = [d for d in days if d.slots > 0]
    if not pool or not active:
        return list(pool)
    slack = CLUSTER_SLACK if len(pool) > sum(d.slots for d in active) else 1.0
    room = max(len(pool), int(len(pool) * slack + 0.999))
    capacities = routing.proportional_capacities(room, [max(1, d.activity_minutes) for d in active])
    extra = [[_day_penalty(ctx, c, d) for d in active] for c in pool]
    labels = routing.balanced_clusters(
        [c.point for c in pool],  # type: ignore[misc]
        capacities,
        anchors=[ctx.hotel if d.anchored else None for d in active],
        extra_cost=extra,
        seed=seed,
    )
    _swap_whole_days(labels, extra, active, capacities)
    spare: list[Candidate] = []
    for j, day in enumerate(active):
        members = [(c, extra[i][j]) for i, c in enumerate(pool) if labels[i] == j]
        spare += [c for c, penalty in members if penalty >= INFEASIBLE]
        _pick_day_stops(ctx, day, [(c, p) for c, p in members if p < INFEASIBLE])
    return spare


def _swap_whole_days(
    labels: list[int], extra: list[list[float]], active: list[_Day], capacities: list[int]
) -> None:
    """Exchange the stop sets of two unanchored equal-capacity days when that lowers the
    closure/weather/time penalties (the clusters' geography is unchanged)."""
    k = len(active)
    improved = True
    while improved:
        improved = False
        for a in range(k):
            for b in range(a + 1, k):
                if active[a].anchored or active[b].anchored or capacities[a] != capacities[b]:
                    continue
                members_a = [i for i, lab in enumerate(labels) if lab == a]
                members_b = [i for i, lab in enumerate(labels) if lab == b]
                now = sum(extra[i][a] for i in members_a) + sum(extra[i][b] for i in members_b)
                swapped = sum(extra[i][b] for i in members_a) + sum(extra[i][a] for i in members_b)
                if swapped < now - 1e-9:
                    for i in members_a:
                        labels[i] = b
                    for i in members_b:
                        labels[i] = a
                    improved = True


def _fit_day(ctx: _Ctx, day: _Day) -> list[Candidate]:
    """Order the day's stops, dropping the weakest until it fits; returns dropped stops."""
    dropped: list[Candidate] = []
    if day.start is None or day.end is None:
        dropped, day.stops = list(day.stops), []
        return dropped
    while True:
        stops = [_stop_for(ctx, c, day) for c in day.stops]
        schedule = routing.order_stops(
            stops, ctx.hotel, ctx.hotel, start_minute=day.start, end_minute=day.end, breaks=day.breaks
        )
        late = [day.stops[v.index] for v in schedule.visits if v.kind == "stop" and v.late > LATE_TOLERANCE_MIN]
        if (schedule.overrun <= OVERRUN_TOLERANCE_MIN and not late) or not day.stops:
            day.schedule = schedule
            return dropped
        pool = late or day.stops
        victim = min(pool, key=lambda c: (c.id in ctx.forced, ctx.scores[c.id], c.id))
        day.stops.remove(victim)
        dropped.append(victim)


def _idle_minutes(day: _Day) -> int:
    if day.schedule is None or day.end is None:
        return 0
    waits = sum(v.wait for v in day.schedule.visits if v.kind == "stop")
    return waits + max(0, day.end - day.schedule.finish)


def _try_add(ctx: _Ctx, day: _Day, candidate: Candidate) -> bool:
    """Add ``candidate`` to the day when the re-optimised day still fits (never an outdoor
    sight on a wet day)."""
    if _day_penalty(ctx, candidate, day) >= INFEASIBLE:
        return False
    if candidate.is_outdoor and day.rain >= WET_DAY_RAIN:
        return False
    trial = [*day.stops, candidate]
    stops = [_stop_for(ctx, c, day) for c in trial]
    schedule = routing.order_stops(
        stops, ctx.hotel, ctx.hotel, start_minute=day.start, end_minute=day.end,  # type: ignore[arg-type]
        breaks=day.breaks,
    )
    late = any(v.kind == "stop" and v.late > LATE_TOLERANCE_MIN for v in schedule.visits)
    if schedule.overrun > OVERRUN_TOLERANCE_MIN or late:
        return False
    day.stops, day.schedule = trial, schedule
    return True


def _rescue_forced(ctx: _Ctx, days: list[_Day], spare: list[Candidate]) -> None:
    """Retry must-visit sights that were dropped (e.g. two forced stops that did not fit one
    short day together) on any day where they still fit, nearest day area first."""
    open_days = [d for d in days if d.schedule is not None and d.start is not None]
    for candidate in sorted({c.id: c for c in spare if c.id in ctx.forced}.values(),
                            key=lambda c: (-ctx.scores[c.id], c.id)):
        point: Coord = candidate.point  # type: ignore[assignment]
        ranked = sorted(open_days, key=lambda d: (routing.distance_km(_day_centre(ctx, d), point), d.index))
        if any(_try_add(ctx, day, candidate) for day in ranked):
            spare[:] = [c for c in spare if c.id != candidate.id]


def _day_centre(ctx: _Ctx, day: _Day) -> Coord:
    """Centroid of the day's stops (the hotel for a day without stops)."""
    return routing.centroid([c.point for c in day.stops]) or ctx.hotel  # type: ignore[misc]


def _fill_days(ctx: _Ctx, days: list[_Day], spare: list[Candidate]) -> None:
    """Use idle time: add the day's own reserve first, then unused sights near the day's area.
    ``spare`` is updated in place and finally also receives the unused reserves."""
    for day in days:
        if day.schedule is None or day.start is None:
            continue
        added = 0
        limit = max(day.slots, ctx.pace.max_per_day) + ctx.pace.fill_extra
        while added < FILL_MAX_PER_DAY and len(day.stops) < limit and _idle_minutes(day) >= FILL_MIN_IDLE:
            centre = routing.centroid([c.point for c in day.stops]) or ctx.hotel  # type: ignore[misc]
            nearby = [
                c for c in spare
                if routing.distance_km(centre, c.point) <= FILL_RADIUS_KM  # type: ignore[arg-type]
            ]
            reserve = [
                c for c in day.reserve
                if routing.distance_km(centre, c.point) <= RESERVE_RADIUS_KM  # type: ignore[arg-type]
            ]
            options = sorted(reserve, key=lambda c: (-ctx.scores[c.id], c.id)) + sorted(
                nearby, key=lambda c: (-ctx.scores[c.id], c.id)
            )
            chosen = next((c for c in options if _try_add(ctx, day, c)), None)
            if chosen is None:
                break
            added += 1
            if chosen in day.reserve:
                day.reserve.remove(chosen)
            else:
                spare.remove(chosen)
    for day in days:
        spare.extend(day.reserve)
        day.reserve = []


# --------------------------------------------------------------------------- meals


def _restaurant_cost(ctx: _Ctx, candidate: Candidate, meal: str) -> tuple[float, str, bool]:
    if candidate.est_cost is not None:
        return candidate.est_cost, candidate.cost_currency or ctx.inputs.currency, False
    kind = "cafe" if candidate.category == "cafe" and meal != "dinner" else meal
    return ctx.defaults.meal(kind, candidate.price_level), ctx.defaults.currency, True


def _food_score(ctx: _Ctx, candidate: Candidate, meal: str) -> float:
    cuisines = candidate.cuisines
    unmet = [w for w in ctx.wishes if w not in ctx.eaten]
    score = _rating_bonus(candidate) + (0.05 if candidate.sources_verified else 0.0)
    if any(w in cuisines for w in unmet):
        score += 1.0
    elif any(w in cuisines for w in ctx.wishes):
        score += 0.3
    if any(p in cuisines for p in ctx.preferred):
        score += 0.5
    if cuisines and all(c in ctx.eaten for c in cuisines):
        score -= 0.4
    if candidate.category == "cafe" and meal != "breakfast":
        score -= 0.3
    low, high = ctx.inputs.restaurant_budget_min, ctx.inputs.restaurant_budget_max
    if low is not None or high is not None:
        amount, currency, _ = _restaurant_cost(ctx, candidate, meal)
        target = ctx.inputs.restaurant_budget_currency or ctx.inputs.currency
        value = budget_mod.static_convert(amount, currency, target)
        if value is not None and (low is None or value >= low) and (high is None or value <= high):
            score += 0.2
    return score


@dataclass(frozen=True)
class _MealSlot:
    """When a meal is wanted: free from ``ready`` at ``pos``, not before ``earliest``."""

    meal: str
    day: date
    ready: int
    earliest: int
    duration: int
    pos: Coord
    nxt: Coord | None = None
    deadline: int | None = None
    """Back at the hotel by then (airport cut-off days)."""
    max_wait: int = MEAL_MAX_WAIT_MIN


@dataclass(frozen=True)
class _MealPick:
    restaurant: Candidate
    arrive: int
    start: int


def _meal_timing(ctx: _Ctx, candidate: Candidate, slot: _MealSlot) -> tuple[int, int] | None:
    """``(arrive, start)`` of the meal at ``candidate`` — travel from ``slot.pos`` included —
    or ``None`` when it is closed then (the meal must end by closing time), would need a wait
    longer than ``slot.max_wait`` or would end too late for ``slot.deadline``."""
    arrive = slot.ready + int(routing.travel_estimate(slot.pos, candidate.point).minutes)  # type: ignore[arg-type]
    start = max(arrive, slot.earliest)
    windows = calendar_util.open_windows(candidate, slot.day, ctx.holidays)
    if windows is not None:
        fitted, _, late = routing.fit_window(start, slot.duration, tuple(windows))
        if late > 0 or fitted - start > slot.max_wait:
            return None
        start = fitted
    if slot.deadline is not None:
        back = int(routing.travel_estimate(candidate.point, ctx.hotel).minutes)  # type: ignore[arg-type]
        if start + slot.duration + back > slot.deadline:
            return None
    return arrive, start


def _choose_restaurant(ctx: _Ctx, slot: _MealSlot) -> _MealPick | None:
    """Best restaurant for a meal near ``slot.pos`` (on the way to ``slot.nxt``) that is open
    for the whole meal at the time the traveller actually gets there, or ``None``.

    A must-visit restaurant gets a large bonus and no distance limit until it has been
    visited (breakfast included); a breakfast café must be within walking distance and may
    repeat on later mornings (with a penalty)."""
    meal = slot.meal
    cap = ctx.caps.meal.get(meal)
    best: tuple[float, str] | None = None
    pick: _MealPick | None = None
    for candidate in ctx.restaurants:
        visited = candidate.id in ctx.used_restaurants
        if visited and meal != "breakfast":
            continue
        if meal == "breakfast" and candidate.category != "cafe" and candidate.best_time != "morning":
            continue
        if meal == "dinner" and candidate.category == "cafe":
            continue
        dist = routing.distance_km(slot.pos, candidate.point)  # type: ignore[arg-type]
        forced = candidate.id in ctx.forced and not visited
        limit = BREAKFAST_RADIUS_KM if meal == "breakfast" else MEAL_SEARCH_KM
        if dist > limit and not (forced and meal != "breakfast"):  # breakfast is always a short walk
            continue
        timing = _meal_timing(ctx, candidate, slot)
        if timing is None:
            continue
        amount, currency, _ = _restaurant_cost(ctx, candidate, meal)
        local = budget_mod.item_cost_local(amount, currency, ctx.inputs.currency)
        if cap is not None and local > cap * 1.15 and not forced:
            continue
        detour = dist
        if slot.nxt is not None:
            onward = routing.distance_km(candidate.point, slot.nxt)  # type: ignore[arg-type]
            detour += onward - routing.distance_km(slot.pos, slot.nxt)
        score = (
            _food_score(ctx, candidate, meal) - 0.4 * max(0.0, detour)
            + (10.0 if forced else 0.0) - (REVISIT_PENALTY if visited else 0.0)
        )
        key = (score, candidate.id)
        if best is None or key[0] > best[0] + 1e-12 or (abs(key[0] - best[0]) <= 1e-12 and key[1] < best[1]):
            best, pick = key, _MealPick(candidate, *timing)
    return pick


def _area_of(point_area: str | None, ctx: _Ctx) -> str:
    return point_area or ctx.hotel_label


# --------------------------------------------------------------------------- item building


def _item(start: int, end: int, kind: str, title: str, **fields: Any) -> PlanItem:
    return PlanItem(start=minutes_to_hhmm(start), end=minutes_to_hhmm(end), kind=kind, title=title, **fields)


def _candidate_fields(ctx: _Ctx, candidate: Candidate, day: _Day) -> dict[str, Any]:
    t = ctx.texts
    hours = calendar_util.hours_label(candidate, day.date, ctx.holidays)
    tips: list[str] = []
    if candidate.booking_required:
        tips.append(t["tip_book"])
    unverified = not candidate.hours_known and not candidate.sources_verified
    if unverified and not candidate.is_food:
        tips.append(t["tip_hours"])
    if candidate.is_outdoor and weather_mod.is_wet(day.weather):
        tips.append(t["tip_rain"])
    if (candidate.holiday_shift and day.date.weekday() in candidate.closed_weekdays
            and calendar_util.is_holiday(day.date, ctx.holidays)):
        tips.append(t["tip_holiday_shift"])
    return {
        "area": candidate.area,
        "lat": candidate.lat,
        "lng": candidate.lon,
        "description": candidate.description,
        "tips": " ".join(tips) or None,
        "source_urls": list(candidate.source_urls),
        "candidate_id": candidate.id,
        "category": candidate.category,
        "name_local": candidate.name_local,
        "hours": hours or candidate.opening_hours_text,
        "hours_verified": candidate.hours_known or candidate.sources_verified,
        "rating": candidate.rating,
        "rating_source": candidate.rating_source,
        "booking_required": candidate.booking_required,
        "is_outdoor": candidate.is_outdoor,
    }


def _free_item(ctx: _Ctx, start: int, end: int, area: str | None) -> PlanItem:
    return _item(start, end, "free", ctx.texts["free"].format(area=_area_of(area, ctx)), area=area)


def _meal_item(
    ctx: _Ctx, day: _Day, meal: str, start: int, duration: int, restaurant: Candidate | None, area: str | None
) -> PlanItem:
    t = ctx.texts
    if restaurant is not None:
        amount, currency, estimated = _restaurant_cost(ctx, restaurant, meal)
        fields = _candidate_fields(ctx, restaurant, day)
        cuisine = restaurant.cuisines[0] if restaurant.cuisines else None
        return _item(start, start + duration, "meal", f"{t[meal]}: {restaurant.name}", est_cost=amount,
                     cost_currency=currency, cost_estimated=estimated, meal=meal, cuisine=cuisine, **fields)
    unmet = [w for w in ctx.wishes if w not in ctx.eaten]
    cuisine = (unmet or ctx.preferred or [None])[0]
    food = cuisine.replace("_", " ") if cuisine else t["food_any"]
    place = _area_of(area, ctx)
    default = ctx.defaults.meal(meal)
    amount = default
    cap = ctx.caps.meal.get(meal)
    if cap is not None:
        capped = budget_mod.static_convert(cap, ctx.inputs.currency, ctx.defaults.currency)
        if capped is not None:
            amount = budget_mod.nice_amount(max(default * GENERIC_MEAL_FLOOR, min(default, capped)))
    ctx.generic_meals += 1
    if cuisine:
        ctx.eaten.append(cuisine)
    query = food if cuisine else ("restaurant" if meal != "breakfast" else "cafe")
    item_links = links.meal_search_links(query, place, ctx.inputs.city, ctx.inputs.country, cuisine)
    return _item(
        start, start + duration, "meal", t["meal_near"].format(meal=t[meal], food=food, area=place),
        area=area, est_cost=amount, cost_currency=ctx.defaults.currency, cost_estimated=True, meal=meal,
        cuisine=cuisine, search_query=query, map_url=item_links["google_maps"], links=item_links,
    )


def _take_restaurant(ctx: _Ctx, restaurant: Candidate | None) -> None:
    """Mark a restaurant as visited (no repeat, must-visit satisfied) and its cuisines as eaten."""
    if restaurant is None:
        return
    ctx.used_restaurants.add(restaurant.id)
    for cuisine in restaurant.cuisines:
        ctx.eaten.append(cuisine)


def _breakfast(ctx: _Ctx, day: _Day) -> tuple[list[PlanItem], Coord]:
    """Optional breakfast at a café within walking distance of the hotel, ending when the day
    starts (no waiting for a later opening)."""
    if day.start is None or day.index == 0:
        return [], ctx.hotel
    start = day.start - BREAKFAST_MIN
    slot = _MealSlot("breakfast", day.date, start - BREAKFAST_WALK_MIN, start, BREAKFAST_MIN, ctx.hotel,
                     max_wait=0)
    pick = _choose_restaurant(ctx, slot)
    if pick is None:
        return [], ctx.hotel
    cafe = pick.restaurant
    item = _meal_item(ctx, day, "breakfast", pick.start, BREAKFAST_MIN, cafe, cafe.area)
    _take_restaurant(ctx, cafe)
    return [item], cafe.point  # type: ignore[return-value]


@dataclass
class _Timeline:
    """A day's re-simulated items; ``t``/``pos`` = when and where the last item ends; ``late``
    = stops that would end more than :data:`LATE_TOLERANCE_MIN` after closing."""

    items: list[PlanItem]
    t: int
    pos: Coord
    late: list[Candidate]


def _day_timeline(ctx: _Ctx, day: _Day) -> _Timeline:
    """Activities, meals and free time of one day.

    Times are re-simulated along the ordered visits (travel from the actual previous place,
    opening windows), so restaurant detours shift later stops. Meals use
    :func:`_choose_restaurant` at the real arrival time; on a ``hard_end`` day every meal also
    leaves time to get back to the hotel by the cut-off (a generic meal is shortened or
    skipped)."""
    assert day.start is not None and day.schedule is not None
    items, pos = _breakfast(ctx, day)
    t = day.start
    area: str | None = ctx.hotel_label
    hard_deadline = day.deadline if day.hard_end else None
    late: list[Candidate] = []
    visits = list(day.schedule.visits)
    for n, visit in enumerate(visits):
        if visit.kind == "stop":
            candidate = day.stops[visit.index]
            hop = routing.travel_estimate(pos, candidate.point)  # type: ignore[arg-type]
            arrive = t + int(hop.minutes)
            stop = _stop_for(ctx, candidate, day)
            start, _, lateness = routing.fit_window(arrive, candidate.visit_minutes, stop.windows)
            if lateness > LATE_TOLERANCE_MIN:
                late.append(candidate)
            if start - arrive >= FREE_GAP_MIN:
                items.append(_free_item(ctx, arrive, start, candidate.area))
            amount, currency, estimated = _activity_cost(ctx, candidate)
            items.append(_item(start, start + candidate.visit_minutes, "activity", candidate.name,
                               est_cost=amount, cost_currency=currency, cost_estimated=estimated,
                               **_candidate_fields(ctx, candidate, day)))
            t, pos, area = start + candidate.visit_minutes, candidate.point, candidate.area or area  # type: ignore[assignment]
            continue
        brk = day.breaks[visit.index]
        duration = brk.duration - MEAL_ALLOWANCE_MIN
        nxt = next((day.stops[v.index].point for v in visits[n + 1:] if v.kind == "stop"), ctx.hotel)
        earliest = min(visit.start, brk.earliest)
        pick = _choose_restaurant(ctx, _MealSlot(brk.key, day.date, t, earliest, duration, pos, nxt,
                                                 deadline=hard_deadline))
        restaurant = pick.restaurant if pick is not None else None
        if pick is not None:
            arrive, start = pick.arrive, pick.start
        else:
            arrive, start = t, max(t, earliest)
            if hard_deadline is not None:
                back = int(routing.travel_estimate(pos, ctx.hotel).minutes)
                duration = min(duration, hard_deadline - back - start)
                if duration < MIN_MEAL_MIN:
                    continue
        _take_restaurant(ctx, restaurant)
        if start - arrive >= FREE_GAP_MIN:
            items.append(_free_item(ctx, arrive, start, restaurant.area if restaurant else area))
        items.append(_meal_item(ctx, day, brk.key, start, duration, restaurant,
                                restaurant.area if restaurant and restaurant.area else area))
        t = start + duration
        if restaurant is not None:
            pos, area = restaurant.point, restaurant.area or area  # type: ignore[assignment]
    return _Timeline(items, t, pos, late)


def _arrival_block(ctx: _Ctx, timing: _Timing, day: date) -> list[PlanItem]:
    t = ctx.texts
    inputs = ctx.inputs
    arrive = _minute_of(inputs.arrival_at, day)
    out = arrive + IMMIGRATION_MIN
    at_hotel = out + int(timing.transfer_in.minutes)
    code = inputs.arrival_airport or inputs.destination
    lat, lng = ctx.airport_in if ctx.airport_in else (None, None)
    hotel_title = t["checkin"] if at_hotel >= 15 * 60 else t["dropbags"]
    fare = ctx.defaults.airport_transfer(timing.transfer_in.distance_km)
    return [
        _item(arrive, out, "arrival", t["arrival"].format(airport=code), lat=lat, lng=lng,
              map_url=links.google_maps_search(f"{code} airport", None, inputs.city)),
        _item(out, at_hotel, "transit", t["to_hotel"].format(area=ctx.hotel_label),
              est_cost=fare, cost_currency=ctx.defaults.currency, cost_estimated=True),
        _item(at_hotel, at_hotel + CHECKIN_MIN, "hotel", hotel_title, area=ctx.hotel_label,
              lat=ctx.hotel[0], lng=ctx.hotel[1]),
    ]


def _departure_title(ctx: _Ctx, code: str, departs: int) -> str:
    """Departure item title; names the date too when the flight leaves after midnight of the
    block's day (an overnight departure's block starts the evening before)."""
    when = minutes_to_hhmm(departs)
    if departs >= 24 * 60:
        label = _date_label(ctx.inputs.departure_at.date(), ctx.inputs.language)
        when = ctx.texts["departure_when"].format(time=when, date=label)
    return ctx.texts["departure"].format(airport=code, time=when)


def _departure_block(ctx: _Ctx, timing: _Timing, day: date, t: int | None, pos: Coord) -> list[PlanItem]:
    """Free time until the bag pickup, pickup, transfer and flight — all in minutes after
    ``day``'s midnight (past 24:00 for a block that runs over midnight)."""
    tx = ctx.texts
    inputs = ctx.inputs
    leave = _minute_of(timing.leave, day)
    pickup = leave - BAG_PICKUP_MIN
    items: list[PlanItem] = []
    if t is not None:
        back = t + int(routing.travel_estimate(pos, ctx.hotel).minutes)
        if pickup - back >= FREE_GAP_MIN:
            items.append(_free_item(ctx, back, pickup, ctx.hotel_label))
    code = inputs.departure_airport or inputs.destination
    at_airport = leave + int(timing.transfer_out.minutes)
    departs = _minute_of(inputs.departure_at, day)
    lat, lng = ctx.airport_out if ctx.airport_out else (None, None)
    fare = ctx.defaults.airport_transfer(timing.transfer_out.distance_km)
    items += [
        _item(pickup, leave, "hotel", tx["pickup_bags"], area=ctx.hotel_label, lat=ctx.hotel[0], lng=ctx.hotel[1]),
        _item(leave, at_airport, "transit", tx["to_airport"].format(airport=code),
              est_cost=fare, cost_currency=ctx.defaults.currency, cost_estimated=True),
        _item(at_airport, departs, "departure", _departure_title(ctx, code, departs), lat=lat, lng=lng,
              map_url=links.google_maps_search(f"{code} airport", None, inputs.city)),
    ]
    return items


def _theme(ctx: _Ctx, items: list[PlanItem]) -> tuple[str, list[str]]:
    areas = [i.area for i in items if i.kind == "activity" and i.area]
    areas = list(dict.fromkeys(areas))
    if areas:
        return " · ".join(areas[:3]), areas
    names = [i.title for i in items if i.kind == "activity"]
    if names:
        return " · ".join(names[:2]), []
    kinds = {i.kind for i in items}
    if "arrival" in kinds:
        return ctx.texts["theme_arrival"], []
    if "departure" in kinds:
        return ctx.texts["theme_departure"], []
    return "", []


def _timeline_within(ctx: _Ctx, day: _Day) -> tuple[_Timeline, list[Candidate]]:
    """Build the day's timeline; while the return to the hotel would be later than
    ``day.deadline`` or a stop would end past closing (restaurant detours are not part of the
    ordering model), drop a stop — a late one, the weakest first, sparing must-visits while
    possible — and re-plan the day. Returns the timeline and the dropped stops."""
    assert day.deadline is not None
    victims: list[Candidate] = []
    while True:
        snapshot = (set(ctx.used_restaurants), list(ctx.eaten), ctx.generic_meals)
        timeline = _day_timeline(ctx, day)
        back = timeline.t + int(routing.travel_estimate(timeline.pos, ctx.hotel).minutes)
        if (back <= day.deadline and not timeline.late) or not day.stops:
            return timeline, victims
        ctx.used_restaurants, ctx.eaten, ctx.generic_meals = snapshot
        pool = timeline.late or day.stops
        if all(c.id in ctx.forced for c in pool):
            pool = [c for c in day.stops if c.id not in ctx.forced] or pool
        victim = min(pool, key=lambda c: (c.id in ctx.forced, ctx.scores[c.id], c.id))
        day.stops.remove(victim)
        victims.append(victim)
        day.schedule = routing.order_stops(
            [_stop_for(ctx, c, day) for c in day.stops], ctx.hotel, ctx.hotel,
            start_minute=day.start, end_minute=day.end, breaks=day.breaks,  # type: ignore[arg-type]
        )


def _build_plan_day(ctx: _Ctx, day: _Day, timing: _Timing, block_date: date) -> tuple[PlanDay, list[Candidate]]:
    """One plan day (arrival block on the first day, departure block on ``block_date``);
    also returns the stops dropped while fitting the timeline."""
    items: list[PlanItem] = []
    if day.index == 0:
        items += _arrival_block(ctx, timing, day.date)
    t: int | None = None
    pos = ctx.hotel
    victims: list[Candidate] = []
    if day.schedule is not None:
        timeline, victims = _timeline_within(ctx, day)
        items += timeline.items
        t, pos = timeline.t, timeline.pos
    if day.date == block_date:
        items += _departure_block(ctx, timing, day.date, t, pos)
    elif t is not None:
        back = t + int(routing.travel_estimate(pos, ctx.hotel).minutes)
        items.append(_item(back, back, "hotel", ctx.texts["back_hotel"], area=ctx.hotel_label,
                           lat=ctx.hotel[0], lng=ctx.hotel[1]))
    theme, areas = _theme(ctx, items)
    plan_day = PlanDay(date=day.date, theme=theme, areas=areas, items=items, weather=day.weather)
    plan_day.holiday = day.holiday  # type: ignore[attr-defined]
    return plan_day, victims


def _layover_day(ctx: _Ctx) -> PlanDay:
    """The whole stay at the airport(s): arrival, a transfer when the departure airport
    differs, waiting time, departure (minutes after the arrival date's midnight)."""
    tx = ctx.texts
    inputs = ctx.inputs
    day = inputs.arrival_at.date()
    arrive = _minute_of(inputs.arrival_at, day)
    departs = _minute_of(inputs.departure_at, day)
    code_in = inputs.arrival_airport or inputs.destination
    code_out = inputs.departure_airport or inputs.destination
    lat, lng = ctx.airport_in if ctx.airport_in else (None, None)
    t = min(arrive + IMMIGRATION_MIN, departs)
    items = [_item(arrive, t, "arrival", tx["arrival"].format(airport=code_in), lat=lat, lng=lng,
                   map_url=links.google_maps_search(f"{code_in} airport", None, inputs.city))]
    if code_in != code_out and ctx.airport_in and ctx.airport_out:
        hop = routing.travel_estimate(ctx.airport_in, ctx.airport_out)
        items.append(_item(t, t + int(hop.minutes), "transit", tx["to_airport"].format(airport=code_out),
                           est_cost=ctx.defaults.airport_transfer(hop.distance_km),
                           cost_currency=ctx.defaults.currency, cost_estimated=True))
        t += int(hop.minutes)
    check_in = min(max(t, departs - AIRPORT_ARRIVE_BEFORE_MIN), departs)
    if check_in - t >= FREE_GAP_MIN:
        items.append(_item(t, check_in, "free", tx["layover_wait"].format(airport=code_out), at_airport=True))
    lat, lng = ctx.airport_out if ctx.airport_out else (None, None)
    items.append(_item(check_in, departs, "departure", _departure_title(ctx, code_out, departs), lat=lat, lng=lng,
                       map_url=links.google_maps_search(f"{code_out} airport", None, inputs.city)))
    weather = ctx.weather.get(day)
    plan_day = PlanDay(date=day, theme=tx["theme_layover"], items=items, weather=dict(weather) if weather else None)
    plan_day.holiday = ctx.holidays.get(day) if ctx.holidays else None  # type: ignore[attr-defined]
    return plan_day


# --------------------------------------------------------------------------- finalize & check

_CODE_BUILT_EXTRAS = ("links", "directions_url", "coords_approximate")
"""Item extras that only code may set; values from a composed plan are discarded."""
MAX_SOURCES = 60


def _point(item: PlanItem) -> Coord | None:
    return (item.lat, item.lng) if item.lat is not None and item.lng is not None else None


def _plan_hotel(plan: ItineraryPlan, inputs: PlanInputs) -> tuple[Coord | None, str | None]:
    """Hotel point and label: the traveller's hotel (given/geocoded) first, else the planner's
    ``hotel`` extra of the plan."""
    hotel = getattr(plan, "hotel", None)
    label = hotel.get("area") if isinstance(hotel, dict) else None
    label = str(label) if label else inputs.hotel_area
    if inputs.hotel_point is not None:
        return inputs.hotel_point, label
    if isinstance(hotel, dict):
        try:
            lat, lng = float(hotel["lat"]), float(hotel["lng"])
        except (KeyError, TypeError, ValueError):
            return None, label
        if -90 <= lat <= 90 and -180 <= lng <= 180:
            return (lat, lng), label
    return None, label


def _apply_candidate(item: PlanItem, candidate: Candidate, inputs: PlanInputs) -> None:
    """Facts of a candidate-backed item come from the candidate, never from the plan: its
    coordinates (``None`` when unlocated), area, verified source URLs and code-built links."""
    item.lat, item.lng = candidate.lat, candidate.lon
    if candidate.area:
        item.area = candidate.area
    item.source_urls = list(candidate.source_urls)
    item_links = links.place_links(candidate, inputs.city, inputs.country)
    item.map_url = item_links["google_maps"]
    item.links = item_links  # type: ignore[attr-defined]


def _plausible(point: Coord, inputs: PlanInputs, hotel: Coord | None) -> bool:
    refs = [inputs.center, *([hotel] if hotel else [])]
    return any(routing.distance_km(ref, point) <= GEOCODE_MAX_KM for ref in refs)


def _apply_code_links(
    item: PlanItem, inputs: PlanInputs, hotel: Coord | None, hotel_label: str | None, verified: set[str]
) -> None:
    """Items without a known candidate: locations and links rebuilt in code. Airports and the
    hotel get their known points; transit/free items none; other items keep plan coordinates
    only when plausible (flagged ``coords_approximate``) and get search links; source URLs
    survive only when verified."""
    item.source_urls = [u for u in item.source_urls if u in verified]
    city = inputs.city
    if item.kind in ("arrival", "departure"):
        code = (inputs.arrival_airport if item.kind == "arrival" else inputs.departure_airport) or inputs.destination
        airport = _airport_point(code)
        item.lat, item.lng = airport if airport else (None, None)
        item.map_url = links.google_maps_search(f"{code} airport", None, city)
    elif item.kind == "hotel":
        item.lat, item.lng = hotel if hotel else (None, None)
        item.map_url = (links.google_maps_point(*hotel) if hotel
                        else links.google_maps_search(item.title, hotel_label or item.area, city))
    elif item.kind in ("transit", "free"):
        item.lat = item.lng = None
        item.map_url = None
    else:
        point = _point(item)
        if point is not None and not _plausible(point, inputs, hotel):
            item.lat = item.lng = None
        elif point is not None:
            item.coords_approximate = True  # type: ignore[attr-defined]
        if item.kind == "meal":
            cuisine = canonical_cuisine(str(getattr(item, "cuisine", None) or ""))
            query = str(getattr(item, "search_query", None) or "") or item.title
            item_links = links.meal_search_links(query, item.area or hotel_label, city, inputs.country, cuisine)
            item.links = item_links  # type: ignore[attr-defined]
            item.map_url = item_links["google_maps"]
        else:
            item.map_url = links.google_maps_search(item.title, item.area, city)


def _clear_code_extras(item: PlanItem) -> None:
    extra = item.__pydantic_extra__
    if extra:
        for key in _CODE_BUILT_EXTRAS:
            extra.pop(key, None)


def _rebuild_sources(plan: ItineraryPlan, verified: set[str]) -> list[SourceRef]:
    """Sources = the items' (verified) source URLs, plus listed sources whose URL is verified."""
    refs = {ref.url: ref for ref in _sources(plan.days)}
    for ref in plan.sources:
        if ref.url in verified:
            refs.setdefault(ref.url, ref)
    return list(refs.values())[:MAX_SOURCES]


def finalize_plan(
    plan: ItineraryPlan,
    inputs: PlanInputs,
    candidates: Sequence[Candidate] = (),
    *,
    compute_budget: bool = True,
    allowed_urls: Iterable[str] = (),
) -> ItineraryPlan:
    """Recompute everything that is derived in code (returns a copy) — run it on every
    Claude-composed plan: nothing factual written by the LLM survives except text, times and
    the choice/order of items.

    * Items whose ``candidate_id`` is in ``candidates``: coordinates, area, source URLs, map
      and guide links from the candidate (LLM values are overwritten).
    * Other items: airport/hotel points and links rebuilt; transit/free items unlocated;
      LLM coordinates kept only within ~60 km of the centre/hotel and flagged
      ``coords_approximate``; map/search links built in code; LLM ``links`` /
      ``directions_url`` dropped; ``source_urls`` kept only when verified (a candidate URL or
      one of ``allowed_urls``, e.g. URLs seen in web-search results).
    * ``plan.sources`` rebuilt from the items' URLs plus listed sources with a verified URL.
    * ``travel_from_prev`` from the previous located item (day start = hotel; a ``transit``
      item carries the hop to the next located item, which then gets none), plus a per-leg
      ``directions_url``. Then ``est_day_cost`` (per person, local currency),
      ``route_metrics`` and — unless ``compute_budget`` is False — ``budget`` (may call the
      FX API).

    The hotel is ``inputs.hotel_point`` or the plan's ``hotel`` extra (keep the draft's
    ``hotel`` when composing).
    """
    plan = plan.model_copy(deep=True)
    by_id = {c.id: c for c in candidates}
    verified = {u for c in candidates for u in c.source_urls}
    verified |= {u for u in allowed_urls if isinstance(u, str) and u.startswith(("https://", "http://"))}
    hotel, hotel_label = _plan_hotel(plan, inputs)
    local = plan.currency_local or inputs.currency
    defaults = budget_mod.local_cost_defaults(inputs.country, local)
    for day in plan.days:
        pos: Coord | None = hotel
        covered = False
        for index, item in enumerate(day.items):
            _clear_code_extras(item)
            item.travel_from_prev = None
            candidate = by_id.get(item.candidate_id or "")
            if candidate is not None:
                _apply_candidate(item, candidate, inputs)
            else:
                _apply_code_links(item, inputs, hotel, hotel_label, verified)
            point = _point(item)
            if item.kind == "arrival":
                pos, covered = point or pos, False
                continue
            if item.kind == "transit":
                dest = next((p for later in day.items[index + 1:] if (p := _point(later)) is not None), None)
                if pos is not None and dest is not None:
                    item.travel_from_prev = routing.travel_estimate(pos, dest)
                    item.directions_url = links.google_maps_directions(  # type: ignore[attr-defined]
                        pos, dest, "transit"
                    )
                pos, covered = dest or pos, True
                continue
            if point is None:
                continue
            if not covered and pos is not None:
                hop = routing.travel_estimate(pos, point)
                if hop.distance_km >= 0.05:
                    item.travel_from_prev = hop
                    item.directions_url = links.google_maps_directions(  # type: ignore[attr-defined]
                        pos, point, hop.mode
                    )
            pos, covered = point, False
        day.est_day_cost = round(budget_mod.day_cost(day, local, defaults))
    plan.sources = _rebuild_sources(plan, verified)
    plan.route_metrics = routing.route_metrics(plan)
    if compute_budget:
        plan.budget = budget_mod.build_budget_summary(inputs, plan)
    return plan


def _parse_span(item: PlanItem) -> tuple[int, int] | None:
    try:
        return hhmm_to_minutes(item.start), hhmm_to_minutes(item.end)
    except (TypeError, ValueError):
        return None


def _day_spans(items: Sequence[PlanItem]) -> list[tuple[int, int]]:
    """Absolute ``(start, end)`` minutes from the day's midnight; times that jump back by
    more than 12 h are taken as past midnight (late arrivals, overnight departures). An
    unparsable time takes the previous item's end."""
    spans: list[tuple[int, int]] = []
    offset, previous = 0, None
    for item in items:
        parsed = _parse_span(item)
        if parsed is None:
            last = spans[-1][1] if spans else 0
            spans.append((last, last))
            continue
        start, end = parsed
        if previous is not None and start + offset < previous - 12 * 60:
            offset += 24 * 60
        start, end = start + offset, end + offset
        if end < start:
            end += 24 * 60
        spans.append((start, end))
        previous = start
    return spans


def _timing_warnings(plan: ItineraryPlan, inputs: PlanInputs, t: dict[str, str]) -> list[str]:
    """Overlaps, too little travel time, long hops, items before arrival / after the cut-off."""
    warnings: list[str] = []
    cutoff = inputs.departure_at - timedelta(minutes=AIRPORT_CUTOFF_MIN)
    for day in plan.days:
        label = _date_label(day.date, inputs.language)
        spans = _day_spans(day.items)
        warnings += [t["w_bad_time"].format(date=label, a=i.title) for i in day.items if _parse_span(i) is None]
        previous: int | None = None
        located: int | None = None  # index of the last item with coordinates (hop origin)
        for index, item in enumerate(day.items):
            start, end = spans[index]
            if previous is not None and start < spans[previous][1] - 1 and item.kind != "departure":
                warnings.append(t["w_overlap"].format(date=label, a=day.items[previous].title, b=item.title))
            hop = item.travel_from_prev
            if hop is not None and located is not None and item.kind != "transit":
                origin = day.items[located]
                if start - spans[located][1] + 5 < hop.minutes:
                    warnings.append(t["w_tight"].format(date=label, a=origin.title, b=item.title,
                                                        min=int(hop.minutes)))
                if hop.minutes > LONG_HOP_MIN and item.kind not in routing.CITY_HOP_EXCLUDED_KINDS:
                    warnings.append(t["w_long"].format(date=label, a=origin.title, b=item.title,
                                                       min=int(hop.minutes)))
            if _point(item) is not None or item.kind == "transit":
                located = index
            when = datetime.combine(day.date, time()) + timedelta(minutes=start)
            if item.kind not in ("arrival",) and when < inputs.arrival_at:
                warnings.append(t["w_before_arrival"].format(date=label, a=item.title))
            finish = datetime.combine(day.date, time()) + timedelta(minutes=end)
            in_town = item.kind not in ("arrival", "transit", "departure") and not getattr(item, "at_airport", False)
            if in_town and finish > cutoff:
                warnings.append(t["w_after_cutoff"].format(date=label, a=item.title,
                                                           time=cutoff.strftime("%H:%M")))
            previous = index
    return warnings


def _availability_warnings(
    plan: ItineraryPlan, inputs: PlanInputs, by_id: Mapping[str, Candidate],
    holidays: Mapping[date, str] | None, t: dict[str, str],
) -> list[str]:
    """Candidate-backed activities/meals on a closing day (weekday, holiday, holiday-shift
    rules) or outside their opening hours (activities may end :data:`LATE_TOLERANCE_MIN`
    after closing, meals must end by closing)."""
    if holidays is None:
        holidays = calendar_util.holidays_for_dates(inputs.country, [d.date for d in plan.days], inputs.language)
    warnings: list[str] = []
    for day in plan.days:
        label = _date_label(day.date, inputs.language)
        for item, (start, end) in zip(day.items, _day_spans(day.items), strict=True):
            candidate = by_id.get(item.candidate_id or "")
            if candidate is None or item.kind not in ("activity", "meal") or start >= 24 * 60:
                continue
            if calendar_util.is_closed(candidate, day.date, holidays):
                warnings.append(t["w_item_closed"].format(date=label, a=item.title))
                continue
            windows = calendar_util.open_windows(candidate, day.date, holidays)
            slack = 0 if item.kind == "meal" else LATE_TOLERANCE_MIN
            if windows and not any(o <= start and end <= c + slack for o, c in windows):
                hours = calendar_util.hours_label(candidate, day.date, holidays) or "?"
                warnings.append(t["w_item_hours"].format(date=label, a=item.title, hours=hours))
    return warnings


def _is_outdoor(item: PlanItem, by_id: Mapping[str, Candidate]) -> bool:
    candidate = by_id.get(item.candidate_id or "")
    return candidate.is_outdoor if candidate is not None else bool(getattr(item, "is_outdoor", False))


def _rain_warnings(
    plan: ItineraryPlan, inputs: PlanInputs, by_id: Mapping[str, Candidate],
    weather: Mapping[date, dict[str, Any]] | None, t: dict[str, str],
) -> list[str]:
    """Outdoor activities on days where rain is likely."""
    warnings: list[str] = []
    for day in plan.days:
        day_weather = (weather or {}).get(day.date) or day.weather
        if not weather_mod.is_wet(day_weather):
            continue
        outdoor = [i.title for i in day.items if i.kind == "activity" and _is_outdoor(i, by_id)]
        if outdoor:
            warnings.append(t["w_rain"].format(
                date=_date_label(day.date, inputs.language),
                prob=(day_weather or {}).get("precip_prob") or "?", names=_names(outdoor),
            ))
    return warnings


def check_plan(
    plan: ItineraryPlan,
    inputs: PlanInputs,
    candidates: Sequence[Candidate] = (),
    *,
    holidays: Mapping[date, str] | None = None,
    weather: Mapping[date, dict[str, Any]] | None = None,
) -> list[str]:
    """Route checker for any plan (rule-based or Claude-composed). Returns warnings in
    ``inputs.language``:

    * timing: overlapping items, too little time for the computed travel, items before arrival
      or (in town) after the airport cut-off, long in-city transfers, backtracking;
    * with ``candidates``: items whose ``candidate_id`` is known that fall on a closing day
      (weekday / public holiday / holiday-shift rules, ``holidays`` = ``{date: name}``, looked
      up with the ``holidays`` package when ``None``) or outside the opening hours;
    * outdoor activities on wet days (``weather`` = ``{date: day weather}``, default: each
      day's ``weather``).

    Run :func:`finalize_plan` first (it computes the travel hops this check relies on).
    """
    t = _texts(inputs.language)
    by_id = {c.id: c for c in candidates}
    warnings = _timing_warnings(plan, inputs, t)
    if by_id:
        warnings += _availability_warnings(plan, inputs, by_id, holidays, t)
    warnings += _rain_warnings(plan, inputs, by_id, weather, t)
    backtracks = plan.route_metrics.backtrack_hops if plan.route_metrics else 0
    if backtracks:
        warnings.append(t["w_backtrack"].format(n=backtracks))
    return list(dict.fromkeys(warnings))


# --------------------------------------------------------------------------- main entry


def _departure_day(timing: _Timing, inputs: PlanInputs) -> date:
    """Date of the departure block: the day the bags are collected (an overnight or
    early-morning flight's block starts the evening before), within the trip dates."""
    return min(max(timing.cutoff.date(), inputs.dates[0]), inputs.dates[-1])


def _summary(ctx: _Ctx, days: list[PlanDay]) -> tuple[str, str]:
    inputs = ctx.inputs
    acts = sum(1 for d in days for i in d.items if i.kind == "activity")
    meals = sum(1 for d in days for i in d.items if i.kind == "meal")
    title = ctx.texts["title"].format(city=inputs.city, n=len(days))
    summary = ctx.texts["summary"].format(
        city=inputs.city, n=len(days), acts=acts, meals=meals,
        first=_date_label(days[0].date, inputs.language),
        last=_date_label(days[-1].date, inputs.language),
    )
    return title, summary


def _sources(days: list[PlanDay]) -> list[SourceRef]:
    refs: dict[str, SourceRef] = {}
    for day in days:
        for item in day.items:
            for url in item.source_urls[:2]:
                refs.setdefault(url, SourceRef(title=item.title, url=url))
    return list(refs.values())[:MAX_SOURCES]


def _data_warnings(ctx: _Ctx, plan_days: list[PlanDay]) -> list[str]:
    t = ctx.texts
    warnings: list[str] = []
    scheduled = {i.candidate_id: i for d in plan_days for i in d.items if i.kind == "activity" and i.candidate_id}
    unverified = [i.title for i in scheduled.values() if not getattr(i, "hours_verified", True)]
    if unverified:
        warnings.append(t["w_hours"].format(names=_names(unverified)))
    for day in plan_days:
        holiday = getattr(day, "holiday", None)
        if holiday:
            warnings.append(t["w_holiday"].format(date=_date_label(day.date, ctx.inputs.language), name=holiday))
    if ctx.generic_meals:
        warnings.append(t["w_generic_meals"])
    return warnings


def _attributions(pool: Sequence[Candidate], weather: Mapping[date, Any]) -> list[str]:
    attributions = [OSM_ATTRIBUTION] if any(c.coord_source in ("osm", "nominatim") for c in pool) else []
    if any(c.coord_source == "nominatim" for c in pool):
        attributions.append(GEOCODE_ATTRIBUTION)
    if weather:
        attributions.append(weather_mod.ATTRIBUTION)
    return attributions


@dataclass
class _Outcome:
    """Scheduled plan days and what could not be scheduled."""

    days: list[PlanDay]
    unscheduled: list[Candidate]
    lost_forced: list[str]
    lost_other: list[str]
    summary: str | None = None


def _schedule(ctx: _Ctx, timing: _Timing, activities: list[Candidate], warnings: list[str], seed: int) -> _Outcome:
    """Select, cluster, order and fit the sights, then build the plan days (meals included).

    Stops dropped at any stage — including while fitting the final timeline (restaurant
    detours, airport cut-off) — are reported: must-visits in ``lost_forced``, other picked
    sights in ``lost_other``; everything unused is ``unscheduled``."""
    block_date = _departure_day(timing, ctx.inputs)
    days = _build_days(ctx, timing, block_date)
    total_slots = sum(d.slots for d in days)
    minutes_budget = sum(d.activity_minutes for d in days if d.slots > 0)
    pool_size = max(total_slots * POOL_FACTOR, total_slots + 4)
    pool_acts, overflow = _select(ctx, activities, pool_size, minutes_budget * POOL_FACTOR)
    if len(pool_acts) < total_slots:
        warnings.append(ctx.texts["w_sparse"].format(n=len(pool_acts), slots=total_slots))
    spare = _assign_days(ctx, pool_acts, days, seed) + overflow
    picked = {c.id for d in days for c in d.stops}
    dropped: list[Candidate] = []
    for day in days:
        dropped += _fit_day(ctx, day)
    pooled = {c.id for c in pool_acts}
    spare += dropped + [c for c in activities if c.id not in pooled]
    _rescue_forced(ctx, days, spare)
    _fill_days(ctx, days, spare)
    built =[_build_plan_day(ctx, day, timing, block_date) for day in days]
    victims = [c for _, lost in built for c in lost]
    dropped += victims
    spare += victims
    plan_days = [plan_day for plan_day, _ in built]
    while len(plan_days) > 1 and plan_days[-1].date > block_date and not plan_days[-1].items:
        plan_days.pop()  # a date after an overnight departure's block
    used = {c.id for d in days for c in d.stops}
    lost_forced = [c.name for c in activities if c.id in ctx.forced and c.id not in used]
    lost_forced += [c.name for c in ctx.restaurants if c.id in ctx.forced and c.id not in ctx.used_restaurants]
    lost_other = [c.name for c in dropped if c.id not in used and c.id not in ctx.forced and c.id in picked]
    return _Outcome(plan_days, [c for c in spare if c.id not in used], lost_forced, lost_other)


def _layover(ctx: _Ctx, activities: list[Candidate], warnings: list[str]) -> _Outcome:
    """Arrival and departure too close to leave the airport: a single airport day."""
    inputs = ctx.inputs
    hours = (inputs.departure_at - inputs.arrival_at).total_seconds() / 3600
    message = ctx.texts["w_layover"].format(hours=f"{hours:.1f}")
    warnings.append(message)
    lost_forced = [c.name for c in [*activities, *ctx.restaurants] if c.id in ctx.forced]
    return _Outcome([_layover_day(ctx)], list(activities), lost_forced, [], summary=message)


def plan_itinerary(
    inputs: PlanInputs,
    candidates: Sequence[Candidate],
    weather: Mapping[date, dict[str, Any]] | None = None,
    holidays: Mapping[date, str] | None = None,
    *,
    exclude_ids: Sequence[str] = (),
    seed: int = 0,
) -> ItineraryPlan:
    """Build a complete rule-based :class:`ItineraryPlan` (see module docstring).

    ``weather``: :func:`app.services.itinerary.weather.get_weather` output (optional).
    ``holidays``: ``{date: name}``; ``None`` → looked up with the ``holidays`` package.
    ``exclude_ids``: candidates to leave out (e.g. rule-based refinement). Deterministic:
    the same inputs (in any candidate order) give the same plan; only the budget summary
    calls the FX API (live rate or static fallback).

    Plan days run from the arrival date to the departure block's date (a date after an
    overnight departure's block is omitted; the departure item then names its date). When
    the stay is shorter than the airport round trip the plan is a single airport day
    (``layover=True``, no hotel).
    """
    texts = _texts(inputs.language)
    if holidays is None:
        holidays = calendar_util.holidays_for_dates(inputs.country, inputs.dates, inputs.language)
    weather = weather or {}
    defaults = budget_mod.local_cost_defaults(inputs.country, inputs.currency)
    pool, warnings = _filter_candidates(candidates, inputs, set(exclude_ids), texts)
    weights = normalized_weights(inputs)
    scores = {c.id: score_candidate(c, weights, inputs.companion) for c in pool}
    forced, missing = _match_must_visit(pool, inputs, scores)
    if missing:
        warnings.append(texts["w_must_missing"].format(names=_names(missing)))
    activities = [c for c in pool if not c.is_food]
    hotel = _hotel_point(inputs, activities, scores)
    ctx = _Ctx(
        inputs=inputs, texts=texts, pace=PACES[inputs.pace], weights=weights, holidays=holidays,
        weather=weather, defaults=defaults, caps=budget_mod.spending_caps(inputs, defaults),
        hotel=hotel.point, hotel_label=hotel.label,
        airport_in=_airport_point(inputs.arrival_airport), airport_out=_airport_point(inputs.departure_airport),
        scores=scores, forced=forced, restaurants=[c for c in pool if c.is_food],
        wishes=_cuisine_keys(inputs.food_wishlist), preferred=_cuisine_keys(inputs.preferred_cuisines),
    )
    timing = _timing(inputs, hotel.point, ctx.airport_in, ctx.airport_out)
    if timing.layover:
        outcome = _layover(ctx, activities, warnings)
    else:
        if not hotel.area_found:
            warnings.append(texts["w_hotel_area"].format(wanted=inputs.hotel_area, area=hotel.label))
        elif hotel.assumed:
            warnings.append(texts["w_hotel"].format(area=hotel.label))
        outcome = _schedule(ctx, timing, activities, warnings, seed)
    if outcome.lost_forced:
        warnings.append(texts["w_must_dropped"].format(names=_names(outcome.lost_forced)))
    if outcome.lost_other:
        warnings.append(texts["w_closed"].format(names=_names(outcome.lost_other)))

    title, summary = _summary(ctx, outcome.days)
    plan = ItineraryPlan(
        title=title,
        summary=outcome.summary or summary,
        destination=inputs.city,
        currency_local=inputs.currency,
        days=outcome.days,
        sources=_sources(outcome.days),
        generator=GENERATOR,
        cost_basis="per_person",
        hotel=None if timing.layover else {
            "lat": hotel.point[0], "lng": hotel.point[1], "area": hotel.label, "assumed": hotel.assumed,
        },
        layover=timing.layover,
        attributions=_attributions(pool, weather),
        unscheduled=[
            {"candidate_id": c.id, "name": c.name, "category": c.category}
            for c in sorted({c.id: c for c in outcome.unscheduled}.values(),
                            key=lambda c: (-scores[c.id], c.id))[:15]
        ],
    )
    plan = finalize_plan(plan, inputs, pool)
    warnings += _data_warnings(ctx, plan.days)
    over = budget_mod.over_budget_lines(plan.budget) if plan.budget else []
    if over:
        warnings.append(texts["w_over_budget"].format(lines=", ".join(over)))
    warnings += check_plan(plan, inputs, pool, holidays=holidays, weather=weather)
    plan.warnings = list(dict.fromkeys(warnings))
    logger.info("Rule-based plan: %d days, %d activities, %d warnings", len(plan.days),
                sum(1 for d in plan.days for i in d.items if i.kind == "activity"), len(plan.warnings))
    return plan
