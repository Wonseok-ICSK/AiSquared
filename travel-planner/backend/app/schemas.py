"""Pydantic API schemas (DESIGN §6/§7). The frontend mirrors these as TypeScript types.

Conventions:
* ``*Create`` / ``*In`` validate request bodies; ``*Update`` = all fields optional (partial).
* Every response schema (``*Out`` and the models nested in them) derives from
  :class:`OutModel` (``from_attributes``), so it validates from ORM rows, provider
  dataclasses or dicts alike. Datetimes typed ``UTCDateTime`` serialize as
  ``"YYYY-MM-DDTHH:MM:SSZ"``.
* Flight times and itinerary times are airport/destination-local strings, never converted.
* Money is ``float`` in major units and always travels with a ``currency`` code.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from typing import Annotated, Any, Literal, get_args

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    StringConstraints,
    field_validator,
    model_validator,
)

from app.config import get_settings
from app.core.airports import airports_for
from app.timeutil import to_utc_iso, validate_hhmm

_Date = date  # alias for models that have a field literally named ``date``

# --------------------------------------------------------------------------- enums

Cabin = Literal["economy", "premium_economy", "business", "first"]
TripType = Literal["round_trip", "one_way"]
PriceBasis = Literal["per_person", "total"]
ScheduleMode = Literal["times", "interval"]
TripStatus = Literal["ok", "partial", "error"]
RunTrigger = Literal["schedule", "manual"]
RunStatus = Literal["running", "ok", "partial", "error"]
ProviderRunStatus = Literal["ok", "error", "skipped"]
AlertKind = Literal["below_target", "price_drop", "new_low"]
NotificationKind = Literal["price_alert", "itinerary", "system"]
NotificationChannel = Literal["inapp", "desktop", "telegram", "email", "ntfy", "kakao"]
Language = Literal["en", "ko"]
StyleMode = Literal["percent", "score"]
Companion = Literal["solo", "couple", "family"]
Pace = Literal["relaxed", "moderate", "packed"]
ItineraryStatus = Literal["pending", "running", "done", "error"]
ItineraryGenerator = Literal["claude", "rule_based"]
PlanItemKind = Literal["arrival", "transit", "hotel", "activity", "meal", "free", "departure"]
SessionStatus = Literal["none", "active", "expired"]
LlmEffort = Literal["low", "medium", "high", "xhigh", "max"]
FlexDays = Literal[0, 1, 3, 7]

STYLE_KEYS: list[str] = [
    "sightseeing",
    "food",
    "relaxation",
    "hotel_resort",
    "shopping",
    "nightlife",
    "nature",
    "photography",
    "culture",
    "local_experience",
]
CABINS: list[str] = list(get_args(Cabin))
TRIP_TYPES: list[str] = list(get_args(TripType))
PRICE_BASES: list[str] = list(get_args(PriceBasis))
SCHEDULE_MODES: list[str] = list(get_args(ScheduleMode))
ALERT_KINDS: list[str] = list(get_args(AlertKind))
NOTIFICATION_KINDS: list[str] = list(get_args(NotificationKind))
NOTIFICATION_CHANNELS: list[str] = list(get_args(NotificationChannel))
STYLE_MODES: list[str] = list(get_args(StyleMode))
COMPANIONS: list[str] = list(get_args(Companion))
PACES: list[str] = list(get_args(Pace))
LLM_EFFORTS: list[str] = list(get_args(LlmEffort))
FLEX_DAYS: list[int] = list(get_args(FlexDays))

MAX_SCHEDULE_TIMES = 6
DEFAULT_SCHEDULE_TIMES = ["08:30", "17:30"]
DEFAULT_STYLE_WEIGHTS: dict[str, float] = {key: 10.0 for key in STYLE_KEYS}
"""Default weights for ``style_mode == "percent"`` (sum 100)."""
DEFAULT_STYLE_SCORES: dict[str, float] = {key: 3.0 for key in STYLE_KEYS}
"""Default weights for ``style_mode == "score"`` (0-5 each)."""
STYLE_PERCENT_MAX = 100.0
STYLE_SCORE_MAX = 5.0
BUDGET_UNLIMITED = -1.0


def default_currency() -> str:
    """``DEFAULT_CURRENCY`` from settings: default for new trips and budgets (DESIGN §8)."""
    return get_settings().default_currency

# --------------------------------------------------------------------------- reusable types


def _upper_strip(value: Any) -> Any:
    return value.strip().upper() if isinstance(value, str) else value


def _blank_to_none(value: Any) -> Any:
    return None if isinstance(value, str) and not value.strip() else value


def _validate_local_iso(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError as exc:
        raise ValueError(f"expected local ISO datetime 'YYYY-MM-DDTHH:MM', got {value!r}") from exc
    if parsed.tzinfo is not None:
        raise ValueError("local datetimes must not carry a timezone offset")
    return parsed.strftime("%Y-%m-%dT%H:%M")


UTCDateTime = Annotated[
    datetime, PlainSerializer(to_utc_iso, return_type=str, when_used="json-unless-none")
]
"""Naive-UTC datetime rendered as ``YYYY-MM-DDTHH:MM:SSZ`` in JSON."""

IataCode = Annotated[
    str, BeforeValidator(_upper_strip), StringConstraints(pattern=r"^[A-Z]{3}$")
]
"""3-letter IATA airport or metro/city code, upper-cased (``icn`` -> ``ICN``)."""

CurrencyCode = Annotated[
    str, BeforeValidator(_upper_strip), StringConstraints(pattern=r"^[A-Z]{3}$")
]
"""ISO-4217 currency code, upper-cased."""

HHMM = Annotated[str, AfterValidator(validate_hhmm)]
"""24h ``HH:MM`` time string."""

OptionalHHMM = Annotated[HHMM | None, BeforeValidator(_blank_to_none)]
"""``HH:MM`` or null (blank string treated as null)."""

LocalDateTimeStr = Annotated[str, AfterValidator(_validate_local_iso)]
"""Airport/destination-local naive datetime ``YYYY-MM-DDTHH:MM`` (no offset)."""

def _check_budget(value: float) -> float:
    if value < 0 and value != BUDGET_UNLIMITED:
        raise ValueError("budget must be positive (cap), or 0 / -1 (unlimited)")
    return value


NonNegativeMoney = Annotated[float, Field(ge=0)]
BudgetAmount = Annotated[float, Field(ge=-1), AfterValidator(_check_budget)]
"""Budget value: positive = cap, ``0`` or ``-1`` = unlimited (other negatives rejected)."""

ShortText = Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)]
LongText = Annotated[str, StringConstraints(max_length=4000)]


class OutModel(BaseModel):
    """Base for all response schemas: validates from dicts, ORM rows or dataclasses."""

    model_config = ConfigDict(from_attributes=True)


# --------------------------------------------------------------------------- misc


class HealthOut(OutModel):
    """``GET /api/health`` response."""

    status: Literal["ok"] = "ok"
    version: str


class MessageOut(OutModel):
    """Generic acknowledgement for action endpoints (login started, session deleted, ...)."""

    ok: bool = True
    message: str = ""


# --------------------------------------------------------------------------- trips


def auto_trip_name(origin: str, destination: str) -> str:
    """Name given to a trip created without one: ``"ICN → NRT"``."""
    return f"{origin} → {destination}"


def _check_trip_consistency(trip: TripCreate) -> None:
    if trip.trip_type == "round_trip":
        if trip.return_date is None:
            raise ValueError("return_date is required for a round trip")
        if trip.return_date < trip.depart_date:
            raise ValueError("return_date must be on or after depart_date")
    if set(airports_for(trip.origin)) & set(airports_for(trip.destination)):
        raise ValueError("origin and destination must differ (incl. airports of the same metro)")
    if trip.schedule_mode == "interval" and trip.schedule_interval_hours is None:
        raise ValueError("schedule_interval_hours is required when schedule_mode is 'interval'")
    if trip.schedule_mode == "times" and not trip.schedule_times:
        raise ValueError("schedule_times needs at least one HH:MM when schedule_mode is 'times'")


def _dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


class TripCreate(BaseModel):
    """Body of ``POST /api/trips``. An empty ``name`` becomes ``"ORIGIN → DESTINATION"``."""

    name: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)] = ""
    origin: IataCode
    destination: IataCode
    trip_type: TripType = "round_trip"
    depart_date: date
    return_date: date | None = None
    adults: int = Field(1, ge=1, le=2)
    cabin: Cabin = "economy"
    direct_only: bool = False
    max_stops: int | None = Field(None, ge=0, le=3)

    outbound_depart_from: OptionalHHMM = None
    outbound_depart_to: OptionalHHMM = None
    outbound_arrive_from: OptionalHHMM = None
    outbound_arrive_to: OptionalHHMM = None
    return_depart_from: OptionalHHMM = None
    return_depart_to: OptionalHHMM = None
    return_arrive_from: OptionalHHMM = None
    return_arrive_to: OptionalHHMM = None

    max_price: NonNegativeMoney | None = None
    target_price: NonNegativeMoney | None = None
    price_basis: PriceBasis = "per_person"
    currency: CurrencyCode = Field(default_factory=default_currency, validate_default=True)
    """Defaults to ``DEFAULT_CURRENCY`` (settings)."""

    flex_depart_days: FlexDays = 0
    flex_return_days: FlexDays = 0
    include_nearby_airports: bool = False
    alt_destinations: list[IataCode] = Field(default_factory=list, max_length=10)
    providers: list[str] = Field(default_factory=list)

    schedule_mode: ScheduleMode = "times"
    schedule_times: list[HHMM] = Field(
        default_factory=lambda: list(DEFAULT_SCHEDULE_TIMES), max_length=MAX_SCHEDULE_TIMES
    )
    schedule_interval_hours: float | None = Field(None, ge=1, le=168)

    active: bool = True
    notes: LongText | None = None

    @field_validator("alt_destinations", "providers")
    @classmethod
    def _unique(cls, values: list[str]) -> list[str]:
        return _dedupe(values)

    @field_validator("schedule_times")
    @classmethod
    def _unique_sorted_times(cls, values: list[str]) -> list[str]:
        return sorted(set(values))

    @model_validator(mode="after")
    def _consistency(self) -> TripCreate:
        if self.trip_type == "one_way":
            self.return_date = None
            self.flex_return_days = 0
            self.return_depart_from = self.return_depart_to = None
            self.return_arrive_from = self.return_arrive_to = None
        _check_trip_consistency(self)
        origin_airports = set(airports_for(self.origin))
        destination_airports = set(airports_for(self.destination))
        self.alt_destinations = [
            code
            for code in self.alt_destinations
            # drop codes that overlap the origin (X -> X searches) or are already covered
            # by the destination (``NRT`` when the destination is ``TYO``)
            if not set(airports_for(code)) & origin_airports
            and not set(airports_for(code)) <= destination_airports
        ]
        if not self.name:
            self.name = auto_trip_name(self.origin, self.destination)
        return self


class TripUpdate(BaseModel):
    """Body of ``PUT /api/trips/{id}``: any subset of ``TripCreate`` fields.

    Field-level rules apply here; cross-field rules are enforced by :meth:`merged_with`,
    which the API layer must call with the stored trip's values.
    """

    name: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)] | None = None
    origin: IataCode | None = None
    destination: IataCode | None = None
    trip_type: TripType | None = None
    depart_date: date | None = None
    return_date: date | None = None
    adults: int | None = Field(None, ge=1, le=2)
    cabin: Cabin | None = None
    direct_only: bool | None = None
    max_stops: int | None = Field(None, ge=0, le=3)

    outbound_depart_from: OptionalHHMM = None
    outbound_depart_to: OptionalHHMM = None
    outbound_arrive_from: OptionalHHMM = None
    outbound_arrive_to: OptionalHHMM = None
    return_depart_from: OptionalHHMM = None
    return_depart_to: OptionalHHMM = None
    return_arrive_from: OptionalHHMM = None
    return_arrive_to: OptionalHHMM = None

    max_price: NonNegativeMoney | None = None
    target_price: NonNegativeMoney | None = None
    price_basis: PriceBasis | None = None
    currency: CurrencyCode | None = None

    flex_depart_days: FlexDays | None = None
    flex_return_days: FlexDays | None = None
    include_nearby_airports: bool | None = None
    alt_destinations: list[IataCode] | None = Field(None, max_length=10)
    providers: list[str] | None = None

    schedule_mode: ScheduleMode | None = None
    schedule_times: list[HHMM] | None = Field(None, max_length=MAX_SCHEDULE_TIMES)
    schedule_interval_hours: float | None = Field(None, ge=1, le=168)

    active: bool | None = None
    notes: LongText | None = None

    def merged_with(self, current: Mapping[str, Any]) -> TripCreate:
        """Overlay the explicitly-set fields on ``current`` and validate the full trip.

        A stored auto-generated name (``"ICN → NRT"``) is regenerated for the new route
        unless this update sets ``name``. Raises ``pydantic.ValidationError`` (map to
        HTTP 422) when the result is invalid.
        """
        data = {name: current[name] for name in TripCreate.model_fields if name in current}
        if "name" not in self.model_fields_set and data.get("name") == auto_trip_name(
            current.get("origin", ""), current.get("destination", "")
        ):
            data.pop("name")
        data.update(self.model_dump(exclude_unset=True))
        return TripCreate.model_validate(data)


class TripOut(OutModel):
    """A trip as returned by the API, plus computed summary fields filled by the API layer."""

    id: int
    name: str
    origin: str
    destination: str
    trip_type: TripType
    depart_date: date
    return_date: date | None
    adults: int
    cabin: Cabin
    direct_only: bool
    max_stops: int | None

    outbound_depart_from: str | None
    outbound_depart_to: str | None
    outbound_arrive_from: str | None
    outbound_arrive_to: str | None
    return_depart_from: str | None
    return_depart_to: str | None
    return_arrive_from: str | None
    return_arrive_to: str | None

    max_price: float | None
    target_price: float | None
    price_basis: PriceBasis
    currency: str

    flex_depart_days: int
    flex_return_days: int
    include_nearby_airports: bool
    alt_destinations: list[str]
    providers: list[str]

    schedule_mode: ScheduleMode
    schedule_times: list[str]
    schedule_interval_hours: float | None

    active: bool
    notes: str | None
    last_checked_at: UTCDateTime | None
    last_status: TripStatus | None
    last_error: str | None
    created_at: UTCDateTime
    updated_at: UTCDateTime

    latest_min_price: float | None = None
    """Lowest matching ``price_total`` of the latest finished run."""
    latest_min_price_basis_value: float | None = None
    """Same offer's price in the trip's ``price_basis`` (per person or total)."""
    lowest_ever: float | None = None
    """Lowest matching price ever observed, in the trip's ``price_basis``."""
    next_run_at: UTCDateTime | None = None
    """Next scheduled check (from the scheduler), if any."""
    unread_alerts: int = 0
    """Unread notifications for this trip."""


