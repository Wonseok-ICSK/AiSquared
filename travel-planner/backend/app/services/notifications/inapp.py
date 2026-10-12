"""In-app channel: the ``Notification`` row the dispatcher always writes *is* the delivery.

The SPA shows these rows in its bell/inbox (``GET /api/notifications``), so sending is a
no-op and the channel is always enabled and configured.
"""

from __future__ import annotations

from app.services.events import NotificationMessage
from app.services.notifications.base import NotificationChannel, register_channel
from app.settings_store import RuntimeSettings


class InAppChannel(NotificationChannel):
    """Inbox inside the web app (source of truth / audit log of every alert)."""

    key = "inapp"
    display_name = "In-app"
    description = "Bell and inbox in the web app; always on."

    def is_enabled(self, rs: RuntimeSettings) -> bool:
        """Always on."""
        return True

    def is_configured(self, rs: RuntimeSettings) -> tuple[bool, str]:
        """Needs no configuration."""
        return True, ""

    def send(self, message: NotificationMessage, rs: RuntimeSettings) -> None:
        """No-op: the dispatcher persists the ``Notification`` row itself."""
        return None


register_channel(InAppChannel())
