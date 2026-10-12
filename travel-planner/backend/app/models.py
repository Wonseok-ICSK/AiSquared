"""ORM models (DESIGN §4). Enum-like columns are lowercase strings validated in ``schemas``.

JSON columns use ``MutableList``/``MutableDict`` so in-place top-level changes
(``run.log.append(...)``, ``run.provider_stats[key] = {...}``) are persisted; nested
mutations still require reassigning the column. Non-null JSON list/dict columns are filled
with ``[]``/``{}`` when the object is constructed (not only at INSERT), so they can be
mutated before the first flush. Nullable JSON columns (``FlightOffer.inbound``,
``Itinerary.plan``) store Python ``None`` as SQL ``NULL`` (``IS NULL`` filters work).
Other column defaults still apply at flush time.
"""

from __future__ import annotations

from datetime import date, datetime
from collections.abc import Callable
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    event,
)
from sqlalchemy.ext.mutable import MutableDict, MutableList
from sqlalchemy.orm import Mapped, MappedColumn, mapped_column, relationship

from app.db import Base
from app.timeutil import utcnow

JSONList = MutableList.as_mutable(JSON)
JSONDict = MutableDict.as_mutable(JSON)
NullableJSONDict = MutableDict.as_mutable(JSON(none_as_null=True))
"""JSON dict column where Python ``None`` is stored as SQL ``NULL`` (not JSON ``'null'``)."""

_CHILD_CASCADE = "all, delete-orphan"
_PY_FACTORY = "py_factory"  # Column.info key: container factory applied at construction


def json_list() -> MappedColumn[Any]:
    """Non-null JSON list column, ``[]`` from construction on."""
    return mapped_column(JSONList, default=list, nullable=False, info={_PY_FACTORY: list})


def json_dict() -> MappedColumn[Any]:
    """Non-null JSON dict column, ``{}`` from construction on."""
    return mapped_column(JSONDict, default=dict, nullable=False, info={_PY_FACTORY: dict})


@event.listens_for(Base, "init", propagate=True)
def _init_json_containers(target: Any, _args: Any, kwargs: dict[str, Any]) -> None:
    """Give unset JSON list/dict columns a fresh container before ``__init__`` assigns kwargs."""
    for prop in target.__mapper__.column_attrs:
        factory: Callable[[], Any] | None = prop.columns[0].info.get(_PY_FACTORY)
        if factory is not None and prop.key not in kwargs:
            setattr(target, prop.key, factory())


class TimestampMixin:
    """``created_at`` / ``updated_at`` as naive UTC."""

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )


class Trip(TimestampMixin, Base):
    """A monitored trip (route, dates, filters, schedule)."""

    __tablename__ = "trips"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    origin: Mapped[str] = mapped_column(String(8))
    destination: Mapped[str] = mapped_column(String(8))
    trip_type: Mapped[str] = mapped_column(String(16), default="round_trip")
    depart_date: Mapped[date] = mapped_column(Date)
    return_date: Mapped[date | None] = mapped_column(Date, default=None)
    adults: Mapped[int] = mapped_column(Integer, default=1)
    cabin: Mapped[str] = mapped_column(String(20), default="economy")
    direct_only: Mapped[bool] = mapped_column(Boolean, default=False)
    max_stops: Mapped[int | None] = mapped_column(Integer, default=None)

    outbound_depart_from: Mapped[str | None] = mapped_column(String(5), default=None)
    outbound_depart_to: Mapped[str | None] = mapped_column(String(5), default=None)
    outbound_arrive_from: Mapped[str | None] = mapped_column(String(5), default=None)
    outbound_arrive_to: Mapped[str | None] = mapped_column(String(5), default=None)
    return_depart_from: Mapped[str | None] = mapped_column(String(5), default=None)
    return_depart_to: Mapped[str | None] = mapped_column(String(5), default=None)
    return_arrive_from: Mapped[str | None] = mapped_column(String(5), default=None)
    return_arrive_to: Mapped[str | None] = mapped_column(String(5), default=None)

    max_price: Mapped[float | None] = mapped_column(Float, default=None)
    target_price: Mapped[float | None] = mapped_column(Float, default=None)
    price_basis: Mapped[str] = mapped_column(String(16), default="per_person")
    currency: Mapped[str] = mapped_column(String(3), default="KRW")

    flex_depart_days: Mapped[int] = mapped_column(Integer, default=0)
    flex_return_days: Mapped[int] = mapped_column(Integer, default=0)
    include_nearby_airports: Mapped[bool] = mapped_column(Boolean, default=False)
    alt_destinations: Mapped[list[str]] = json_list()
    providers: Mapped[list[str]] = json_list()

    schedule_mode: Mapped[str] = mapped_column(String(16), default="times")
    schedule_times: Mapped[list[str]] = json_list()
    schedule_interval_hours: Mapped[float | None] = mapped_column(Float, default=None)

    active: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str | None] = mapped_column(Text, default=None)

    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    last_status: Mapped[str | None] = mapped_column(String(16), default=None)
    last_error: Mapped[str | None] = mapped_column(Text, default=None)

    runs: Mapped[list[SearchRun]] = relationship(back_populates="trip", cascade=_CHILD_CASCADE)
    offers: Mapped[list[FlightOffer]] = relationship(back_populates="trip", cascade=_CHILD_CASCADE)
    alert_rules: Mapped[list[AlertRule]] = relationship(
        back_populates="trip", cascade=_CHILD_CASCADE
    )
    notifications: Mapped[list[Notification]] = relationship(
        back_populates="trip", cascade=_CHILD_CASCADE
    )
    preferences: Mapped[TravelPreference | None] = relationship(
        back_populates="trip", cascade=_CHILD_CASCADE, uselist=False
    )
    itineraries: Mapped[list[Itinerary]] = relationship(
        back_populates="trip", cascade=_CHILD_CASCADE
    )


