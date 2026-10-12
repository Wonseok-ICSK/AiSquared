"""Flight providers. Importing this package imports every provider module in it, so each
concrete provider (decorated with ``@register``) becomes available in the registry.

A provider module that fails to import (e.g. an optional dependency is missing) is logged
and skipped instead of breaking the app.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil

from app.providers.flights.registry import (
    create_provider,
    default_provider_keys,
    get_provider_class,
    list_provider_classes,
    register,
    resolve_provider_keys,
)

logger = logging.getLogger(__name__)

_INFRA_MODULES = {"base", "registry"}


def _import_provider_modules() -> None:
    for module in pkgutil.iter_modules(__path__):
        if module.name in _INFRA_MODULES or module.name.startswith("_"):
            continue
        try:
            importlib.import_module(f"{__name__}.{module.name}")
        except Exception:  # noqa: BLE001 - one broken provider must not break the app
            logger.exception("Flight provider module %r failed to import; skipped", module.name)


_import_provider_modules()

__all__ = [
    "create_provider",
    "default_provider_keys",
    "get_provider_class",
    "list_provider_classes",
    "register",
    "resolve_provider_keys",
]