# --------------------------------------------------------------------------- runs & offers


class ProviderRunStat(OutModel):
    """Per-provider outcome inside ``SearchRunOut.provider_stats``."""

    model_config = ConfigDict(extra="allow")

    status: ProviderRunStatus
    offers: int = 0
    error: str | None = None
    duration_s: float = 0.0
    queries: int = 0


class SearchRunOut(OutModel):
    """One monitoring execution (poll ``GET /api/runs/{id}`` while ``status == 'running'``)."""

    id: int
    trip_id: int
    trigger: RunTrigger
    started_at: UTCDateTime
    finished_at: UTCDateTime | None
    status: RunStatus
    error: str | None
    combinations_total: int
    combinations_searched: int
    offers_found: int
    offers_matching: int
    min_price_total: float | None
    min_price_per_person: float | None
    currency: str
    best_offer_id: int | None
    provider_stats: dict[str, ProviderRunStat]
    log: list[str]


class FlightSegmentOut(OutModel):
    """One flown segment of a leg (mirrors ``providers.flights.base.FlightSegment``)."""

    carrier_code: str | None = None
    carrier_name: str | None = None
    flight_number: str | None = None
    departure_airport: str
    arrival_airport: str
    departure_time: str
    arrival_time: str
    duration_minutes: int | None = None
    operating_carrier: str | None = None
    aircraft: str | None = None


