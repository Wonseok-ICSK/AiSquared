"""Notification channel abstraction, channel registry and small text helpers (DESIGN §12).

A channel turns a channel-independent :class:`~app.services.events.NotificationMessage`
into one delivery (toast, Telegram message, e-mail, ...). Channels are stateless
singletons registered in :data:`CHANNELS`; the dispatcher iterates them in the order of
``schemas.NOTIFICATION_CHANNELS`` (inapp, desktop, telegram, email, ntfy, kakao).

Contract for :meth:`NotificationChannel.send`: return normally on success, raise
:class:`ChannelError` with a short, user-presentable reason (never containing secrets) on
failure. The dispatcher additionally guards against any other exception.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import ClassVar
from urllib.parse import urlparse

from pydantic import SecretStr

from app.config import get_settings
from app.schemas import NOTIFICATION_CHANNELS
from app.services.events import NotificationMessage
from app.settings_store import RuntimeSettings

logger = logging.getLogger(__name__)

ELLIPSIS = "…"
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


class ChannelError(Exception):
    """A channel could not deliver a message; ``str(err)`` is safe to show and store."""


class NotificationChannel(ABC):
    """One delivery channel. Subclasses set ``key``/``display_name`` and implement ``send``."""

    key: ClassVar[str]
    display_name: ClassVar[str]
    description: ClassVar[str] = ""

    def is_enabled(self, rs: RuntimeSettings) -> bool:
        """Whether the user switched this channel on (``notifications.<key>.enabled``)."""
        section = getattr(rs.notifications, self.key, None)
        return bool(getattr(section, "enabled", False))

    @abstractmethod
    def is_configured(self, rs: RuntimeSettings) -> tuple[bool, str]:
        """``(True, "")`` when everything needed to send is present, else ``(False, why)``."""

    @abstractmethod
    def send(self, message: NotificationMessage, rs: RuntimeSettings) -> None:
        """Deliver ``message``; raise :class:`ChannelError` on failure."""


# --------------------------------------------------------------------------- registry

CHANNELS: dict[str, NotificationChannel] = {}
"""Registered channel singletons by key, always in the canonical order of
``schemas.NOTIFICATION_CHANNELS`` whatever the import order (filled on import)."""


def register_channel(channel: NotificationChannel) -> NotificationChannel:
    """Add ``channel`` to :data:`CHANNELS` (its key must be a known channel name).

    The dict is re-ordered in place (it is exported by identity) so iterating it yields
    inapp, desktop, telegram, email, ntfy, kakao.
    """
    if channel.key not in NOTIFICATION_CHANNELS:
        raise ValueError(f"unknown notification channel {channel.key!r}")
    registered = CHANNELS | {channel.key: channel}
    CHANNELS.clear()
    CHANNELS.update({key: registered[key] for key in NOTIFICATION_CHANNELS if key in registered})
    return channel


def list_channels() -> list[NotificationChannel]:
    """Registered channels in the canonical order (inapp, desktop, telegram, email, ntfy, kakao)."""
    return [CHANNELS[key] for key in NOTIFICATION_CHANNELS if key in CHANNELS]


def get_channel(key: str) -> NotificationChannel:
    """The channel registered under ``key`` (``KeyError`` if unknown)."""
    try:
        return CHANNELS[key]
    except KeyError:
        raise KeyError(
            f"unknown notification channel {key!r}; expected one of {', '.join(CHANNELS)}"
        ) from None


# --------------------------------------------------------------------------- helpers


def secret_value(value: SecretStr | None) -> str | None:
    """Plain value of an optional ``SecretStr`` (``None`` when unset or blank)."""
    if value is None:
        return None
    plain = value.get_secret_value().strip()
    return plain or None


def is_http_url(url: str | None) -> bool:
    """True for an absolute ``http://`` or ``https://`` URL with a host."""
    if not url:
        return False
    parsed = urlparse(url)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def is_local_url(url: str | None) -> bool:
    """True for http(s) URLs pointing at this machine (only openable on this PC)."""
    return is_http_url(url) and (urlparse(url or "").hostname or "") in _LOCAL_HOSTS


def app_url(path: str = "/") -> str:
    """Absolute URL of a page of the local web app (``http://127.0.0.1:8765/...``)."""
    settings = get_settings()
    host = settings.host if settings.host not in ("0.0.0.0", "::", "") else "127.0.0.1"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}:{settings.port}/{path.lstrip('/')}"


def _cut_at_boundary(window: str) -> str:
    """``window`` cut back to its last line break / space if that keeps >= 60 % of it."""
    for separator in ("\n", " "):
        cut = window.rfind(separator)
        if cut >= len(window) * 0.6:
            return window[:cut].rstrip()
    return window.rstrip()


def truncate_text(text: str, limit: int, ellipsis: str = ELLIPSIS) -> str:
    """Shorten ``text`` to at most ``limit`` characters at a line/word boundary when possible."""
    if len(text) <= limit:
        return text
    return _cut_at_boundary(text[: max(limit - len(ellipsis), 0)]) + ellipsis


def truncate_utf8(text: str, max_bytes: int, ellipsis: str = ELLIPSIS) -> str:
    """Shorten ``text`` so that its UTF-8 encoding fits in ``max_bytes`` bytes."""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    budget = max(max_bytes - len(ellipsis.encode("utf-8")), 0)
    clipped = encoded[:budget].decode("utf-8", errors="ignore")
    return _cut_at_boundary(clipped) + ellipsis


def full_text(message: NotificationMessage) -> str:
    """Title, blank line, body — the plain-text rendering used by text channels."""
    return f"{message.title}\n\n{message.body}".rstrip() if message.body else message.title


def subject_line(message: NotificationMessage) -> str:
    """One-line headline: ``data["subject"]`` (lists alert reasons) or the title."""
    subject = message.data.get("subject")
    return str(subject) if subject else message.title


def summary_text(message: NotificationMessage) -> str:
    """Short multi-line summary (``data["summary"]``) for space-limited channels, else the body."""
    summary = message.data.get("summary")
    return str(summary) if summary else message.body


def redact(text: str, *secrets: str | None) -> str:
    """Replace every non-empty secret occurring in ``text`` with ``***``."""
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text
