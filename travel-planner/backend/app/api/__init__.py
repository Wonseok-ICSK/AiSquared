"""API routers (DESIGN §7).

Contract for router modules: any ``app/api/<name>.py`` (or sub-package) that defines
``router = APIRouter(...)`` with paths relative to ``/api`` (e.g. ``@router.get("/trips")``)
is discovered and mounted by :func:`include_routers` — no edits to ``main.py`` or to this
file are needed. Modules whose name starts with ``_`` (helpers such as ``_deps.py``) and
modules without a ``router`` attribute are not mounted. Routers are included in
alphabetical module order.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil

from fastapi import APIRouter, FastAPI

logger = logging.getLogger(__name__)

API_PREFIX = "/api"


def discover_router_modules() -> list[str]:
    """Names of the candidate router modules in this package (sorted, ``_``-prefixed skipped)."""
    return sorted(m.name for m in pkgutil.iter_modules(__path__) if not m.name.startswith("_"))


def include_routers(app: FastAPI) -> list[str]:
    """Include every discovered router module under ``/api``; returns the names included."""
    included: list[str] = []
    for name in discover_router_modules():
        module = importlib.import_module(f"{__name__}.{name}")  # import errors propagate
        router = getattr(module, "router", None)
        if router is None:
            logger.debug("Module %s.%s has no 'router'; not mounted", __name__, name)
            continue
        if not isinstance(router, APIRouter):
            raise TypeError(f"{__name__}.{name}.router must be an APIRouter")
        app.include_router(router, prefix=API_PREFIX)
        included.append(name)
    return included