class FlightLegOut(OutModel):
    """Outbound or inbound journey (mirrors ``providers.flights.base.FlightLeg``)."""

    departure_airport: str
    arrival_airport: str
    departure_time: str
    arrival_time: str
    duration_minutes: int | None = None
    stops: int = 0
    airlines: list[str] = Field(default_factory=list)
    flight_numbers: list[str] = Field(default_factory=list)
    segments: list[FlightSegmentOut] = Field(default_factory=list)
    layovers: list[str] = Field(default_factory=list)


class FlightOfferOut(OutModel):
    """A stored offer observation (one row of price history)."""

    id: int
    trip_id: int
    run_id: int
    searched_at: UTCDateTime
    provider: str
    seller: str | None
    offer_key: str
    origin: str
    destination: str
    depart_date: date
    return_date: date | None
    airline: str | None
    flight_number: str | None
    departure_airport: str
    arrival_airport: str
    departure_time: str
    arrival_time: str
    duration_minutes: int | None
    stops: int
    outbound: FlightLegOut
    inbound: FlightLegOut | None
    cabin: Cabin
    fare_type: str | None
    baggage: str | None
    price_total: float
    price_per_person: float
    currency: str
    previous_price: float | None
    lowest_price: float | None
    booking_url: str | None
    source_url: str | None
    matches_filters: bool


