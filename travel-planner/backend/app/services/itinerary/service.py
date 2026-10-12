"""Itinerary jobs (DESIGN §7, §10): create, generate in the background, refine, recover.

* :func:`create_itinerary` validates the request (trip, preferences, flight offer or manual
  times) and commits a ``pending`` :class:`~app.models.Itinerary` whose ``request`` holds the
  input snapshot (:class:`PlanInputs` incl. preferences and budget). Arrival = the outbound
  leg's arrival at the destination; departure = the inbound leg's departure from it (unknown
  inbound → the return date at 18:00 local, with a warning).
* :func:`start_generation` runs :func:`generate_itinerary` in a daemon thread (at most one
  run per itinerary in this process); the UI polls ``status``/``progress``.
* :func:`generate_itinerary` — with an Anthropic key (and ``use_ai``): Claude web research
  (OpenStreetMap fallback) → geocoding → weather/holidays → rule-based draft → Claude
  composition (the draft is kept when it fails); without a key: OpenStreetMap → rule-based
  planner (``generator="rule_based"``). Saves plan, status, usage and model, then notifies
  (best effort). Rows created by :func:`create_refinement` are revised instead: Claude
  :meth:`~ItineraryLLM.refine_itinerary`, or rule-based tweaks (pace, removed/added places,
  cuisines) and a re-plan without a key.
* :func:`recover_stale_itineraries` marks rows left ``pending``/``running`` by a previous
  process as errors (call at startup).

Only flight *times and airports* reach Claude; flight prices and other provider data never do.
Each step uses its own short DB session; the background job never raises.
"""

from __future__ import annotations

import copy
import importlib
import logging
import re
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

import anthropic
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import schemas
from app.db import session_scope
from app.models import FlightOffer, Itinerary, Trip
from app.schemas import STYLE_KEYS, ItineraryPlan
from app.services.itinerary import calendar_util
from app.services.itinerary.candidates import OverpassSource
from app.services.itinerary.claude_source import ClaudeWebResearchSource
from app.services.itinerary.geocode import geocode_candidates, resolve_plan_locations
from app.services.itinerary.llm import ItineraryLLM, ItineraryLLMError, LLMUsage
from app.services.itinerary.models import (
    CATEGORY_STYLES,
    Candidate,
    PlanInputs,
    build_plan_inputs,
    canonical_cuisine,
    normalize_text,
)
from app.services.itinerary.planner import plan_itinerary
from app.services.itinerary.weather import get_weather
from app.settings_store import get_secret, load_runtime_settings

logger = logging.getLogger(__name__)

__all__ = [
    "FeedbackChanges",
    "InvalidItineraryRequest",
    "ItineraryNotFoundError",
    "ItineraryServiceError",
    "TripNotFoundError",
    "create_itinerary",
    "create_refinement",
    "generate_itinerary",
    "is_generating",
    "parse_feedback",
    "recover_stale_itineraries",
    "start_generation",
]

ASSUMED_DEPARTURE = time(18, 0)
"""Departure time assumed when the inbound flight is unknown."""
MAX_STAY_DAYS = 30
MAX_FEEDBACK_CHARS = 4000
ERROR_CHARS = 500
ACTIVE_STATUSES = ("pending", "running")

PROGRESS: dict[str, dict[str, str]] = {
    "en": {
        "research": "Researching places…",
        "geocode": "Geocoding…",
        "weather": "Checking weather…",
        "routes": "Optimizing routes…",
        "write": "Writing itinerary…",
        "refine": "Revising itinerary…",
    },
    "ko": {
        "research": "장소 조사 중…",
        "geocode": "위치 확인 중…",
        "weather": "날씨 확인 중…",
        "routes": "동선 최적화 중…",
        "write": "일정 작성 중…",
        "refine": "일정 수정 중…",
    },
}

