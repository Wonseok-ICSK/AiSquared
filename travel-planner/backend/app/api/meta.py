"""``GET /meta``: enums, style keys, UI defaults and a providers summary (DESIGN §6, §7)."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app import __version__
from app.api.providers import build_provider_infos
from app.config import get_settings
from app.db import get_db
from app.schemas import MetaDefaults, MetaOut

router = APIRouter(tags=["meta"])


@router.get("/meta", response_model=MetaOut)
def read_meta(db: Session = Depends(get_db)) -> MetaOut:
    """Everything the UI needs to build forms: enums (schema defaults), defaults, providers."""
    settings = get_settings()
    return MetaOut(
        version=__version__,
        defaults=MetaDefaults(currency=settings.default_currency, timezone=settings.timezone),
        providers=build_provider_infos(db),
    )
