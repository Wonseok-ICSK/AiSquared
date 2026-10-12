"""Accommodation provider interface (future hotel sources; no implementations yet)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any, ClassVar

from app.providers import ProviderRegistry
from app.providers.flights.base import ProviderContext, ProviderStatus


@dataclass(frozen=True)
class HotelQuery:
    """A hotel search for one stay."""

    city: str  # city name or IATA/metro code
    check_in: date
    check_out: date
    adults: int = 1
    rooms: int = 1
    currency: str = "KRW"
    area: str | None = None  # preferred neighbourhood, e.g. "Shinjuku"
    lat: float | None = None  # search centre (overrides city/area when given)
    lng: float | None = None
    radius_km: float | None = None
    max_price_per_night: float | None = None
    min_rating: float | None = None
    breakfast_required: bool = False

    @property
    def nights(self) -> int:
        """Number of nights of the stay."""
        return (self.check_out - self.check_in).days


@dataclass
class HotelOffer:
    """A priced room offer. Prices are in major units of ``currency``."""

    provider: str
    name: str
    price_per_night: float
    total_price: float
    currency: str
    rating: float | None = None  # normalised 0-10 when known
    review_count: int | None = None
    lat: float | None = None
    lng: float | None = None
    address: str | None = None
    area: str | None = None
    distance_to_station_m: int | None = None
    breakfast_included: bool | None = None
    room_type: str | None = None
    cancellation_policy: str | None = None
    booking_url: str | None = None
    provider_offer_id: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe dict."""
        return asdict(self)


class AccommodationProvider(ABC):
    """Base class for hotel sources (same context/status model as flight providers)."""

    key: ClassVar[str]
    display_name: ClassVar[str]
    description: ClassVar[str] = ""
    requires_api_key: ClassVar[bool] = False
    api_key_name: ClassVar[str | None] = None
    uses_browser: ClassVar[bool] = False

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
    def search_hotels(self, query: HotelQuery) -> list[HotelOffer]:
        """Search hotels for the stay; raise ``ProviderError`` subclasses on failure."""

    def get_booking_url(self, offer: HotelOffer) -> str | None:
        """Where the user can book this offer."""
        return offer.booking_url


_registry: ProviderRegistry[type[AccommodationProvider]] = ProviderRegistry("accommodation")


def register(cls: type[AccommodationProvider]) -> type[AccommodationProvider]:
    """Register an accommodation provider class under ``cls.key``."""
    return _registry.register(cls)


def get_provider_class(key: str) -> type[AccommodationProvider]:
    """Provider class for ``key`` (``KeyError`` if unknown)."""
    return _registry.get(key)


def list_provider_classes() -> list[type[AccommodationProvider]]:
    """All registered accommodation provider classes."""
    return _registry.all()


def create_provider(key: str, ctx: ProviderContext) -> AccommodationProvider:
    """Instantiate the accommodation provider registered under ``key``."""
    return get_provider_class(key)(ctx)