BookingHandoff = Literal["prefilled", "homepage", "unknown"]
"""How Google's "Continue" opens the airline's site: its flight list or fare page with the
route, date and passengers filled in, its home page (details entered by hand), or not
checked yet."""
BookingPage = Literal["google_booking", "google_search", "other", "none"]


class BookingTicketOut(OutModel):
    """One ticket the traveller buys: one leg, or both legs on one booking."""

    legs: list[Literal["outbound", "inbound"]]
    carrier_code: str | None
    airline: str | None
    flight_numbers: list[str]
    origin: str
    destination: str
    depart_date: date
    return_date: date | None
    """Return date when this ticket covers both legs."""
    departure_time: str
    arrival_time: str
    handoff: BookingHandoff
    airline_url: str | None


class BookingPlanOut(OutModel):
    """``GET /offers/{id}/booking-plan``: what booking an offer involves."""

    offer: FlightOfferOut
    booking_url: str | None
    """Where "Book" goes: normally Google's "Booking options" page with both flights."""
    booking_page: BookingPage
    adults: int
    round_trip: bool
    return_known: bool
    separate_tickets: bool | None
    """Outbound and return flown by different airlines, so normally two separate bookings
    (Google's page confirms); ``None`` while the return flight is unknown."""
    tickets: list[BookingTicketOut]
    resolved_total: float | None
    """Round-trip total Google showed when the return flight was looked up, when it differs
    from the offer's price (fares moved since the search)."""


class PricePoint(OutModel):
    """Lowest matching prices of one finished run."""

    run_id: int
    checked_at: UTCDateTime
    min_total: float | None = None
    min_per_person: float | None = None
    by_provider: dict[str, float] = Field(default_factory=dict)
    """provider key -> lowest per-person price in that run."""


class PriceHistoryOut(OutModel):
    """``GET /api/trips/{id}/price-history``."""

    trip_id: int
    currency: str
    price_basis: PriceBasis
    points: list[PricePoint] = Field(default_factory=list)


class OfferHistoryPoint(OutModel):
    """One observation of a specific itinerary (same ``offer_key``)."""

    offer_id: int
    run_id: int
    searched_at: UTCDateTime
    provider: str
    price_total: float
    price_per_person: float


class OfferHistoryOut(OutModel):
    """``GET /api/trips/{id}/offers/{offer_id}/history``."""

    trip_id: int
    offer_key: str
    currency: str
    lowest_per_person: float | None = None
    points: list[OfferHistoryPoint] = Field(default_factory=list)


# --------------------------------------------------------------------------- alerts & notifications


class AlertRuleCreate(BaseModel):
    """Body of ``POST /api/trips/{id}/alerts``."""

    kind: AlertKind
    enabled: bool = True
    threshold: NonNegativeMoney | None = None
    """below_target only; null = use the trip's ``target_price``."""
    drop_percent: float | None = Field(None, gt=0, le=100)
    """price_drop only, e.g. ``10.0``."""
    price_basis: PriceBasis = "per_person"

    @model_validator(mode="after")
    def _kind_fields(self) -> AlertRuleCreate:
        if self.kind == "price_drop" and self.drop_percent is None:
            raise ValueError("drop_percent is required for a price_drop rule")
        return self