NOTICES: dict[str, dict[str, str]] = {
    "en": {
        "inbound_unknown": "Return flight time unknown — assumed a departure at 18:00 on {date}.",
        "research_failed": "Claude place research failed ({reason}) — OpenStreetMap data was used instead.",
        "research_empty": "Claude found no usable places — OpenStreetMap data was used instead.",
        "no_places": "No place data was found for {city}; the plan only covers arrival, hotel and departure.",
        "compose_failed": "Claude could not write the itinerary ({reason}) — showing the route-optimised draft.",
        "refine_failed": "Claude could not revise the itinerary ({reason}) — simple rule-based changes were "
                         "applied instead.",
        "refine_rules": "Feedback applied without AI: {changes}.",
        "refine_nothing": "Without AI the feedback could not be interpreted; the plan was re-arranged.",
        "chg_pace": "pace → {pace}",
        "chg_removed": "removed {names}",
        "chg_added": "must-visit {names}",
        "chg_more": "more {names}",
        "chg_no_food": "no {names}",
        "chg_more_food": "more {names}",
        "interrupted": "Interrupted: the application stopped before the itinerary was finished.",
        "failed": "Itinerary generation failed: {reason}",
    },
    "ko": {
        "inbound_unknown": "귀국편 시간을 알 수 없어 {date} 18:00 출발로 가정했습니다.",
        "research_failed": "Claude 장소 조사에 실패해({reason}) OpenStreetMap 데이터를 사용했습니다.",
        "research_empty": "Claude가 쓸 만한 장소를 찾지 못해 OpenStreetMap 데이터를 사용했습니다.",
        "no_places": "{city}의 장소 데이터를 찾지 못해 도착·호텔·출국 일정만 포함했습니다.",
        "compose_failed": "Claude가 일정을 작성하지 못해({reason}) 동선 최적화 초안을 표시합니다.",
        "refine_failed": "Claude가 일정을 수정하지 못해({reason}) 간단한 규칙 기반 수정을 적용했습니다.",
        "refine_rules": "AI 없이 반영한 요청: {changes}.",
        "refine_nothing": "AI 없이 요청을 해석하지 못해 일정을 다시 배치했습니다.",
        "chg_pace": "일정 강도 → {pace}",
        "chg_removed": "{names} 제외",
        "chg_added": "{names} 꼭 방문",
        "chg_more": "{names} 더 많이",
        "chg_no_food": "{names} 제외",
        "chg_more_food": "{names} 더 먹기",
        "interrupted": "중단됨: 일정 생성이 끝나기 전에 앱이 종료되었습니다.",
        "failed": "일정 생성 실패: {reason}",
    },
}
_PACE_NAMES = {
    "en": {"relaxed": "relaxed", "moderate": "moderate", "packed": "packed"},
    "ko": {"relaxed": "여유롭게", "moderate": "보통", "packed": "빡빡하게"},
}


def _texts(language: str, table: dict[str, dict[str, str]] = NOTICES) -> dict[str, str]:
    return table.get(language, table["en"])


# --------------------------------------------------------------------------- errors


class ItineraryServiceError(Exception):
    """Base error of this module; ``str(exc)`` is a user-facing message."""


class TripNotFoundError(ItineraryServiceError, LookupError):
    """The trip does not exist (HTTP 404)."""


class ItineraryNotFoundError(ItineraryServiceError, LookupError):
    """The itinerary does not exist (HTTP 404)."""


class InvalidItineraryRequest(ItineraryServiceError, ValueError):
    """The request cannot be planned (missing preferences, bad offer/times; HTTP 400/422)."""


# --------------------------------------------------------------------------- create


@dataclass
class _Flight:
    """Arrival/departure resolved from a flight offer or manual times."""

    arrival_at: datetime
    departure_at: datetime
    arrival_airport: str | None = None
    departure_airport: str | None = None
    offer_id: int | None = None
    price_total: float | None = None
    currency: str | None = None
    summary: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def _parse_local(value: str | datetime, what: str) -> datetime:
    """Destination-local naive datetime (minutes) from an ISO string."""
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).strip())
    except ValueError as exc:
        raise InvalidItineraryRequest(f"invalid {what} time {value!r}") from exc
    return parsed.replace(tzinfo=None, second=0, microsecond=0)


