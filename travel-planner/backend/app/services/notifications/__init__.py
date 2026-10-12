"""Notification service (DESIGN §11-§13.3).

Channels (in dispatch order): ``inapp`` (the DB row), ``desktop`` (Windows toast),
``telegram``, ``email`` (SMTP STARTTLS), ``ntfy``, ``kakao`` (KakaoTalk "send to me").
Importing this package registers all channels in :data:`base.CHANNELS`.

Typical use::

    from app.services.notifications import notify_price_alert
    notify_price_alert(db, trip, run, best_offer, events)   # monitoring, after evaluate_alerts
    notify_itinerary_ready(db, trip, itinerary)             # itinerary service, when done/failed
    notify(db, kind="system", message=NotificationMessage(title=..., body=...))
"""

from __future__ import annotations

# Channel modules register themselves on import, listed in the canonical order
# (base.register_channel also enforces that order in CHANNELS).
# isort: off
from app.services.notifications import (  # noqa: F401
    inapp,
    desktop,
    telegram,
    email,
    ntfy,
    kakao,
)

# isort: on
from app.services.notifications.base import (
    CHANNELS,
    ChannelError,
    NotificationChannel,
    get_channel,
    list_channels,
)
from app.services.notifications.dispatcher import (
    channel_statuses,
    notify,
    notify_itinerary_ready,
    notify_price_alert,
    send_test,
)
from app.services.notifications.formatter import (
    format_itinerary_ready,
    format_price_alert,
    format_test_message,
)

__all__ = [
    "CHANNELS",
    "ChannelError",
    "NotificationChannel",
    "channel_statuses",
    "format_itinerary_ready",
    "format_price_alert",
    "format_test_message",
    "get_channel",
    "list_channels",
    "notify",
    "notify_itinerary_ready",
    "notify_price_alert",
    "send_test",
]