class AlertRuleUpdate(BaseModel):
    """Body of ``PUT /api/alerts/{id}`` (partial). Setting ``enabled`` true re-arms the rule.

    Field-level rules apply here; cross-field rules (``price_drop`` needs ``drop_percent``)
    are enforced by :meth:`merged_with`, which the API layer must call with the stored
    rule's values. ``armed`` is not part of the merged rule: apply it from this object.
    """

    kind: AlertKind | None = None
    enabled: bool | None = None
    threshold: NonNegativeMoney | None = None
    drop_percent: float | None = Field(None, gt=0, le=100)
    price_basis: PriceBasis | None = None
    armed: bool | None = None

    def merged_with(self, current: Mapping[str, Any]) -> AlertRuleCreate:
        """Overlay the explicitly-set fields on ``current`` and validate the full rule.

        Raises ``pydantic.ValidationError`` (map to HTTP 422) when the result is invalid,
        e.g. switching a ``below_target`` rule to ``price_drop`` without a ``drop_percent``.
        """
        data = {name: current[name] for name in AlertRuleCreate.model_fields if name in current}
        data.update(self.model_dump(exclude_unset=True, exclude={"armed"}))
        return AlertRuleCreate.model_validate(data)


class AlertRuleOut(OutModel):
    """An alert rule with its trigger state."""

    id: int
    trip_id: int
    kind: AlertKind
    enabled: bool
    threshold: float | None
    drop_percent: float | None
    price_basis: PriceBasis
    armed: bool
    last_triggered_at: UTCDateTime | None
    last_triggered_price: float | None
    created_at: UTCDateTime
    updated_at: UTCDateTime


class NotificationOut(OutModel):
    """An in-app notification with per-channel delivery results."""

    id: int
    trip_id: int | None
    alert_rule_id: int | None
    kind: NotificationKind
    title: str
    body: str
    url: str | None
    data: dict[str, Any]
    read_at: UTCDateTime | None
    channel_results: dict[str, str]
    """channel -> ``"sent"`` | ``"skipped"`` | ``"failed: <reason>"``."""
    created_at: UTCDateTime


class UnreadCountOut(OutModel):
    """``GET /api/notifications/unread-count``."""

    count: int


class NotificationTestIn(BaseModel):
    """Body of ``POST /api/settings/notifications/test``."""

    channel: NotificationChannel


class NotificationTestOut(OutModel):
    """Result of a channel test send."""

    channel: NotificationChannel
    result: str
    """``"sent"`` | ``"skipped"`` | ``"failed: <reason>"``."""


# --------------------------------------------------------------------------- preferences


class PreferencesIn(BaseModel):
    """Body of ``PUT /api/trips/{id}/preferences`` (travel style + budget).

    ``style_weights``: unknown keys are rejected, missing style keys default to 0. When
    omitted, the default for ``style_mode`` applies (:data:`DEFAULT_STYLE_WEIGHTS` for
    percent, :data:`DEFAULT_STYLE_SCORES` for score).
    Budgets: null = not specified, ``0``/``-1`` = unlimited, positive = cap.
    """

    style_mode: StyleMode = "percent"
    style_weights: dict[str, float] = Field(default_factory=lambda: dict(DEFAULT_STYLE_WEIGHTS))
    companion: Companion = "solo"
    pace: Pace = "moderate"
    food_wishlist: list[ShortText] = Field(default_factory=list, max_length=50)
    preferred_cuisines: list[ShortText] = Field(default_factory=list, max_length=50)
    avoid_foods: list[ShortText] = Field(default_factory=list, max_length=50)
    restaurant_budget_min: NonNegativeMoney | None = None
    restaurant_budget_max: NonNegativeMoney | None = None
    restaurant_budget_currency: CurrencyCode | None = None
    hotel_area: ShortText | None = None
    must_visit: list[ShortText] = Field(default_factory=list, max_length=50)
    avoid: list[ShortText] = Field(default_factory=list, max_length=50)
    notes: LongText | None = None

    budget_basis: PriceBasis = "total"
    budget_currency: CurrencyCode = Field(default_factory=default_currency, validate_default=True)
    """Defaults to ``DEFAULT_CURRENCY`` (settings)."""
    budget_total: BudgetAmount
    budget_flight: BudgetAmount
    budget_hotel: BudgetAmount | None = None
    budget_food: BudgetAmount | None = None
    budget_transport: BudgetAmount | None = None
    budget_activities: BudgetAmount | None = None
    budget_shopping: BudgetAmount | None = None
    budget_other: BudgetAmount | None = None

    @field_validator("style_weights")
    @classmethod
    def _known_style_keys(cls, weights: dict[str, float]) -> dict[str, float]:
        unknown = sorted(set(weights) - set(STYLE_KEYS))
        if unknown:
            raise ValueError(f"unknown style keys: {', '.join(unknown)}")
        return {key: float(weights.get(key, 0.0)) for key in STYLE_KEYS}

    @model_validator(mode="after")
    def _style_ranges(self) -> PreferencesIn:
        if "style_weights" not in self.model_fields_set and self.style_mode == "score":
            self.style_weights = dict(DEFAULT_STYLE_SCORES)
        values = self.style_weights.values()
        if self.style_mode == "percent":
            if any(v < 0 or v > STYLE_PERCENT_MAX for v in values):
                raise ValueError("percent style weights must be between 0 and 100")
            if sum(values) > STYLE_PERCENT_MAX + 1e-6:
                raise ValueError("percent style weights must sum to 100 or less")
        elif any(v < 0 or v > STYLE_SCORE_MAX for v in values):
            raise ValueError("score style weights must be between 0 and 5")
        low, high = self.restaurant_budget_min, self.restaurant_budget_max
        if low is not None and high is not None and low > high:
            raise ValueError("restaurant_budget_min must not exceed restaurant_budget_max")
        return self


