"""Scheduled monitoring jobs (DESIGN §7): ``GET /scheduler/jobs``."""

from __future__ import annotations

from fastapi import APIRouter

from app.schemas import SchedulerJobOut
from app.services.scheduler import get_scheduler

router = APIRouter(tags=["scheduler"])


@router.get("/scheduler/jobs", response_model=list[SchedulerJobOut])
def list_jobs() -> list[SchedulerJobOut]:
    """One entry per scheduled trip (its next check) plus housekeeping, soonest first."""
    return [SchedulerJobOut.model_validate(job) for job in get_scheduler().jobs()]