class SearchRun(TimestampMixin, Base):
    """One monitoring execution for a trip."""

    __tablename__ = "search_runs"
    __table_args__ = (Index("ix_search_runs_trip_started", "trip_id", "started_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trip_id: Mapped[int] = mapped_column(ForeignKey("trips.id", ondelete="CASCADE"))
    trigger: Mapped[str] = mapped_column(String(16), default="manual")
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    status: Mapped[str] = mapped_column(String(16), default="running")
    error: Mapped[str | None] = mapped_column(Text, default=None)
    combinations_total: Mapped[int] = mapped_column(Integer, default=0)
    combinations_searched: Mapped[int] = mapped_column(Integer, default=0)
    offers_found: Mapped[int] = mapped_column(Integer, default=0)
    offers_matching: Mapped[int] = mapped_column(Integer, default=0)
    min_price_total: Mapped[float | None] = mapped_column(Float, default=None)
    min_price_per_person: Mapped[float | None] = mapped_column(Float, default=None)
    currency: Mapped[str] = mapped_column(String(3), default="KRW")
    best_offer_id: Mapped[int | None] = mapped_column(Integer, default=None)
    provider_stats: Mapped[dict[str, Any]] = json_dict()
    log: Mapped[list[str]] = json_list()

    trip: Mapped[Trip] = relationship(back_populates="runs")
    offers: Mapped[list[FlightOffer]] = relationship(back_populates="run", cascade=_CHILD_CASCADE)


class FlightOffer(TimestampMixin, Base):
    """One observed offer/price (a price-history row)."""

    __tablename__ = "flight_offers"
    __table_args__ = (
        Index("ix_flight_offers_trip_offer_key", "trip_id", "offer_key"),
        Index("ix_flight_offers_run_id", "run_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trip_id: Mapped[int] = mapped_column(ForeignKey("trips.id", ondelete="CASCADE"))
    run_id: Mapped[int] = mapped_column(ForeignKey("search_runs.id", ondelete="CASCADE"))
    searched_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    provider: Mapped[str] = mapped_column(String(40))
    seller: Mapped[str | None] = mapped_column(String(200), default=None)
    offer_key: Mapped[str] = mapped_column(String(512))
    origin: Mapped[str] = mapped_column(String(8))
    destination: Mapped[str] = mapped_column(String(8))
    depart_date: Mapped[date] = mapped_column(Date)
    return_date: Mapped[date | None] = mapped_column(Date, default=None)

    airline: Mapped[str | None] = mapped_column(String(300), default=None)
    flight_number: Mapped[str | None] = mapped_column(String(200), default=None)
    departure_airport: Mapped[str] = mapped_column(String(8))
    arrival_airport: Mapped[str] = mapped_column(String(8))
    departure_time: Mapped[str] = mapped_column(String(19))
    arrival_time: Mapped[str] = mapped_column(String(19))
    duration_minutes: Mapped[int | None] = mapped_column(Integer, default=None)
    stops: Mapped[int] = mapped_column(Integer, default=0)
    outbound: Mapped[dict[str, Any]] = json_dict()
    inbound: Mapped[dict[str, Any] | None] = mapped_column(NullableJSONDict, default=None)

    cabin: Mapped[str] = mapped_column(String(20), default="economy")
    fare_type: Mapped[str | None] = mapped_column(String(100), default=None)
    baggage: Mapped[str | None] = mapped_column(String(200), default=None)
    price_total: Mapped[float] = mapped_column(Float)
    price_per_person: Mapped[float] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String(3))
    previous_price: Mapped[float | None] = mapped_column(Float, default=None)
    lowest_price: Mapped[float | None] = mapped_column(Float, default=None)

    booking_url: Mapped[str | None] = mapped_column(Text, default=None)
    source_url: Mapped[str | None] = mapped_column(Text, default=None)
    matches_filters: Mapped[bool] = mapped_column(Boolean, default=True)
    raw: Mapped[dict[str, Any]] = json_dict()

    trip: Mapped[Trip] = relationship(back_populates="offers")
    run: Mapped[SearchRun] = relationship(back_populates="offers")


class AlertRule(TimestampMixin, Base):
    """A price alert rule attached to a trip."""

    __tablename__ = "alert_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trip_id: Mapped[int] = mapped_column(ForeignKey("trips.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(20))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    threshold: Mapped[float | None] = mapped_column(Float, default=None)
    drop_percent: Mapped[float | None] = mapped_column(Float, default=None)
    price_basis: Mapped[str] = mapped_column(String(16), default="per_person")
    armed: Mapped[bool] = mapped_column(Boolean, default=True)
    last_triggered_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    last_triggered_price: Mapped[float | None] = mapped_column(Float, default=None)

    trip: Mapped[Trip] = relationship(back_populates="alert_rules")


class Notification(TimestampMixin, Base):
    """An in-app notification plus per-channel delivery results."""

    __tablename__ = "notifications"
    __table_args__ = (Index("ix_notifications_read_at", "read_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trip_id: Mapped[int | None] = mapped_column(
        ForeignKey("trips.id", ondelete="CASCADE"), default=None
    )
    alert_rule_id: Mapped[int | None] = mapped_column(
        ForeignKey("alert_rules.id", ondelete="SET NULL"), default=None
    )
    kind: Mapped[str] = mapped_column(String(20), default="system")
    title: Mapped[str] = mapped_column(String(300))
    body: Mapped[str] = mapped_column(Text, default="")
    url: Mapped[str | None] = mapped_column(Text, default=None)
    data: Mapped[dict[str, Any]] = json_dict()
    read_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    channel_results: Mapped[dict[str, str]] = json_dict()

    trip: Mapped[Trip | None] = relationship(back_populates="notifications")


class TravelPreference(TimestampMixin, Base):
    """Travel style, food and budget preferences (1-1 with a trip)."""

    __tablename__ = "travel_preferences"

    trip_id: Mapped[int] = mapped_column(
        ForeignKey("trips.id", ondelete="CASCADE"), primary_key=True
    )
    style_mode: Mapped[str] = mapped_column(String(16), default="percent")
    style_weights: Mapped[dict[str, float]] = json_dict()
    companion: Mapped[str] = mapped_column(String(16), default="solo")
    pace: Mapped[str] = mapped_column(String(16), default="moderate")
    food_wishlist: Mapped[list[str]] = json_list()
    preferred_cuisines: Mapped[list[str]] = json_list()
    avoid_foods: Mapped[list[str]] = json_list()
    restaurant_budget_min: Mapped[float | None] = mapped_column(Float, default=None)
    restaurant_budget_max: Mapped[float | None] = mapped_column(Float, default=None)
    restaurant_budget_currency: Mapped[str | None] = mapped_column(String(3), default=None)
    hotel_area: Mapped[str | None] = mapped_column(String(200), default=None)
    must_visit: Mapped[list[str]] = json_list()
    avoid: Mapped[list[str]] = json_list()
    notes: Mapped[str | None] = mapped_column(Text, default=None)

    budget_basis: Mapped[str] = mapped_column(String(16), default="total")
    budget_currency: Mapped[str] = mapped_column(String(3), default="KRW")
    budget_total: Mapped[float] = mapped_column(Float)
    budget_flight: Mapped[float] = mapped_column(Float)
    budget_hotel: Mapped[float | None] = mapped_column(Float, default=None)
    budget_food: Mapped[float | None] = mapped_column(Float, default=None)
    budget_transport: Mapped[float | None] = mapped_column(Float, default=None)
    budget_activities: Mapped[float | None] = mapped_column(Float, default=None)
    budget_shopping: Mapped[float | None] = mapped_column(Float, default=None)
    budget_other: Mapped[float | None] = mapped_column(Float, default=None)

    trip: Mapped[Trip] = relationship(back_populates="preferences")


class Itinerary(TimestampMixin, Base):
    """A generated travel plan (``plan`` holds the ``ItineraryPlan`` JSON, §10)."""

    __tablename__ = "itineraries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trip_id: Mapped[int] = mapped_column(ForeignKey("trips.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(String(16), default="pending")
    error: Mapped[str | None] = mapped_column(Text, default=None)
    progress: Mapped[str | None] = mapped_column(String(300), default=None)
    offer_id: Mapped[int | None] = mapped_column(
        ForeignKey("flight_offers.id", ondelete="SET NULL"), default=None
    )
    arrival_at: Mapped[str | None] = mapped_column(String(19), default=None)
    departure_at: Mapped[str | None] = mapped_column(String(19), default=None)
    arrival_airport: Mapped[str | None] = mapped_column(String(8), default=None)
    departure_airport: Mapped[str | None] = mapped_column(String(8), default=None)
    hotel_area: Mapped[str | None] = mapped_column(String(200), default=None)
    generator: Mapped[str] = mapped_column(String(16), default="rule_based")
    model: Mapped[str | None] = mapped_column(String(100), default=None)
    request: Mapped[dict[str, Any]] = json_dict()
    plan: Mapped[dict[str, Any] | None] = mapped_column(NullableJSONDict, default=None)
    parent_id: Mapped[int | None] = mapped_column(
        ForeignKey("itineraries.id", ondelete="SET NULL"), default=None
    )
    feedback: Mapped[str | None] = mapped_column(Text, default=None)
    usage: Mapped[dict[str, Any]] = json_dict()

    trip: Mapped[Trip] = relationship(back_populates="itineraries")


class ProviderSession(TimestampMixin, Base):
    """Login session state for a browser-based provider."""

    __tablename__ = "provider_sessions"

    provider: Mapped[str] = mapped_column(String(40), primary_key=True)
    status: Mapped[str] = mapped_column(String(16), default="none")
    storage_path: Mapped[str | None] = mapped_column(Text, default=None)
    account_hint: Mapped[str | None] = mapped_column(String(200), default=None)


class AppSetting(TimestampMixin, Base):
    """Key/value JSON settings (e.g. ``runtime`` for :mod:`app.settings_store`)."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[dict[str, Any]] = json_dict()


class GeocodeCache(Base):
    """Permanent geocoding cache (itinerary module): normalized ``"name|city|country"`` -> point."""

    __tablename__ = "geocode_cache"

    query: Mapped[str] = mapped_column(String(400), primary_key=True)
    lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    lon: Mapped[float | None] = mapped_column(Float, nullable=True)
    display_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String(20), default="none", nullable=False)
    """``"nominatim"`` | ``"llm"`` | ``"none"`` (looked up, nothing found)."""
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
