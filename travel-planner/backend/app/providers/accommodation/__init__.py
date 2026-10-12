"""Accommodation (hotel) providers — interface only for now."""

from app.providers.accommodation.base import (
    AccommodationProvider,
    HotelOffer,
    HotelQuery,
    create_provider,
    get_provider_class,
    list_provider_classes,
    register,
)

__all__ = [
    "AccommodationProvider",
    "HotelOffer",
    "HotelQuery",
    "create_provider",
    "get_provider_class",
    "list_provider_classes",
    "register",
]
