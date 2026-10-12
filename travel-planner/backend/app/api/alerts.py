"""Alert rules (DESIGN §7, §11): ``GET/POST /trips/{id}/alerts``; ``PUT/DELETE /alerts/{id}``.

Re-arming: a rule that is switched from disabled to enabled, or whose condition changes
(kind, threshold, drop percent, price basis), is armed again; an explicit ``armed`` in the
update body always wins.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response, status
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api._common import get_or_404, get_trip_or_404, validation_error
from app.db import get_db
from app.models import AlertRule
from app.schemas import AlertRuleCreate, AlertRuleOut, AlertRuleUpdate

router = APIRouter(tags=["alerts"])

_CONDITION_FIELDS = ("kind", "threshold", "drop_percent", "price_basis")


@router.get("/trips/{trip_id}/alerts", response_model=list[AlertRuleOut])
def list_alerts(trip_id: int, db: Session = Depends(get_db)) -> list[AlertRule]:
    """The trip's alert rules, oldest first."""
    get_trip_or_404(db, trip_id)
    return list(
        db.scalars(select(AlertRule).where(AlertRule.trip_id == trip_id).order_by(AlertRule.id))
    )


@router.post(
    "/trips/{trip_id}/alerts", response_model=AlertRuleOut, status_code=status.HTTP_201_CREATED
)
def create_alert(trip_id: int, body: AlertRuleCreate, db: Session = Depends(get_db)) -> AlertRule:
    """Add an (armed) alert rule to the trip."""
    get_trip_or_404(db, trip_id)
    rule = AlertRule(trip_id=trip_id, armed=True, **body.model_dump())
    db.add(rule)
    db.commit()
    return rule


@router.put("/alerts/{rule_id}", response_model=AlertRuleOut)
def update_alert(rule_id: int, body: AlertRuleUpdate, db: Session = Depends(get_db)) -> AlertRule:
    """Partial update; the merged rule is validated as a whole (422 when invalid)."""
    rule = get_or_404(db, AlertRule, rule_id, "Alert rule")
    current = {name: getattr(rule, name) for name in AlertRuleCreate.model_fields}
    try:
        merged = body.merged_with(current).model_dump()
    except ValidationError as exc:
        raise validation_error(exc) from exc
    rearm = (merged["enabled"] and not rule.enabled) or any(
        merged[name] != current[name] for name in _CONDITION_FIELDS
    )
    for name, value in merged.items():
        setattr(rule, name, value)
    if body.armed is not None:
        rule.armed = body.armed
    elif rearm:
        rule.armed = True
    db.commit()
    return rule


@router.delete("/alerts/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_alert(rule_id: int, db: Session = Depends(get_db)) -> Response:
    """Delete an alert rule (its notifications keep existing, unlinked)."""
    rule = get_or_404(db, AlertRule, rule_id, "Alert rule")
    db.delete(rule)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
