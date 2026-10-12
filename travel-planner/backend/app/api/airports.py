"""Airport / metro-code autocomplete (DESIGN §7): ``GET /airports?q=``."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from app.core import airports
from app.schemas import AirportOut

router = APIRouter(tags=["meta"])

MAX_RESULTS = 20


@router.get("/airports", response_model=list[AirportOut])
def search_airports(
    q: str = Query("", max_length=100),
    limit: int = Query(MAX_RESULTS, ge=1, le=MAX_RESULTS),
) -> list[dict[str, Any]]:
    """Airports and metro codes matching ``q`` (code, city, name; metros first); blank -> ``[]``."""
    return airports.search(q, limit=limit)