def _local_iso(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M")


def _leg_summary(leg: dict[str, Any] | None) -> dict[str, Any] | None:
    """Times/airports/flight numbers of a leg (the only flight data kept for the plan)."""
    if not leg:
        return None
    keys = ("departure_airport", "arrival_airport", "departure_time", "arrival_time", "airlines",
            "flight_numbers", "stops")
    return {k: leg.get(k) for k in keys if k in leg}


def _offer_flight(db: Session, trip: Trip, offer_id: int, language: str) -> _Flight:
    offer = db.get(FlightOffer, offer_id)
    if offer is None or offer.trip_id != trip.id:
        raise InvalidItineraryRequest(f"flight offer {offer_id} not found for this trip")
    arrival = _parse_local(offer.arrival_time, "arrival")
    inbound = offer.inbound or None
    warnings: list[str] = []
    inbound_assumed = not (inbound and inbound.get("departure_time"))
    if not inbound_assumed:
        departure = _parse_local(inbound["departure_time"], "departure")
        departure_airport = inbound.get("departure_airport") or offer.arrival_airport
    else:
        return_date = offer.return_date or trip.return_date
        if return_date is None:
            raise InvalidItineraryRequest(
                "this flight has no return; enter the departure from the destination manually"
            )
        departure = datetime.combine(return_date, ASSUMED_DEPARTURE)
        departure_airport = offer.arrival_airport
        warnings.append(_texts(language)["inbound_unknown"].format(date=return_date.isoformat()))
    return _Flight(
        arrival_at=arrival, departure_at=departure,
        arrival_airport=offer.arrival_airport, departure_airport=departure_airport,
        offer_id=offer.id, price_total=offer.price_total, currency=offer.currency,
        summary={"provider": offer.provider, "outbound": _leg_summary(offer.outbound) or {
            "departure_airport": offer.departure_airport, "arrival_airport": offer.arrival_airport,
            "departure_time": offer.departure_time, "arrival_time": offer.arrival_time,
        }, "inbound": _leg_summary(inbound), "inbound_assumed": inbound_assumed},
        warnings=warnings,
    )


def _resolve_flight(db: Session, trip: Trip, body: schemas.ItineraryCreate, language: str) -> _Flight:
    """Times from the offer (``offer_id``) and/or the manual fields (manual times win)."""
    flight: _Flight | None = None
    if body.offer_id is not None:
        flight = _offer_flight(db, trip, body.offer_id, language)
    if body.arrival_at is not None and body.departure_at is not None:
        arrival = _parse_local(body.arrival_at, "arrival")
        departure = _parse_local(body.departure_at, "departure")
        if flight is None:
            flight = _Flight(arrival_at=arrival, departure_at=departure, summary={"manual": True})
        else:
            flight.arrival_at, flight.departure_at, flight.warnings = arrival, departure, []
            flight.summary["manual_times"] = True
    if flight is None:  # ItineraryCreate already requires one of the two
        raise InvalidItineraryRequest("provide offer_id or both arrival_at and departure_at")
    if flight.departure_at <= flight.arrival_at:
        raise InvalidItineraryRequest("the departure must be after the arrival")
    if (flight.departure_at - flight.arrival_at) > timedelta(days=MAX_STAY_DAYS):
        raise InvalidItineraryRequest(f"stays longer than {MAX_STAY_DAYS} days are not supported")
    return flight


def create_itinerary(db: Session, trip_id: int, body: schemas.ItineraryCreate) -> Itinerary:
    """Validate the request and commit a ``pending`` itinerary (run it with
    :func:`start_generation`).

    Raises :class:`TripNotFoundError`, or :class:`InvalidItineraryRequest` when the trip has
    no travel preferences, the offer does not belong to the trip, the flight has no return
    and no manual departure was given, the times are inconsistent or the destination is
    unknown."""
    trip = db.get(Trip, trip_id)
    if trip is None:
        raise TripNotFoundError(f"trip {trip_id} not found")
    preferences = trip.preferences
    if preferences is None:
        raise InvalidItineraryRequest(
            "set the trip's travel preferences (style and budget) before generating an itinerary"
        )
    runtime = load_runtime_settings(db)
    language = runtime.language
    flight = _resolve_flight(db, trip, body, language)
    hotel_area = body.hotel_area or preferences.hotel_area
    try:
        inputs = build_plan_inputs(
            trip, preferences,
            arrival_at=flight.arrival_at, departure_at=flight.departure_at,
            arrival_airport=flight.arrival_airport, departure_airport=flight.departure_airport,
            hotel_area=hotel_area, notes=body.notes, language=language,
            flight_price_total=flight.price_total, flight_currency=flight.currency,
        )
    except ValueError as exc:
        raise InvalidItineraryRequest(f"cannot plan this trip: {exc}") from exc
    request = {
        "source": "offer" if flight.offer_id is not None else "manual",
        "use_ai": body.use_ai,
        "notes": body.notes,
        "hotel_area": hotel_area,
        "language": language,
        "flight": flight.summary,
        "inputs": inputs.model_dump(mode="json"),
        "warnings": flight.warnings,
    }
    row = Itinerary(
        trip_id=trip.id, status="pending", offer_id=flight.offer_id,
        arrival_at=_local_iso(flight.arrival_at), departure_at=_local_iso(flight.departure_at),
        arrival_airport=flight.arrival_airport, departure_airport=flight.departure_airport,
        hotel_area=hotel_area, generator="rule_based", request=request,
    )
    db.add(row)
    db.commit()
    logger.info("Itinerary %s created for trip %s (%s, %s → %s)", row.id, trip.id, request["source"],
                row.arrival_at, row.departure_at)
    return row


def create_refinement(db: Session, itinerary_id: int, feedback: str, *, start: bool = True) -> Itinerary:
    """Commit a new ``pending`` itinerary that revises ``itinerary_id`` with ``feedback``
    (``parent_id`` set) and, when ``start``, run it in the background.

    Raises :class:`ItineraryNotFoundError`, or :class:`InvalidItineraryRequest` when the
    parent is not finished or the feedback is empty."""
    parent = db.get(Itinerary, itinerary_id)
    if parent is None:
        raise ItineraryNotFoundError(f"itinerary {itinerary_id} not found")
    if parent.status != "done" or not parent.plan:
        raise InvalidItineraryRequest("only a finished itinerary can be refined")
    text = (feedback or "").strip()[:MAX_FEEDBACK_CHARS]
    if not text:
        raise InvalidItineraryRequest("feedback must not be empty")
    request = copy.deepcopy(dict(parent.request or {}))
    request["refine_of"] = parent.id
    row = Itinerary(
        trip_id=parent.trip_id, status="pending", offer_id=parent.offer_id,
        arrival_at=parent.arrival_at, departure_at=parent.departure_at,
        arrival_airport=parent.arrival_airport, departure_airport=parent.departure_airport,
        hotel_area=parent.hotel_area, generator=parent.generator, request=request,
        parent_id=parent.id, feedback=text,
    )
    db.add(row)
    db.commit()
    logger.info("Itinerary %s created as a refinement of %s", row.id, parent.id)
    if start:
        start_generation(row.id)
    return row


# --------------------------------------------------------------------------- background runs


class _Guard:
    """Itineraries being generated in this process."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ids: set[int] = set()

    def claim(self, itinerary_id: int) -> bool:
        with self._lock:
            if itinerary_id in self._ids:
                return False
            self._ids.add(itinerary_id)
            return True

    def release(self, itinerary_id: int) -> None:
        with self._lock:
            self._ids.discard(itinerary_id)

    def claimed(self) -> set[int]:
        with self._lock:
            return set(self._ids)


_guard = _Guard()


def is_generating(itinerary_id: int) -> bool:
    """True while this process generates ``itinerary_id``."""
    return itinerary_id in _guard.claimed()


def start_generation(itinerary_id: int, *, client: Any | None = None) -> bool:
    """Run :func:`generate_itinerary` in a daemon thread; False when it is already running.

    ``client`` optionally replaces the Anthropic client (tests)."""
    if not _guard.claim(itinerary_id):
        logger.info("Itinerary %s is already being generated", itinerary_id)
        return False
    try:
        threading.Thread(
            target=_run_and_release, args=(itinerary_id, client), name=f"itinerary-{itinerary_id}", daemon=True,
        ).start()
    except BaseException:
        _guard.release(itinerary_id)
        raise
    return True


def _run_and_release(itinerary_id: int, client: Any | None) -> None:
    try:
        generate_itinerary(itinerary_id, client=client)
    finally:
        _guard.release(itinerary_id)


def recover_stale_itineraries() -> int:
    """Mark ``pending``/``running`` itineraries that no thread of this process owns as
    errors (a previous process stopped mid-run). Call at startup; returns the count."""
    busy = _guard.claimed()
    with session_scope() as db:
        rows = db.scalars(select(Itinerary).where(Itinerary.status.in_(ACTIVE_STATUSES))).all()
        stale = [row for row in rows if row.id not in busy]
        for row in stale:
            language = str((row.request or {}).get("language") or "en")
            row.status, row.progress = "error", None
            row.error = _texts(language)["interrupted"]
    if stale:
        logger.warning("Marked %d interrupted itinerar%s as error", len(stale), "y" if len(stale) == 1 else "ies")
    return len(stale)


# --------------------------------------------------------------------------- the pipeline


@dataclass
class _Job:
    """Snapshot of an itinerary row taken when its run starts."""

    id: int
    trip_id: int
    request: dict[str, Any]
    parent_id: int | None
    feedback: str | None

    @property
    def inputs(self) -> PlanInputs:
        return PlanInputs.model_validate(self.request["inputs"])

    @property
    def language(self) -> str:
        return str(self.request.get("language") or (self.request.get("inputs") or {}).get("language") or "en")


@dataclass
class _Result:
    plan: ItineraryPlan
    inputs: PlanInputs
    candidates: list[Candidate]
    usage: LLMUsage
    generator: str
    model: str | None


def generate_itinerary(itinerary_id: int, *, client: Any | None = None) -> None:
    """Generate (or, for a refinement row, revise) ``itinerary_id`` synchronously.

    Never raises: failures end as ``status="error"`` with a user-facing ``error``. Sends an
    "itinerary ready/failed" notification (best effort). ``client`` optionally replaces the
    Anthropic client (tests)."""
    try:
        job = _start_job(itinerary_id)
    except Exception:  # noqa: BLE001 - e.g. the database is unavailable
        logger.exception("Itinerary %s could not be started", itinerary_id)
        return
    if job is None:
        return
    try:
        result = _refine(job, client) if job.parent_id is not None and job.feedback else _generate(job, client)
        _finish(job, result)
    except Exception as exc:  # noqa: BLE001 - background job: record, never raise
        logger.exception("Itinerary %s failed", itinerary_id)
        _fail(job, exc)
    _notify(itinerary_id)


def _start_job(itinerary_id: int) -> _Job | None:
    with session_scope() as db:
        row = db.get(Itinerary, itinerary_id)
        if row is None:
            logger.warning("Itinerary %s vanished before generation", itinerary_id)
            return None
        if row.status == "done":
            logger.info("Itinerary %s is already done", itinerary_id)
            return None
        row.status, row.error = "running", None
        return _Job(id=row.id, trip_id=row.trip_id, request=copy.deepcopy(dict(row.request or {})),
                    parent_id=row.parent_id, feedback=row.feedback)


def _progress(job: _Job, step: str) -> None:
    message = _texts(job.language, PROGRESS)[step]
    with session_scope() as db:
        row = db.get(Itinerary, job.id)
        if row is not None:
            row.status, row.progress = "running", message
    logger.info("Itinerary %s: %s", job.id, message)


def _make_client(api_key: str) -> anthropic.Anthropic:
    """The Anthropic client used for itinerary calls (the key is never logged)."""
    return anthropic.Anthropic(api_key=api_key, max_retries=2)


def _llm_for(job: _Job, client: Any | None) -> ItineraryLLM | None:
    """Claude for this job, or ``None`` (``use_ai`` off or no Anthropic key)."""
    if not job.request.get("use_ai", True):
        return None
    with session_scope() as db:
        runtime = load_runtime_settings(db)
        key = get_secret("anthropic", db)
    if not key:
        return None
    return ItineraryLLM(
        client=client if client is not None else _make_client(key),
        model=runtime.llm_model, effort=runtime.llm_effort, language=runtime.language,
    )


def _osm_candidates(inputs: PlanInputs) -> list[Candidate]:
    with session_scope() as db:
        return OverpassSource().fetch(inputs, db)


def _research(job: _Job, inputs: PlanInputs, llm: ItineraryLLM | None, usage: LLMUsage,
              warnings: list[str]) -> tuple[list[Candidate], ItineraryLLM | None]:
    """Candidates from Claude (when available) with the OpenStreetMap fallback; returns the
    LLM to use for later steps (``None`` after a fatal error such as an invalid key)."""
    texts = _texts(job.language)
    _progress(job, "research")
    candidates: list[Candidate] = []
    if llm is not None:
        source = ClaudeWebResearchSource(llm)
        candidates = source.fetch(inputs)
        usage.merge(source.last_usage)
        failure = source.last_exception
        if failure is not None:
            warnings.append(texts["research_failed"].format(reason=failure))
            if failure.fatal:
                llm = None
        elif not candidates:
            warnings.append(texts["research_empty"])
    if not candidates:
        candidates = _osm_candidates(inputs)
    return candidates, llm


def _locate(job: _Job, inputs: PlanInputs, candidates: list[Candidate]) -> tuple[PlanInputs, list[Candidate]]:
    """City centre / hotel area and candidate coordinates (Nominatim, cached)."""
    _progress(job, "geocode")
    with session_scope() as db:
        inputs = resolve_plan_locations(db, inputs)
        located = geocode_candidates(db, candidates, inputs.city, inputs.country, inputs.center)
    return inputs, located


def _conditions(job: _Job, inputs: PlanInputs) -> tuple[dict[date, dict[str, Any]], dict[date, str]]:
    _progress(job, "weather")
    weather = get_weather(inputs.center_lat, inputs.center_lon, inputs.dates, inputs.timezone,
                          language=inputs.language)
    holidays = calendar_util.holidays_for_dates(inputs.country, inputs.dates, inputs.language)
    return weather, holidays


def _generate(job: _Job, client: Any | None) -> _Result:
    """New itinerary: research → geocode → weather/holidays → draft → (Claude) compose."""
    inputs = job.inputs
    texts = _texts(job.language)
    warnings = list(job.request.get("warnings") or [])
    usage = LLMUsage()
    llm = _llm_for(job, client)
    model = llm.model if llm is not None else None
    candidates, llm = _research(job, inputs, llm, usage, warnings)
    inputs, candidates = _locate(job, inputs, candidates)
    if not candidates:
        warnings.append(texts["no_places"].format(city=inputs.city))
    weather, holidays = _conditions(job, inputs)
    _progress(job, "routes")
    plan = plan_itinerary(inputs, candidates, weather=weather, holidays=holidays)
    generator = "rule_based"
    if llm is not None and candidates and not getattr(plan, "layover", False):
        _progress(job, "write")
        try:
            plan, compose_usage = llm.compose_itinerary(inputs, plan, candidates, weather=weather,
                                                        holidays=holidays)
            usage.merge(compose_usage)
            generator = "claude"
        except ItineraryLLMError as exc:
            usage.merge(exc.usage)
            warnings.append(texts["compose_failed"].format(reason=exc))
    plan.generator = generator  # type: ignore[assignment]
    plan.warnings = list(dict.fromkeys([*warnings, *plan.warnings]))
    return _Result(plan, inputs, candidates, usage, generator, model if usage.requests else None)


# --------------------------------------------------------------------------- refinement


@dataclass
class FeedbackChanges:
    """What the rule-based refinement understood from free-text feedback."""

    pace: str | None = None
    exclude_ids: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    must_visit: list[str] = field(default_factory=list)
    avoid_foods: list[str] = field(default_factory=list)
    more_foods: list[str] = field(default_factory=list)
    more_styles: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.pace or self.exclude_ids or self.must_visit or self.avoid_foods
                    or self.more_foods or self.more_styles)


_RELAXED_RE = re.compile(
    r"relax|slow|less packed|less busy|fewer|leisure|easy|여유|느긋|천천히|쉬엄|덜 빡|적게|줄여", re.I)
_PACKED_RE = re.compile(
    r"packed|busy|more (?:places|stops|sights|activities)|as much as|빡빡|알차게|더 많이|많이 보", re.I)
_NEGATIVE_RE = re.compile(
    r"\b(?:no|not|skip|remove|drop|avoid|without|don'?t|instead of|less)\b|빼|제외|말고|싫|안 가|가지 않|없이|대신|줄여",
    re.I)
_POSITIVE_RE = re.compile(r"\b(?:add|include|visit|want|must|more|love)\b|추가|넣어|가고 싶|꼭|더|좋아", re.I)
_CLAUSE_RE = re.compile(r"[.,;!?\n]+|\bbut\b|\band\b|그리고|하지만|그런데")
_CATEGORY_WORDS: dict[str, tuple[str, ...]] = {
    "museum": ("museum", "gallery", "박물관", "미술관", "갤러리"),
    "historic": ("temple", "shrine", "castle", "palace", "historic", "사찰", "신사", "궁궐", "고궁", "성곽"),
    "shopping": ("shopping", "mall", "쇼핑", "백화점"),
    "nightlife": ("nightlife", "bar", "club", "밤문화", "술집", "클럽"),
    "nature": ("nature", "hike", "hiking", "자연", "등산"),
    "park": ("park", "garden", "공원", "정원"),
    "theme_park": ("theme park", "amusement", "zoo", "aquarium", "테마파크", "놀이공원", "동물원", "수족관"),
    "viewpoint": ("viewpoint", "observation", "전망대", "야경"),
    "market": ("market", "시장"),
}


def _has_word(clause: str, word: str) -> bool:
    if word.isascii():
        return re.search(rf"\b{re.escape(word)}s?\b", clause, re.I) is not None
    return word in clause


def parse_feedback(feedback: str, candidates: list[Candidate]) -> FeedbackChanges:
    """Rule-based reading of free-text feedback (English/Korean): pace, places to remove or
    add (by name), cuisines to avoid or want more of, categories to drop or favour."""
    changes = FeedbackChanges()
    if _RELAXED_RE.search(feedback):
        changes.pace = "relaxed"
    elif _PACKED_RE.search(feedback):
        changes.pace = "packed"
    for clause in (c.strip() for c in _CLAUSE_RE.split(feedback)):
        if not clause:
            continue
        negative = bool(_NEGATIVE_RE.search(clause))
        positive = bool(_POSITIVE_RE.search(clause)) and not negative
        if not (negative or positive):
            continue
        folded = normalize_text(clause)
        for candidate in candidates:
            if any(len(term) >= 2 and term in folded for term in candidate.search_terms()):
                if negative and candidate.id not in changes.exclude_ids:
                    changes.exclude_ids.append(candidate.id)
                    changes.removed.append(candidate.name)
                elif positive and candidate.name not in changes.must_visit:
                    changes.must_visit.append(candidate.name)
        cuisine = canonical_cuisine(clause)
        if cuisine:
            target = changes.avoid_foods if negative else changes.more_foods
            if cuisine not in target:
                target.append(cuisine)
        for category, words in _CATEGORY_WORDS.items():
            if any(_has_word(clause, w) for w in words):
                matching = [c for c in candidates if c.category == category]
                if negative:
                    for c in matching:
                        if c.id not in changes.exclude_ids:
                            changes.exclude_ids.append(c.id)
                            changes.removed.append(c.name)
                elif category not in changes.more_styles:
                    changes.more_styles.append(category)
    return changes


def _apply_changes(inputs: PlanInputs, changes: FeedbackChanges) -> PlanInputs:
    """Inputs with the understood feedback applied (pace, must-visit, foods, style weights)."""
    update: dict[str, Any] = {}
    if changes.pace:
        update["pace"] = changes.pace
    if changes.must_visit:
        update["must_visit"] = list(dict.fromkeys([*inputs.must_visit, *changes.must_visit]))
    if changes.avoid_foods:
        update["avoid_foods"] = list(dict.fromkeys([*inputs.avoid_foods, *changes.avoid_foods]))
    if changes.more_foods:
        update["food_wishlist"] = list(dict.fromkeys([*changes.more_foods, *inputs.food_wishlist]))
    if changes.more_styles:
        weights = dict(inputs.style_weights)
        peak = max(weights.values(), default=0.0) or (3.0 if inputs.style_mode == "score" else 10.0)
        for category in changes.more_styles:
            for style in CATEGORY_STYLES.get(category, ())[:1]:
                if style in STYLE_KEYS:
                    weights[style] = max(weights.get(style, 0.0), peak)
        update["style_weights"] = weights
    return inputs.model_copy(update=update) if update else inputs


def _describe_changes(changes: FeedbackChanges, language: str) -> str:
    t = _texts(language)
    parts: list[str] = []
    if changes.pace:
        parts.append(t["chg_pace"].format(pace=_PACE_NAMES.get(language, _PACE_NAMES["en"])[changes.pace]))
    if changes.removed:
        parts.append(t["chg_removed"].format(names=", ".join(dict.fromkeys(changes.removed[:6]))))
    if changes.must_visit:
        parts.append(t["chg_added"].format(names=", ".join(changes.must_visit[:6])))
    if changes.more_styles:
        parts.append(t["chg_more"].format(names=", ".join(changes.more_styles)))
    if changes.avoid_foods:
        parts.append(t["chg_no_food"].format(names=", ".join(changes.avoid_foods)))
    if changes.more_foods:
        parts.append(t["chg_more_food"].format(names=", ".join(changes.more_foods)))
    return "; ".join(parts)


def _rule_based_refine(
    job: _Job, inputs: PlanInputs, candidates: list[Candidate],
    weather: dict[date, dict[str, Any]], holidays: dict[date, str],
) -> tuple[ItineraryPlan, PlanInputs]:
    """Re-plan with the feedback the rules understand (a different arrangement otherwise);
    returns the plan and the revised inputs (kept for further refinements)."""
    texts = _texts(job.language)
    changes = parse_feedback(job.feedback or "", candidates)
    revised = _apply_changes(inputs, changes)
    _progress(job, "routes")
    plan = plan_itinerary(revised, candidates, weather=weather, holidays=holidays,
                          exclude_ids=changes.exclude_ids, seed=1 if changes.empty else 0)
    note = (texts["refine_nothing"] if changes.empty
            else texts["refine_rules"].format(changes=_describe_changes(changes, job.language)))
    plan.warnings = [note, *plan.warnings]
    return plan, revised


def _parent_plan(job: _Job) -> ItineraryPlan:
    with session_scope() as db:
        parent = db.get(Itinerary, job.parent_id)
        if parent is None or not parent.plan:
            raise ItineraryServiceError("the itinerary being refined no longer exists")
        return ItineraryPlan.model_validate(copy.deepcopy(dict(parent.plan)))


def _stored_candidates(job: _Job) -> list[Candidate]:
    stored: list[Candidate] = []
    for raw in job.request.get("candidates") or []:
        try:
            stored.append(Candidate.model_validate(raw))
        except ValueError as exc:
            logger.info("Skipping a stored candidate of itinerary %s: %s", job.id, exc)
    return stored


def _refine(job: _Job, client: Any | None) -> _Result:
    """Refinement row: Claude revises the parent plan, else rule-based tweaks + re-plan."""
    texts = _texts(job.language)
    inputs = job.inputs
    parent_plan = _parent_plan(job)
    candidates = _stored_candidates(job)
    warnings = list(job.request.get("warnings") or [])
    usage = LLMUsage()
    llm = _llm_for(job, client)
    model = llm.model if llm is not None else None
    weather = {d.date: d.weather for d in parent_plan.days if d.weather}
    holidays = calendar_util.holidays_for_dates(inputs.country, inputs.dates, inputs.language)
    plan: ItineraryPlan | None = None
    generator = "rule_based"
    if llm is not None:
        _progress(job, "refine")
        try:
            plan, refine_usage = llm.refine_itinerary(inputs, parent_plan, job.feedback or "", candidates,
                                                      weather=weather, holidays=holidays)
            usage.merge(refine_usage)
            generator = "claude"
        except ItineraryLLMError as exc:
            usage.merge(exc.usage)
            warnings.append(texts["refine_failed"].format(reason=exc))
    if plan is None:
        if not candidates:
            candidates, _ = _research(job, inputs, None, usage, warnings)
            inputs, candidates = _locate(job, inputs, candidates)
        plan, inputs = _rule_based_refine(job, inputs, candidates, weather, holidays)
    plan.generator = generator  # type: ignore[assignment]
    plan.warnings = list(dict.fromkeys([*warnings, *plan.warnings]))
    return _Result(plan, inputs, candidates, usage, generator, model if usage.requests else None)


# --------------------------------------------------------------------------- persistence & notification


def _finish(job: _Job, result: _Result) -> None:
    with session_scope() as db:
        row = db.get(Itinerary, job.id)
        if row is None:
            logger.warning("Itinerary %s vanished before it was saved", job.id)
            return
        request = copy.deepcopy(dict(row.request or {}))
        request["inputs"] = result.inputs.model_dump(mode="json")
        request["candidates"] = [c.model_dump(mode="json", exclude_defaults=True) for c in result.candidates]
        row.request = request
        row.plan = result.plan.model_dump(mode="json")
        row.status, row.error, row.progress = "done", None, None
        row.generator = result.generator
        row.model = result.model
        row.usage = result.usage.to_dict()
    logger.info("Itinerary %s done (%s, %d days, ~$%.3f)", job.id, result.generator, len(result.plan.days),
                result.usage.est_usd)


def _fail(job: _Job, exc: Exception) -> None:
    if isinstance(exc, ItineraryServiceError | ItineraryLLMError):
        reason = str(exc)
    else:
        reason = f"{type(exc).__name__}: {exc}"
    message = _texts(job.language)["failed"].format(reason=reason)[:ERROR_CHARS]
    try:
        with session_scope() as db:
            row = db.get(Itinerary, job.id)
            if row is not None:
                row.status, row.error, row.progress = "error", message, None
    except Exception:  # noqa: BLE001 - nothing more can be done
        logger.exception("Could not record the failure of itinerary %s", job.id)


def _notify(itinerary_id: int) -> None:
    """Best-effort "itinerary ready/failed" notification (dispatcher imported lazily)."""
    try:
        dispatcher = importlib.import_module("app.services.notifications.dispatcher")
    except ImportError as exc:
        logger.info("Notification dispatcher unavailable (%s)", exc)
        return
    try:
        with session_scope() as db:
            row = db.get(Itinerary, itinerary_id)
            trip = db.get(Trip, row.trip_id) if row is not None else None
            if row is None or trip is None:
                return
            ready = getattr(dispatcher, "notify_itinerary_ready", None)
            if callable(ready):
                ready(db, trip, row)
                return
            dispatcher.notify(db, kind="itinerary", message=_message(db, trip, row), trip_id=trip.id)
    except Exception:  # noqa: BLE001 - notifications never fail the itinerary
        logger.exception("Notifying about itinerary %s failed", itinerary_id)


def _message(db: Session, trip: Trip, row: Itinerary) -> Any:
    """Notification content via ``formatter.format_itinerary_ready`` (or a plain fallback)."""
    from app.services.events import NotificationMessage

    language = load_runtime_settings(db).language
    try:
        formatter = importlib.import_module("app.services.notifications.formatter")
        return formatter.format_itinerary_ready(trip, row, language)
    except (ImportError, AttributeError):
        title = (row.plan or {}).get("title") or trip.name
        return NotificationMessage(title=f"Itinerary {row.status}", body=str(title))
