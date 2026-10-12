"""In-app inbox (DESIGN §7, §12).

``GET /notifications?unread_only=&limit=`` (newest first); ``GET /notifications/unread-count``;
``POST /notifications/{id}/read``; ``POST /notifications/read-all``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.api._common import get_or_404
from app.db import get_db
from app.models import Notification
from app.schemas import MessageOut, NotificationOut, UnreadCountOut
from app.timeutil import utcnow

router = APIRouter(tags=["notifications"])


@router.get("/notifications", response_model=list[NotificationOut])
def list_notifications(
    unread_only: bool = False,
    limit: int = Query(100, ge=1, le=1000),
    db: Session = Depends(get_db),
) -> list[Notification]:
    """Notifications, newest first."""
    stmt = select(Notification).order_by(Notification.id.desc()).limit(limit)
    if unread_only:
        stmt = stmt.where(Notification.read_at.is_(None))
    return list(db.scalars(stmt))


@router.get("/notifications/unread-count", response_model=UnreadCountOut)
def unread_count(db: Session = Depends(get_db)) -> UnreadCountOut:
    """How many notifications are unread (the bell badge)."""
    count = db.scalar(select(func.count(Notification.id)).where(Notification.read_at.is_(None)))
    return UnreadCountOut(count=count or 0)


@router.post("/notifications/read-all", response_model=MessageOut)
def mark_all_read(db: Session = Depends(get_db)) -> MessageOut:
    """Mark every unread notification as read."""
    result = db.execute(
        update(Notification).where(Notification.read_at.is_(None)).values(read_at=utcnow())
    )
    db.commit()
    count = result.rowcount or 0
    return MessageOut(message=f"{count} notification{'' if count == 1 else 's'} marked as read")


@router.post("/notifications/{notification_id}/read", response_model=NotificationOut)
def mark_read(notification_id: int, db: Session = Depends(get_db)) -> Notification:
    """Mark one notification as read (idempotent: the first read time is kept)."""
    notification = get_or_404(db, Notification, notification_id, "Notification")
    if notification.read_at is None:
        notification.read_at = utcnow()
        db.commit()
    return notification
