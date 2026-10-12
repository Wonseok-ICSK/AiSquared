"""ntfy push channel (https://ntfy.sh or self-hosted; DESIGN §13.3).

Publishes as JSON (``POST {server}/`` with ``{topic, title, message, click, tags, priority}``)
rather than via ``Title``/``Click`` headers: header values must be ASCII/latin-1, which
breaks on Korean text. On ntfy.sh the topic name is effectively the password - pick an
unguessable one (e.g. ``aisq-`` + random characters). Free tier: 250 messages/day per IP,
4,096-byte message body. An optional access token is sent as ``Authorization: Bearer``.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from app.services.events import NotificationMessage
from app.services.notifications.base import (
    ChannelError,
    NotificationChannel,
    is_http_url,
    redact,
    register_channel,
    secret_value,
    subject_line,
    truncate_text,
    truncate_utf8,
)
from app.settings_store import RuntimeSettings

logger = logging.getLogger(__name__)

DEFAULT_SERVER = "https://ntfy.sh"
MESSAGE_LIMIT_BYTES = 4096
TITLE_LIMIT = 250
TIMEOUT_S = 15.0
TOPIC_RE = re.compile(r"^[-_A-Za-z0-9]{1,64}$")
_PRIORITY = {"price_alert": 4}
_TAGS = {"price_alert": ["airplane"], "itinerary": ["spiral_calendar"], "system": ["bell"]}


def build_payload(topic: str, message: NotificationMessage) -> dict[str, Any]:
    """JSON body for ntfy's publish-as-JSON endpoint."""
    kind = str(message.data.get("kind") or "system")
    payload: dict[str, Any] = {
        "topic": topic,
        "title": truncate_text(subject_line(message), TITLE_LIMIT),
        "message": truncate_utf8(message.body or message.title, MESSAGE_LIMIT_BYTES),
        "tags": _TAGS.get(kind, ["bell"]),
        "priority": _PRIORITY.get(kind, 3),
    }
    if is_http_url(message.url):
        payload["click"] = message.url
        payload["actions"] = [{"action": "view", "label": message.url_label, "url": message.url}]
    return payload


def publish(
    server: str, topic: str, message: NotificationMessage, *, token: str | None = None
) -> dict[str, Any]:
    """Publish ``message`` to ``topic`` on ``server``; returns ntfy's JSON reply."""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    url = server.rstrip("/") + "/"
    try:
        response = httpx.post(
            url, json=build_payload(topic, message), headers=headers, timeout=TIMEOUT_S
        )
    except httpx.HTTPError as exc:
        raise ChannelError(
            f"cannot reach {server}: {type(exc).__name__}: {redact(str(exc), token)}"
        ) from None
    if response.status_code == 200:
        try:
            reply = response.json()
        except ValueError:
            reply = {}
        return reply if isinstance(reply, dict) else {}
    raise ChannelError(_error_text(response, token))


def _error_text(response: httpx.Response, token: str | None) -> str:
    try:
        detail = str(response.json().get("error") or "")
    except (ValueError, AttributeError):
        detail = ""
    status = response.status_code
    if status in (401, 403):
        hint = "access denied - check the access token / topic permissions"
    elif status == 429:
        hint = "rate limited (ntfy.sh free tier: 250 messages/day)"
    elif status == 413:
        hint = "message too large"
    else:
        hint = detail or response.reason_phrase or "error"
    return redact(f"ntfy HTTP {status}: {hint}", token)


class NtfyChannel(NotificationChannel):
    """Push notification through an ntfy topic (Android/iOS ntfy app)."""

    key = "ntfy"
    display_name = "ntfy"
    description = "Phone push via the free ntfy app; subscribe to your private topic."

    def is_configured(self, rs: RuntimeSettings) -> tuple[bool, str]:
        """Needs an http(s) server and a valid topic name."""
        cfg = rs.notifications.ntfy
        if not is_http_url(cfg.server or DEFAULT_SERVER):
            return False, "server must be an http(s) URL"
        topic = (cfg.topic or "").strip()
        if not topic:
            return False, "topic is not set"
        if not TOPIC_RE.match(topic):
            return False, "topic may only contain letters, digits, '-' and '_' (max 64)"
        return True, ""

    def send(self, message: NotificationMessage, rs: RuntimeSettings) -> None:
        """Publish ``message`` to the configured topic."""
        configured, why = self.is_configured(rs)
        if not configured:
            raise ChannelError(why)
        cfg = rs.notifications.ntfy
        publish(
            cfg.server or DEFAULT_SERVER,
            (cfg.topic or "").strip(),
            message,
            token=secret_value(cfg.token),
        )


register_channel(NtfyChannel())
