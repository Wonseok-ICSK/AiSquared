"""Flight provider registry. Providers self-register on import of ``app.providers.flights``."""

from __future__ import annotations

import logging
from collections.abc import Sequence

from app.providers import ProviderRegistry
from app.providers.flights.base import FlightProvider, ProviderContext

logger = logging.getLogger(__name__)

_registry: ProviderRegistry[type[FlightProvider]] = ProviderRegistry("flight")


def register(cls: type[FlightProvider]) -> type[FlightProvider]:
    """Register a provider class under ``cls.key`` (use as ``@register``)."""
    return _registry.register(cls)


def get_provider_class(key: str) -> type[FlightProvider]:
    """Provider class for ``key`` (``KeyError`` if unknown)."""
    return _registry.get(key)


def list_provider_classes() -> list[type[FlightProvider]]:
    """All registered provider classes, in registration order."""
    return _registry.all()


def create_provider(key: str, ctx: ProviderContext) -> FlightProvider:
    """Instantiate the provider registered under ``key``."""
    return get_provider_class(key)(ctx)


def default_provider_keys(default_providers: Sequence[str] = ()) -> list[str]:
    """Providers used for a trip whose ``providers`` list is empty.

    ``default_providers`` is the runtime setting of the same name: when non-empty it is the
    list (unknown keys dropped); when empty, every registered provider with
    ``enabled_by_default`` (so never ``mock``). Callers still skip providers whose
    ``status().configured`` is False.
    """
    if default_providers:
        return _known_keys(default_providers)
    return [cls.key for cls in list_provider_classes() if cls.enabled_by_default]


def resolve_provider_keys(
    trip_providers: Sequence[str], default_providers: Sequence[str] = ()
) -> list[str]:
    """Provider keys to search for a trip: its own list if non-empty, else the defaults."""
    if trip_providers:
        return _known_keys(trip_providers)
    return default_provider_keys(default_providers)


def _known_keys(keys: Sequence[str]) -> list[str]:
    known = {cls.key for cls in list_provider_classes()}
    unknown = [key for key in keys if key not in known]
    if unknown:
        logger.warning("Ignoring unknown flight provider(s): %s", ", ".join(unknown))
    return [key for key in dict.fromkeys(keys) if key in known]