class PreferencesOut(OutModel):
    """Stored travel preferences of a trip."""

    trip_id: int
    style_mode: StyleMode
    style_weights: dict[str, float]
    companion: Companion
    pace: Pace
    food_wishlist: list[str]
    preferred_cuisines: list[str]
    avoid_foods: list[str]
    restaurant_budget_min: float | None
    restaurant_budget_max: float | None
    restaurant_budget_currency: str | None
    hotel_area: str | None
    must_visit: list[str]
    avoid: list[str]
    notes: str | None
    budget_basis: PriceBasis
    budget_currency: str
    budget_total: float
    budget_flight: float
    budget_hotel: float | None
    budget_food: float | None
    budget_transport: float | None
    budget_activities: float | None
    budget_shopping: float | None
    budget_other: float | None
    created_at: UTCDateTime
    updated_at: UTCDateTime


# --------------------------------------------------------------------------- itinerary


class ItineraryCreate(BaseModel):
    """Body of ``POST /api/trips/{id}/itineraries``: an ``offer_id`` or manual arrival/departure."""

    offer_id: int | None = None
    arrival_at: LocalDateTimeStr | None = None
    """Destination-local arrival ``YYYY-MM-DDTHH:MM`` (manual mode)."""
    departure_at: LocalDateTimeStr | None = None
    """Destination-local departure ``YYYY-MM-DDTHH:MM`` (manual mode)."""
    hotel_area: ShortText | None = None
    notes: LongText | None = None
    use_ai: bool = True

    @model_validator(mode="after")
    def _source(self) -> ItineraryCreate:
        manual = self.arrival_at is not None or self.departure_at is not None
        if self.offer_id is None and not manual:
            raise ValueError("provide offer_id or both arrival_at and departure_at")
        if manual and (self.arrival_at is None or self.departure_at is None):
            raise ValueError("arrival_at and departure_at must be given together")
        if manual and self.departure_at <= self.arrival_at:  # type: ignore[operator]
            raise ValueError("departure_at must be after arrival_at")
        return self


class ItineraryRefine(BaseModel):
    """Body of ``POST /api/itineraries/{id}/refine``."""

    feedback: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]


class TravelHop(OutModel):
    """Travel from the previous plan item."""

    mode: str
    """e.g. ``walk`` | ``transit`` | ``taxi``."""
    minutes: float
    distance_km: float


class PlanItem(OutModel):
    """One timed entry of a plan day (times are destination-local ``HH:MM``)."""

    model_config = ConfigDict(extra="allow")

    start: str
    end: str
    kind: PlanItemKind
    title: str
    area: str | None = None
    lat: float | None = None
    lng: float | None = None
    description: str | None = None
    est_cost: float | None = None
    cost_currency: str | None = None
    travel_from_prev: TravelHop | None = None
    tips: str | None = None
    map_url: str | None = None
    source_urls: list[str] = Field(default_factory=list)
    candidate_id: str | None = None


class PlanDay(OutModel):
    """One day of the plan."""

    model_config = ConfigDict(extra="allow")

    date: _Date
    theme: str = ""
    areas: list[str] = Field(default_factory=list)
    items: list[PlanItem] = Field(default_factory=list)
    est_day_cost: float | None = None
    weather: dict[str, Any] | None = None
    """Forecast/climate summary, e.g. ``{summary, temp_min_c, temp_max_c, precip_mm}``."""


class BudgetLine(OutModel):
    """Budget vs estimate for one category (amounts in ``BudgetSummary.currency``)."""

    category: str
    budget: float | None = None
    estimated: float | None = None
    unlimited: bool = False


class BudgetSummary(OutModel):
    """Budget comparison in the trip's budget currency."""

    model_config = ConfigDict(extra="allow")

    currency: str
    lines: list[BudgetLine] = Field(default_factory=list)
    total_budget: float | None = None
    total_estimated: float | None = None
    within_budget: bool | None = None
    notes: str | None = None


class SourceRef(OutModel):
    """A cited web source."""

    title: str
    url: str


class RouteMetrics(OutModel):
    """Route quality metrics computed by the route checker."""

    total_travel_minutes: float = 0.0
    avg_hop_km: float = 0.0
    max_hop_km: float = 0.0
    backtrack_hops: int = 0


class ItineraryPlan(OutModel):
    """Canonical ``ItineraryPlan`` JSON (DESIGN §10) stored in ``Itinerary.plan``."""

    model_config = ConfigDict(extra="allow")

    title: str
    summary: str = ""
    destination: str
    currency_local: str
    days: list[PlanDay] = Field(default_factory=list)
    budget: BudgetSummary | None = None
    warnings: list[str] = Field(default_factory=list)
    sources: list[SourceRef] = Field(default_factory=list)
    generator: ItineraryGenerator = "rule_based"
    route_metrics: RouteMetrics = Field(default_factory=RouteMetrics)


