"""Monitoring runs (DESIGN §7): ``GET /trips/{id}/runs?limit=`` and ``GET /runs/{run_id}``."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api._common import get_or_404, get_trip_or_404
from app.db import get_db
from app.models import SearchRun
from app.schemas import SearchRunOut

router = APIRouter(tags=["runs"])


@router.get("/trips/{trip_id}/runs", response_model=list[SearchRunOut])
def list_runs(
    trip_id: int,
    limit: int = Query(50, ge=1, le=500),
    db: Session = Depends(get_db),
) -> list[SearchRun]:
    """The trip's runs, newest first."""
    get_trip_or_404(db, trip_id)
    stmt = (
        select(SearchRun)
        .where(SearchRun.trip_id == trip_id)
        .order_by(SearchRun.id.desc())
        .limit(limit)
    )
    return list(db.scalars(stmt))


@router.get("/runs/{run_id}", response_model=SearchRunOut)
def get_run(run_id: int, db: Session = Depends(get_db)) -> SearchRun:
    """One run (poll while ``status == "running"``)."""
    return get_or_404(db, SearchRun, run_id, "Run")
