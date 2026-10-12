"""Runtime settings, notification channel tests and Telegram chat detection (DESIGN §7, §8).

* ``GET /settings`` — effective runtime settings with secrets redacted (``{is_set, source}``).
* ``PUT /settings`` — partial update (:class:`RuntimeSettingsIn`): non-secret ``null`` = reset
  to env/default, secret omitted/``null`` = unchanged, ``""`` = clear. A changed browser
  channel / headless mode is pushed to the shared BrowserManager.
* ``POST /settings/notifications/test`` — one test message through one channel.
* ``POST /settings/telegram/detect-chat`` — reads ``getUpdates`` with the saved bot token
  and stores the chat id found.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.browser.manager import get_browser_manager
from app.db import get_db
from app.providers.flights import registry
from app.schemas import (
    NotificationTestIn,
    NotificationTestOut,
    OutModel,
    RuntimeSettingsIn,
    RuntimeSettingsOut,
)
from app.services.notifications import dispatcher, telegram
from app.services.notifications.base import ChannelError, secret_value
from app.settings_store import (
    RuntimeSettings,
    load_runtime_settings,
    save_runtime_settings,
    to_public,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["settings"])

_BROWSER_FIELDS = ("browser_channel", "browser_headless")


class TelegramDetectOut(OutModel):
    """``POST /api/settings/telegram/detect-chat`` result."""

    chat_id: str | None = None
    username: str | None = None


def apply_browser_settings(runtime: RuntimeSettings) -> None:
    """Push the runtime browser channel / headless default to the shared BrowserManager.

    Never raises (an invalid channel is logged). Call it at startup too, so UI-saved values
    win over ``BROWSER_CHANNEL``/``BROWSER_HEADLESS`` from the environment.
    """
    try:
        get_browser_manager().configure(
            channel=runtime.browser_channel, headless=runtime.browser_headless
        )
    except Exception as exc:  # noqa: BLE001 - settings are saved regardless
        logger.warning("Could not apply the browser settings: %s", exc)


def _validation_detail(exc: ValidationError) -> list[dict[str, Any]]:
    """Pydantic errors in FastAPI's 422 shape (``loc`` prefixed with ``body``)."""
    return [
        {"loc": ["body", *error["loc"]], "msg": error["msg"], "type": error["type"]}
        for error in exc.errors(include_url=False, include_context=False, include_input=False)
    ]


def _check_provider_keys(body: RuntimeSettingsIn) -> None:
    if not body.default_providers:
        return
    known = {cls.key for cls in registry.list_provider_classes()}
    unknown = [key for key in dict.fromkeys(body.default_providers) if key not in known]
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=[
                {
                    "loc": ["body", "default_providers"],
                    "msg": f"unknown flight provider(s): {', '.join(unknown)}",
                    "type": "value_error",
                }
            ],
        )


@router.get("/settings", response_model=RuntimeSettingsOut)
def read_settings(db: Session = Depends(get_db)) -> RuntimeSettingsOut:
    """Effective runtime settings (env defaults + saved UI values), secrets redacted."""
    return to_public(load_runtime_settings(db))


@router.put("/settings", response_model=RuntimeSettingsOut)
def update_settings(body: RuntimeSettingsIn, db: Session = Depends(get_db)) -> RuntimeSettingsOut:
    """Apply a partial settings update and return the new effective (redacted) settings."""
    _check_provider_keys(body)
    before = load_runtime_settings(db)
    try:
        after = save_runtime_settings(db, body)
    except ValidationError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=_validation_detail(exc)) from None
    browser_changed = any(
        getattr(before, name) != getattr(after, name) for name in _BROWSER_FIELDS
    )
    if browser_changed or body.model_fields_set & set(_BROWSER_FIELDS):
        apply_browser_settings(after)
    return to_public(after)


@router.post("/settings/notifications/test", response_model=NotificationTestOut)
def test_notification(body: NotificationTestIn, db: Session = Depends(get_db)) -> NotificationTestOut:
    """Send a test message via ``channel`` (ignores its enabled switch, not its config).

    ``result``: ``"sent"`` | ``"skipped: <why>"`` | ``"failed: <reason>"``.
    """
    result = dispatcher.send_test(db, body.channel)
    return NotificationTestOut(channel=body.channel, result=result)


@router.post("/settings/telegram/detect-chat", response_model=TelegramDetectOut)
def detect_telegram_chat(db: Session = Depends(get_db)) -> TelegramDetectOut:
    """Find the chat that sent ``/start`` to the bot (``getUpdates``) and save its id."""
    token = secret_value(load_runtime_settings(db).notifications.telegram.bot_token)
    if not token:
        raise HTTPException(
            status_code=400,
            detail=(
                "Telegram bot token is not set: create a bot with @BotFather, paste its token "
                "here and save it, send /start to the bot, then detect the chat ID."
            ),
        )
    try:
        found = telegram.detect_chat_id(token)
    except ChannelError as exc:
        raise HTTPException(status_code=400, detail=f"Could not detect the chat ID: {exc}") from None
    chat_id = found.get("chat_id")
    if chat_id:
        save_runtime_settings(db, {"notifications": {"telegram": {"chat_id": str(chat_id)}}})
        logger.info("Telegram chat id detected and saved")
    return TelegramDetectOut(chat_id=chat_id, username=found.get("username"))
