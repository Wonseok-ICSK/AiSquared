"""Flight provider abstraction (DESIGN §5).

Times are airport-local naive ISO strings (``"2026-11-20T09:05"``) and are never converted.
``FlightOffer.offer_key()`` identifies the same itinerary across runs and providers.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, fields
from datetime import date
from typing import TYPE_CHECKING, Any, ClassVar, Literal

if TYPE_CHECKING:
    from app.browser.manager import BrowserManager

CabinClass = Literal["economy", "premium_economy", "business", "first"]

_FLIGHT_NO_RE = re.compile(r"^([A-Z0-9]{2})(\d{1,4})([A-Z]?)$")


@dataclass(frozen=True)
class FlightQuery:
    """One provider search: a single origin/destination/date combination."""

    origin: str  # IATA airport or metro code
    destination: str
    depart_date: date
    return_date: date | None  # None => one-way
    adults: int = 1
    cabin: CabinClass = "economy"
    direct_only: bool = False
    max_stops: int | None = None
    currency: str = "KRW"

    @property
    def is_round_trip(self) -> bool:
        """True when a return date is set."""
        return self.return_date is not None


@dataclass
class FlightSegment:
    """One flown segment (single flight number)."""

    carrier_code: str | None
    carrier_name: str | None
    flight_number: str | None  # "KE703"
    departure_airport: str
    arrival_airport: str
    departure_time: str  # local ISO "YYYY-MM-DDTHH:MM"
    arrival_time: str
    duration_minutes: int | None = None
    operating_carrier: str | None = None
    aircraft: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe dict."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FlightSegment:
        """Inverse of :meth:`to_dict` (unknown keys ignored)."""
        return cls(**_known_fields(cls, data))


@dataclass
class FlightLeg:
    """Outbound or inbound journey, possibly with connections."""

    departure_airport: str
    arrival_airport: str
    departure_time: str  # local ISO
    arrival_time: str
    duration_minutes: int | None
    stops: int
    airlines: list[str]  # display names
    flight_numbers: list[str]  # may be empty if provider lacks them
    segments: list[FlightSegment] = field(default_factory=list)
    layovers: list[str] = field(default_factory=list)  # airport codes

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe dict (stored in ``FlightOffer.outbound``/``inbound`` columns)."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FlightLeg:
        """Inverse of :meth:`to_dict` (unknown keys ignored)."""
        values = _known_fields(cls, data)
        values["segments"] = [FlightSegment.from_dict(s) for s in data.get("segments") or []]
        values["airlines"] = list(data.get("airlines") or [])
        values["flight_numbers"] = list(data.get("flight_numbers") or [])
        values["layovers"] = list(data.get("layovers") or [])
        return cls(**values)


@dataclass
class FlightOffer:
    """A priced itinerary from one provider. Prices are in major units of ``currency``."""

    provider: str
    outbound: FlightLeg
    inbound: FlightLeg | None
    price_total: float
    price_per_person: float
    currency: str
    cabin: CabinClass
    adults: int = 1
    fare_type: str | None = None
    baggage: str | None = None
    seller: str | None = None
    booking_url: str | None = None
    source_url: str | None = None
    provider_offer_id: str | None = None
    return_date: str | None = None
    """``"YYYY-MM-DD"`` of the return flight when this is a round trip whose ``inbound`` leg
    details are unknown (e.g. Google Flights lists outbound options priced at the round-trip
    total). Keeps offers for different return dates distinct in :meth:`offer_key`."""
    raw: dict[str, Any] = field(default_factory=dict)

    def offer_key(self) -> str:
        """``"{OUT}|{IN}|{CABIN}"`` — stable across runs/providers (see :func:`leg_key`).

        ``IN`` is the inbound leg key, ``"RT:<return_date>"`` for a round trip with unknown
        inbound details, or ``"-"`` for one-way.
        """
        if self.inbound is not None:
            inbound = leg_key(self.inbound)
        elif self.return_date:
            inbound = f"RT:{self.return_date}"
        else:
            inbound = "-"
        return f"{leg_key(self.outbound)}|{inbound}|{self.cabin}".upper()

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe dict (asdict)."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FlightOffer:
        """Inverse of :meth:`to_dict` (unknown keys ignored)."""
        values = _known_fields(cls, data)
        values["outbound"] = FlightLeg.from_dict(data["outbound"])
        values["inbound"] = FlightLeg.from_dict(data["inbound"]) if data.get("inbound") else None
        values["raw"] = dict(data.get("raw") or {})
        return cls(**values)


def _known_fields(cls: type, data: dict[str, Any]) -> dict[str, Any]:
    names = {f.name for f in fields(cls)}
    return {k: v for k, v in data.items() if k in names}


def normalize_flight_number(value: str) -> str:
    """Canonical flight number: ``"ke 0703"`` -> ``"KE703"``; unknown shapes upper-cased."""
    compact = re.sub(r"[\s\-]", "", value).upper()
    match = _FLIGHT_NO_RE.match(compact)
    if not match:
        return compact
    carrier, number, suffix = match.groups()
    return f"{carrier}{int(number)}{suffix}"


def leg_key(leg: FlightLeg) -> str:
    """``"{dep}-{arr}-{dep_local_iso}-{ids}"`` with ids = flight numbers (or airlines) joined '+'."""
    ids = [normalize_flight_number(n) for n in leg.flight_numbers if n] or [
        a.strip() for a in leg.airlines if a
    ]
    departure = leg.departure_time.replace(" ", "T")[:16]
    return f"{leg.departure_airport}-{leg.arrival_airport}-{departure}-{'+'.join(ids)}".upper()


@dataclass
class ProviderStatus:
    """Whether a provider can run, with a human-readable reason."""

    configured: bool
    message: str = ""


class ProviderError(Exception):
    """A provider search failed."""


class ProviderNotConfigured(ProviderError):
    """Missing API key / login."""


class ProviderBlocked(ProviderError):
    """Captcha, anti-bot or consent wall."""


@dataclass
class ProviderContext:
    """What a provider may use: secrets (``get_secret("serpapi")``), the shared browser, options."""

    get_secret: Callable[[str], str | None]
    browser: BrowserManager | None = None
    http_timeout: float = 30.0
    options: dict[str, Any] = field(default_factory=dict)
    """Per-provider options from ``RuntimeSettings.provider_options[<key>]`` (e.g.
    ``{"fetch_mode": "http"}`` for google_flights). Providers must tolerate missing keys."""


class FlightProvider(ABC):
    """Base class for flight sources; subclasses register via ``registry.register``."""

    key: ClassVar[str]
    display_name: ClassVar[str]
    description: ClassVar[str] = ""
    requires_api_key: ClassVar[bool] = False
    api_key_name: ClassVar[str | None] = None  # secret name in settings_store
    uses_browser: ClassVar[bool] = False
    supports_login: ClassVar[bool] = False
    supports_metro_codes: ClassVar[bool] = False  # accepts SEL/TYO directly
    max_queries_per_run: ClassVar[int] = 12  # cost/latency cap hint
    # Used for trips with an empty ``providers`` list (unless the runtime setting
    # ``default_providers`` overrides it); ``mock`` and opt-in providers set False.
    enabled_by_default: ClassVar[bool] = True

    def __init__(self, ctx: ProviderContext) -> None:
        self.ctx = ctx

    def status(self) -> ProviderStatus:
        """Configured unless an API key is required and missing."""
        if not self.requires_api_key:
            return ProviderStatus(configured=True)
        if self.api_key_name and self.ctx.get_secret(self.api_key_name):
            return ProviderStatus(configured=True, message="API key configured")
        return ProviderStatus(configured=False, message=f"API key '{self.api_key_name}' is not set")

    @abstractmethod
    def search_flights(self, query: FlightQuery) -> list[FlightOffer]:
        """Search one combination; raise ``ProviderError`` subclasses on failure."""

    def get_flight_details(self, offer: FlightOffer) -> FlightOffer:
        """Enrich an offer (segments, baggage...); default returns it unchanged."""
        return offer

    def get_price(self, offer: FlightOffer) -> float | None:
        """Current per-person price of the same itinerary (re-search + ``offer_key`` match)."""
        key = offer.offer_key()
        for candidate in self.search_flights(query_for_offer(offer)):
            if candidate.offer_key() == key:
                return candidate.price_per_person
        return None

    def get_booking_url(self, offer: FlightOffer) -> str | None:
        """Where the user can book this offer."""
        return offer.booking_url or offer.source_url


def query_for_offer(offer: FlightOffer) -> FlightQuery:
    """Rebuild the search that would return ``offer`` (actual airports and dates)."""
    out = offer.outbound
    return FlightQuery(
        origin=out.departure_airport,
        destination=out.arrival_airport,
        depart_date=date.fromisoformat(out.departure_time[:10]),
        return_date=(
            date.fromisoformat(offer.inbound.departure_time[:10])
            if offer.inbound
            else (date.fromisoformat(offer.return_date) if offer.return_date else None)
        ),
        adults=offer.adults,
        cabin=offer.cabin,
        direct_only=False,
        currency=offer.currency,
    )