class ItineraryOut(OutModel):
    """An itinerary job/result (poll while ``status`` is pending/running)."""

    id: int
    trip_id: int
    status: ItineraryStatus
    error: str | None
    progress: str | None
    offer_id: int | None
    arrival_at: str | None
    departure_at: str | None
    arrival_airport: str | None
    departure_airport: str | None
    hotel_area: str | None
    generator: ItineraryGenerator
    model: str | None
    request: dict[str, Any]
    plan: ItineraryPlan | None
    parent_id: int | None
    feedback: str | None
    usage: dict[str, Any]
    """``{input_tokens, output_tokens, web_searches, est_usd, ...}``."""
    created_at: UTCDateTime
    updated_at: UTCDateTime


# --------------------------------------------------------------------------- providers, airports, scheduler


class ProviderInfo(OutModel):
    """A flight provider and its readiness (``GET /api/providers``)."""

    key: str
    display_name: str
    description: str = ""
    requires_api_key: bool = False
    configured: bool
    enabled_by_default: bool = True
    """Provider class default (``mock`` and opt-in providers are False)."""
    enabled: bool = True
    """Used for trips whose ``providers`` list is empty (runtime ``default_providers`` if
    set, else ``enabled_by_default``); a disabled provider runs only when a trip lists it."""
    uses_browser: bool = False
    supports_login: bool = False
    session_status: SessionStatus | None = None
    """Login session state for providers that support login, else null."""
    message: str = ""


class ProviderTestOut(OutModel):
    """State of a provider test search (``POST /api/providers/{key}/test``)."""

    key: str
    status: Literal["running", "ok", "error"]
    message: str = ""
    started_at: UTCDateTime | None = None
    finished_at: UTCDateTime | None = None
    offers_found: int | None = None
    min_price: float | None = None
    currency: str | None = None


class AirportOut(OutModel):
    """Airport or metro (multi-airport city) code for autocomplete."""

    code: str
    name: str
    city: str
    city_ko: str | None = None
    """Korean city name (``도쿄``) for popular cities, else ``None``."""
    city_code: str | None = None
    """Metro/city code the airport belongs to (``NRT`` -> ``TYO``), if any."""
    country: str
    lat: float
    lon: float
    is_metro: bool = False


class SchedulerJobOut(OutModel):
    """A scheduled monitoring job (``GET /api/scheduler/jobs``)."""

    job_id: str
    trip_id: int | None = None
    trip_name: str | None = None
    next_run_at: UTCDateTime | None = None
    trigger: str = ""
    """Human-readable schedule, e.g. ``"daily at 08:30, 17:30 (Asia/Seoul)"``."""


# --------------------------------------------------------------------------- runtime settings


class SecretState(OutModel):
    """Redacted secret: whether a value is set and where it comes from."""

    is_set: bool
    source: Literal["ui", "env"] | None = None


class DesktopSettingsOut(OutModel):
    """Windows toast notifications."""

    enabled: bool


class EmailSettingsOut(OutModel):
    """SMTP e-mail notifications."""

    enabled: bool
    smtp_host: str | None
    smtp_port: int
    username: str | None
    password: SecretState
    from_addr: str | None
    to_addrs: list[str]
    use_starttls: bool


class KakaoSettingsOut(OutModel):
    """KakaoTalk "send to me" notifications (OAuth tokens are managed by the integration)."""

    enabled: bool
    rest_api_key: SecretState
    client_secret: SecretState
    redirect_uri: str | None
    access_token: SecretState
    refresh_token: SecretState
    access_expires_at: UTCDateTime | None
    refresh_expires_at: UTCDateTime | None


class TelegramSettingsOut(OutModel):
    """Telegram bot notifications."""

    enabled: bool
    bot_token: SecretState
    chat_id: str | None


class NtfySettingsOut(OutModel):
    """ntfy.sh (or self-hosted ntfy) push notifications."""

    enabled: bool
    server: str
    topic: str | None
    token: SecretState


class NotificationSettingsOut(OutModel):
    """All notification channel settings."""

    desktop: DesktopSettingsOut
    email: EmailSettingsOut
    kakao: KakaoSettingsOut
    telegram: TelegramSettingsOut
    ntfy: NtfySettingsOut


class ApiKeysOut(OutModel):
    """Third-party API keys (redacted)."""

    anthropic: SecretState
    serpapi: SecretState
    google_maps: SecretState


class RuntimeSettingsOut(OutModel):
    """``GET /api/settings`` — effective runtime settings with secrets redacted."""

    browser_channel: str
    browser_headless: bool
    llm_model: str
    llm_effort: LlmEffort
    max_search_combinations: int
    default_providers: list[str]
    provider_options: dict[str, dict[str, Any]]
    """Per-provider options, e.g. ``{"google_flights": {"fetch_mode": "browser"}}``."""
    language: Language
    """Language for generated itineraries and notification text."""
    notifications: NotificationSettingsOut
    api_keys: ApiKeysOut


