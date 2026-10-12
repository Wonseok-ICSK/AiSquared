"""Telegram Bot API channel (DESIGN §13.3).

Setup: create a bot with @BotFather (``/newbot`` -> token), open the bot and press *Start*
(bots cannot message a user first), then :func:`detect_chat_id` reads the chat id from
``getUpdates``. Telegram keeps undelivered updates for 24 h only, so detection must happen
within a day of ``/start`` (or the user sends another message). ``getUpdates`` fails with
409 while a webhook is set.

Messages are plain text (no ``parse_mode``, so nothing needs escaping). Public http(s) links
become an inline URL button; other links (``localhost`` app pages, non-http schemes) are
appended as text because Telegram rejects such button URLs.

The bot token is part of every request URL: it is redacted from all errors and never logged.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.services.events import NotificationMessage
from app.services.notifications.base import (
    ChannelError,
    NotificationChannel,
    full_text,
    is_http_url,
    is_local_url,
    redact,
    register_channel,
    secret_value,
    truncate_text,
)
from app.settings_store import RuntimeSettings

logger = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"
TEXT_LIMIT = 4096
TIMEOUT_S = 15.0
_UPDATE_KINDS = (
    "message",
    "edited_message",
    "channel_post",
    "edited_channel_post",
    "my_chat_member",
)


class TelegramApiError(ChannelError):
    """Bot API returned ``ok: false`` (``error_code`` = Telegram's code or the HTTP status)."""

    def __init__(self, message: str, *, error_code: int, description: str) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.description = description


def _api_error(status: int, data: dict[str, Any], token: str) -> TelegramApiError:
    code = int(data.get("error_code") or status)
    description = redact(str(data.get("description") or f"HTTP {status}"), token)
    lowered = description.lower()
    if code in (401, 404):
        hint = "invalid bot token - copy it again from @BotFather"
    elif code == 403:
        hint = "the bot cannot message this chat - open the bot, press Start / unblock it"
    elif code == 400 and "chat not found" in lowered:
        hint = "chat not found - send /start to your bot, then use 'Detect chat ID'"
    elif code == 409:
        hint = "a webhook is set for this bot; getUpdates does not work until it is deleted"
    elif code == 429:
        retry = (data.get("parameters") or {}).get("retry_after")
        hint = f"rate limited by Telegram; retry after {retry} s" if retry else "rate limited"
    else:
        hint = description
    return TelegramApiError(
        f"Telegram error {code}: {hint}", error_code=code, description=description
    )


def _call(token: str, method: str, *, payload: dict[str, Any] | None = None) -> Any:
    """Call a Bot API method (POST JSON) and return its ``result``."""
    url = f"{API_BASE}/bot{token}/{method}"
    try:
        response = httpx.post(url, json=payload or {}, timeout=TIMEOUT_S)
    except httpx.HTTPError as exc:
        raise ChannelError(
            f"cannot reach Telegram: {type(exc).__name__}: {redact(str(exc), token)}"
        ) from None
    try:
        data = response.json()
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    if response.status_code == 200 and data.get("ok"):
        return data.get("result")
    raise _api_error(response.status_code, data, token)


def send_message(
    bot_token: str,
    chat_id: str,
    text: str,
    *,
    url: str | None = None,
    url_label: str = "Open",
) -> None:
    """Send a plain-text message; a public http(s) ``url`` becomes an inline button."""
    use_button = is_http_url(url) and not is_local_url(url)
    payload = _message_payload(chat_id, text, url, url_label, use_button)
    try:
        _call(bot_token, "sendMessage", payload=payload)
    except TelegramApiError as err:
        if not (use_button and err.error_code == 400 and _is_button_error(err.description)):
            raise
        logger.info("Telegram rejected the link button; resending with the link as text")
        payload = _message_payload(chat_id, text, url, url_label, False)
        _call(bot_token, "sendMessage", payload=payload)


def _is_button_error(description: str) -> bool:
    lowered = description.lower()
    return "button" in lowered or "url" in lowered


def _message_payload(
    chat_id: str, text: str, url: str | None, url_label: str, use_button: bool
) -> dict[str, Any]:
    suffix = f"\n\n{url_label}: {url}" if url and not use_button else ""
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": truncate_text(text, TEXT_LIMIT - len(suffix)) + suffix,
        "link_preview_options": {"is_disabled": True},
    }
    if use_button:
        payload["reply_markup"] = {"inline_keyboard": [[{"text": url_label, "url": url}]]}
    return payload


def detect_chat_id(bot_token: str) -> dict[str, str | None]:
    """``{"chat_id", "username"}`` of the newest chat that wrote to the bot (via ``getUpdates``).

    Raises :class:`ChannelError` with setup guidance when no update is available.
    Updates are only read, not confirmed, so this can be repeated safely.
    """
    if not bot_token or not bot_token.strip():
        raise ChannelError("enter the bot token from @BotFather first")
    updates = _call(bot_token.strip(), "getUpdates", payload={"timeout": 0})
    chats = [chat for chat in map(_update_chat, reversed(updates or [])) if chat]
    if not chats:
        raise ChannelError(
            "no messages found for this bot - open your bot in Telegram, send /start "
            "(or any message), then try again; Telegram keeps updates only for 24 h"
        )
    chat = next((c for c in chats if c.get("type") == "private"), chats[0])
    return {"chat_id": str(chat["id"]), "username": _chat_name(chat)}


def _update_chat(update: Any) -> dict[str, Any] | None:
    if not isinstance(update, dict):
        return None
    for kind in _UPDATE_KINDS:
        chat = (update.get(kind) or {}).get("chat")
        if isinstance(chat, dict) and "id" in chat:
            return chat
    return None


def _chat_name(chat: dict[str, Any]) -> str | None:
    if chat.get("username"):
        return str(chat["username"])
    name = " ".join(str(chat[k]) for k in ("first_name", "last_name") if chat.get(k))
    return name or (str(chat["title"]) if chat.get("title") else None)


class TelegramChannel(NotificationChannel):
    """Telegram bot message with an inline "open booking page" button."""

    key = "telegram"
    display_name = "Telegram"
    description = "Push message from your own Telegram bot (phone + desktop)."

    def is_configured(self, rs: RuntimeSettings) -> tuple[bool, str]:
        """Needs the bot token and the chat id."""
        cfg = rs.notifications.telegram
        if not secret_value(cfg.bot_token):
            return False, "bot token is not set (create a bot with @BotFather)"
        if not (cfg.chat_id or "").strip():
            return False, "chat ID is not set (send /start to your bot, then 'Detect chat ID')"
        return True, ""

    def send(self, message: NotificationMessage, rs: RuntimeSettings) -> None:
        """Send ``message`` to the configured chat."""
        cfg = rs.notifications.telegram
        token = secret_value(cfg.bot_token)
        if not token or not cfg.chat_id:
            raise ChannelError("Telegram is not configured")
        send_message(
            token,
            cfg.chat_id.strip(),
            full_text(message),
            url=message.url,
            url_label=message.url_label,
        )


register_channel(TelegramChannel())