class DesktopSettingsIn(BaseModel):
    """Partial update of desktop notification settings."""

    enabled: bool | None = None


class EmailSettingsIn(BaseModel):
    """Partial update of e-mail settings; ``password``: null = unchanged, ``""`` = clear."""

    enabled: bool | None = None
    smtp_host: str | None = None
    smtp_port: int | None = Field(None, ge=1, le=65535)
    username: str | None = None
    password: str | None = None
    from_addr: str | None = None
    to_addrs: list[str] | None = None
    use_starttls: bool | None = None


class KakaoSettingsIn(BaseModel):
    """Partial update of Kakao settings; secrets: null = unchanged, ``""`` = clear."""

    enabled: bool | None = None
    rest_api_key: str | None = None
    client_secret: str | None = None
    redirect_uri: str | None = None
    access_token: str | None = None
    refresh_token: str | None = None
    access_expires_at: datetime | None = None
    refresh_expires_at: datetime | None = None


class TelegramSettingsIn(BaseModel):
    """Partial update of Telegram settings; ``bot_token``: null = unchanged, ``""`` = clear."""

    enabled: bool | None = None
    bot_token: str | None = None
    chat_id: str | None = None


class NtfySettingsIn(BaseModel):
    """Partial update of ntfy settings; ``token``: null = unchanged, ``""`` = clear."""

    enabled: bool | None = None
    server: str | None = None
    topic: str | None = None
    token: str | None = None


class NotificationSettingsIn(BaseModel):
    """Partial update of notification settings."""

    desktop: DesktopSettingsIn | None = None
    email: EmailSettingsIn | None = None
    kakao: KakaoSettingsIn | None = None
    telegram: TelegramSettingsIn | None = None
    ntfy: NtfySettingsIn | None = None


class ApiKeysIn(BaseModel):
    """Partial update of API keys: null/omitted = unchanged, ``""`` = clear."""

    anthropic: str | None = None
    serpapi: str | None = None
    google_maps: str | None = None


class RuntimeSettingsIn(BaseModel):
    """``PUT /api/settings`` body (partial).

    Non-secret fields: omitted = unchanged, null = reset to the .env/default value.
    Secret fields: omitted/null = unchanged, ``""`` = clear the UI value.
    """

    browser_channel: Literal["msedge", "chrome", "chromium"] | None = None
    browser_headless: bool | None = None
    llm_model: Annotated[
        Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
        | None,
        BeforeValidator(_blank_to_none),
    ] = None
    """Blank = null = reset to ``LLM_MODEL`` from env (default ``claude-opus-5-5``)."""
    llm_effort: LlmEffort | None = None
    max_search_combinations: int | None = Field(None, ge=1, le=60)
    default_providers: list[str] | None = None
    provider_options: dict[str, dict[str, Any]] | None = None
    """Replaces the whole per-provider options map when given."""
    language: Language | None = None
    notifications: NotificationSettingsIn | None = None
    api_keys: ApiKeysIn | None = None


# --------------------------------------------------------------------------- meta


class MetaDefaults(OutModel):
    """Default values the UI pre-fills."""

    currency: str
    timezone: str
    adults: int = 1
    cabin: Cabin = "economy"
    trip_type: TripType = "round_trip"
    price_basis: PriceBasis = "per_person"
    schedule_mode: ScheduleMode = "times"
    schedule_times: list[str] = Field(default_factory=lambda: list(DEFAULT_SCHEDULE_TIMES))
    style_mode: StyleMode = "percent"
    style_weights: dict[str, float] = Field(default_factory=lambda: dict(DEFAULT_STYLE_WEIGHTS))
    """Default weights for percent mode."""
    score_style_weights: dict[str, float] = Field(
        default_factory=lambda: dict(DEFAULT_STYLE_SCORES)
    )
    """Default weights for score mode."""
    budget_basis: PriceBasis = "total"
    max_schedule_times: int = MAX_SCHEDULE_TIMES


class MetaOut(OutModel):
    """``GET /api/meta`` — enums, style keys, defaults and a providers summary."""

    version: str
    cabins: list[str] = Field(default_factory=lambda: list(CABINS))
    trip_types: list[str] = Field(default_factory=lambda: list(TRIP_TYPES))
    price_bases: list[str] = Field(default_factory=lambda: list(PRICE_BASES))
    schedule_modes: list[str] = Field(default_factory=lambda: list(SCHEDULE_MODES))
    flex_days: list[int] = Field(default_factory=lambda: list(FLEX_DAYS))
    alert_kinds: list[str] = Field(default_factory=lambda: list(ALERT_KINDS))
    notification_kinds: list[str] = Field(default_factory=lambda: list(NOTIFICATION_KINDS))
    notification_channels: list[str] = Field(default_factory=lambda: list(NOTIFICATION_CHANNELS))
    style_keys: list[str] = Field(default_factory=lambda: list(STYLE_KEYS))
    style_modes: list[str] = Field(default_factory=lambda: list(STYLE_MODES))
    companions: list[str] = Field(default_factory=lambda: list(COMPANIONS))
    paces: list[str] = Field(default_factory=lambda: list(PACES))
    llm_efforts: list[str] = Field(default_factory=lambda: list(LLM_EFFORTS))
    defaults: MetaDefaults
    providers: list[ProviderInfo] = Field(default_factory=list)
