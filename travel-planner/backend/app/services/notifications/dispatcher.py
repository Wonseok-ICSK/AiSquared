"""Notification fan-out (DESIGN §11-§12).

:func:`notify` always writes the in-app ``Notification`` row first (committed, so the inbox
has it even if a channel hangs), then sends to every *enabled and configured* channel in
parallel. Each channel's outcome is stored in ``Notification.channel_results``:

* ``"sent"``
* ``"skipped: <why>"`` - disabled, or not configured (reason from ``is_configured``)
* ``"failed: <reason>"`` - the channel raised; other channels are unaffected

Dispatch never raises. :func:`notify` and :func:`notify_price_alert` commit ``db`` (including
anything the caller had pending), because the row must be durable before external sends.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor, wait
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import FlightOffer, Itinerary, Notification, SearchRun, Trip
from app.schemas import NOTIFICATION_CHANNELS
from app.services.events import AlertEvent, NotificationKind, NotificationMessage
from app.services.notifications.base import (
    ChannelError,
    NotificationChannel,
    get_channel,
    list_channels,
    truncate_text,
)
from app.services.notifications.formatter import (
    TEXT,
    format_amount,
    format_itinerary_ready,
    format_price_alert,
    format_test_message,
)
from app.settings_store import RuntimeSettings, load_runtime_settings

logger = logging.getLogger(__name__)

SEND_TIMEOUT_S = 90.0
"""Upper bound for the whole parallel fan-out (each channel also has its own timeouts)."""
REASON_LIMIT = 300
TITLE_LIMIT = 300
SENT = "sent"
_KIND_ORDER = {"below_target": 0, "price_drop": 1, "new_low": 2}


# --------------------------------------------------------------------------- helpers


def _load_settings(db: Session) -> RuntimeSettings:
    try:
        return load_runtime_settings(db)
    except Exception:  # noqa: BLE001 - fall back to defaults (in-app + desktop) rather than fail
        logger.exception("Could not load runtime settings; using defaults for notifications")
        db.rollback()
        return RuntimeSettings()


def _failed(reason: str) -> str:
    return f"failed: {truncate_text(reason.strip() or 'unknown error', REASON_LIMIT)}"


def _precheck(channel: NotificationChannel, rs: RuntimeSettings) -> str | None:
    """``None`` when the channel should be sent to, else its ``skipped``/``failed`` result."""
    try:
        if not channel.is_enabled(rs):
            return "skipped: disabled"
        configured, why = channel.is_configured(rs)
    except Exception as exc:  # noqa: BLE001 - a broken check must not break the fan-out
        logger.exception("Notification channel %s: status check failed", channel.key)
        return _failed(f"{type(exc).__name__}: {exc}")
    return None if configured else f"skipped: {why or 'not configured'}"


def _deliver(
    channel: NotificationChannel, message: NotificationMessage, rs: RuntimeSettings
) -> str:
    """Send through one channel and translate the outcome into a result string."""
    try:
        channel.send(message, rs)
    except ChannelError as err:
        logger.warning("Notification channel %s failed: %s", channel.key, err)
        return _failed(str(err))
    except Exception as exc:  # noqa: BLE001 - one channel must never break the others
        logger.exception("Notification channel %s crashed", channel.key)
        return _failed(f"{type(exc).__name__}: {exc}")
    return SENT


def _send_external(message: NotificationMessage, rs: RuntimeSettings) -> dict[str, str]:
    """Fan out to every channel except in-app; returns ``{channel: result}``."""
    results: dict[str, str] = {}
    targets: list[NotificationChannel] = []
    for channel in list_channels():
        if channel.key == "inapp":
            continue
        verdict = _precheck(channel, rs)
        if verdict is None:
            targets.append(channel)
        else:
            results[channel.key] = verdict
    if not targets:
        return results
    pool = ThreadPoolExecutor(max_workers=len(targets), thread_name_prefix="notify")
    try:
        futures = {pool.submit(_deliver, channel, message, rs): channel.key for channel in targets}
        done, pending = wait(futures, timeout=SEND_TIMEOUT_S)
        for future in done:
            results[futures[future]] = future.result()
        for future in pending:
            logger.warning("Notification channel %s timed out", futures[future])
            results[futures[future]] = _failed(f"timed out after {SEND_TIMEOUT_S:g} s")
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return results


def _ordered(results: dict[str, str]) -> dict[str, str]:
    order = {key: index for index, key in enumerate(NOTIFICATION_CHANNELS)}
    return dict(sorted(results.items(), key=lambda item: order.get(item[0], len(order))))


def _json_safe(data: dict[str, Any]) -> dict[str, Any]:
    try:
        return json.loads(json.dumps(data, default=str))
    except (TypeError, ValueError):
        logger.warning("Notification data is not JSON-serializable; dropped")
        return {}


def _persist_row(db: Session, notification: Notification) -> str:
    """Insert + commit the in-app row; returns the ``inapp`` result."""
    try:
        db.add(notification)
        db.commit()
    except Exception as exc:  # noqa: BLE001 - external channels still get the message
        db.rollback()
        logger.exception("Could not store in-app notification %r", notification.title)
        return _failed(f"database error: {type(exc).__name__}")
    return SENT


def _save_results(db: Session, notification: Notification, results: dict[str, str]) -> None:
    notification.channel_results = results
    if notification.id is None:
        return
    try:
        db.commit()
    except Exception:  # noqa: BLE001 - the notification itself is already stored
        db.rollback()
        logger.exception("Could not store channel results of notification %s", notification.id)


def _dispatch(
    db: Session,
    rs: RuntimeSettings,
    *,
    kind: NotificationKind,
    message: NotificationMessage,
    trip_id: int | None,
    alert_rule_id: int | None,
) -> Notification:
    """Store the row, fan out, save the results. Never raises."""
    try:
        return _dispatch_unguarded(
            db, rs, kind=kind, message=message, trip_id=trip_id, alert_rule_id=alert_rule_id
        )
    except Exception as exc:  # noqa: BLE001 - last-resort guard: dispatch never raises
        logger.exception("Notification dispatch failed")
        return Notification(
            trip_id=trip_id,
            alert_rule_id=alert_rule_id,
            kind=kind,
            title=message.title,
            body=message.body,
            url=message.url,
            channel_results={"inapp": _failed(f"{type(exc).__name__}: {exc}")},
        )


def _dispatch_unguarded(
    db: Session,
    rs: RuntimeSettings,
    *,
    kind: NotificationKind,
    message: NotificationMessage,
    trip_id: int | None,
    alert_rule_id: int | None,
) -> Notification:
    message = dataclasses.replace(message, data={**message.data, "kind": kind})
    notification = Notification(
        trip_id=trip_id,
        alert_rule_id=alert_rule_id,
        kind=kind,
        title=truncate_text(message.title, TITLE_LIMIT),
        body=message.body,
        url=message.url,
        data=_json_safe(message.data),
    )
    results = {"inapp": _persist_row(db, notification)}
    results |= _send_external(message, rs)
    results = _ordered(results)
    _save_results(db, notification, results)
    logger.info(
        "Notification %s (%s) %r: %s",
        notification.id,
        kind,
        notification.title,
        ", ".join(f"{key}={value}" for key, value in results.items()),
    )
    return notification


# --------------------------------------------------------------------------- public API


def notify(
    db: Session,
    *,
    kind: NotificationKind,
    message: NotificationMessage,
    trip_id: int | None = None,
    alert_rule_id: int | None = None,
) -> Notification:
    """Store ``message`` as an in-app notification and send it to all active channels.

    Never raises; per-channel outcomes are in the returned row's ``channel_results``.
    Commits ``db``.
    """
    rs = _load_settings(db)
    return _dispatch(
        db, rs, kind=kind, message=message, trip_id=trip_id, alert_rule_id=alert_rule_id
    )


def _previous_lowest(db: Session, trip: Trip, run: SearchRun | None) -> float | None:
    """Lowest best price (trip basis) over this trip's earlier finished runs, same currency."""
    if run is None or run.id is None or trip.id is None:
        return None
    column = (
        SearchRun.min_price_total if trip.price_basis == "total" else SearchRun.min_price_per_person
    )
    stmt = select(func.min(column)).where(
        SearchRun.trip_id == trip.id,
        SearchRun.id < run.id,
        SearchRun.status.in_(("ok", "partial")),
        SearchRun.currency == run.currency,
        column.is_not(None),
    )
    try:
        return db.scalar(stmt)
    except Exception:  # noqa: BLE001 - the alert is still useful without this line
        logger.exception("Could not compute the previous lowest price of trip %s", trip.id)
        db.rollback()
        return None


def _fallback_alert(
    trip: Trip, best_offer: FlightOffer | None, events: Sequence[AlertEvent], language: str
) -> NotificationMessage:
    """Minimal alert used if the formatter fails (should not happen)."""
    event = events[0]
    table = TEXT.get(language, TEXT["en"])
    url = (best_offer.booking_url or best_offer.source_url) if best_offer else None
    lines = [f"{trip.origin} → {trip.destination}", format_amount(event.price, event.currency)]
    lines += [e.reason for e in events if e.reason]
    return NotificationMessage(
        title=table["alert_title"],
        body="\n".join(lines),
        url=url,
        url_label=table["open_booking"],
        data={"kind": "price_alert", "trip_id": trip.id, "rule_ids": [e.rule_id for e in events]},
    )


def notify_price_alert(
    db: Session,
    trip: Trip,
    run: SearchRun | None,
    best_offer: FlightOffer | None,
    events: Sequence[AlertEvent],
) -> Notification | None:
    """Merge all alert ``events`` of one run into ONE notification and dispatch it.

    Returns ``None`` when ``events`` is empty. The notification's ``alert_rule_id`` is the
    primary event's rule (below_target > price_drop > new_low); all rule ids are in
    ``data["rule_ids"]``. Never raises; commits ``db``.
    """
    if not events:
        return None
    ordered = sorted(events, key=lambda e: _KIND_ORDER.get(e.kind, 9))
    rs = _load_settings(db)
    try:
        known = any(event.previous_lowest is not None for event in ordered)
        previous_lowest = None if known else _previous_lowest(db, trip, run)
        message = format_price_alert(
            trip, run, best_offer, ordered, rs.language, previous_lowest=previous_lowest
        )
    except Exception:  # noqa: BLE001 - still alert the user with a plain message
        logger.exception("Formatting the price alert for trip %s failed", trip.id)
        message = _fallback_alert(trip, best_offer, ordered, rs.language)
    return _dispatch(
        db,
        rs,
        kind="price_alert",
        message=message,
        trip_id=trip.id,
        alert_rule_id=ordered[0].rule_id,
    )


def notify_itinerary_ready(db: Session, trip: Trip, itinerary: Itinerary) -> Notification:
    """Notify that ``itinerary`` finished (or failed), in the runtime language. Never raises."""
    language = _load_settings(db).language
    try:
        message = format_itinerary_ready(trip, itinerary, language)
    except Exception:  # noqa: BLE001 - still tell the user something happened
        logger.exception("Formatting the itinerary notification for trip %s failed", trip.id)
        message = NotificationMessage(title=f"Itinerary {itinerary.status}", body=trip.name or "")
    return notify(db, kind="itinerary", message=message, trip_id=trip.id)


def send_test(db: Session, channel: str) -> str:
    """Send a test message through one channel: ``"sent"`` | ``"skipped: …"`` | ``"failed: …"``.

    Ignores the channel's ``enabled`` switch (so it can be tested before enabling) but not
    its configuration. The ``inapp`` test stores a ``system`` notification in the inbox;
    other channels' tests are not stored.
    """
    try:
        target = get_channel(channel)
    except KeyError:
        return _failed(f"unknown channel {channel!r}")
    rs = _load_settings(db)
    message = format_test_message(rs.language)
    if target.key == "inapp":
        notification = Notification(
            kind="system",
            title=message.title,
            body=message.body,
            url=message.url,
            data=_json_safe(message.data),
        )
        result = _persist_row(db, notification)
        _save_results(db, notification, {"inapp": result})
        return result
    try:
        configured, why = target.is_configured(rs)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Notification channel %s: status check failed", target.key)
        return _failed(f"{type(exc).__name__}: {exc}")
    if not configured:
        return f"skipped: {why or 'not configured'}"
    return _deliver(target, message, rs)


def channel_statuses(rs: RuntimeSettings) -> list[dict[str, Any]]:
    """Per-channel ``{key, display_name, description, enabled, configured, message}`` for the UI."""
    statuses = []
    for channel in list_channels():
        try:
            configured, why = channel.is_configured(rs)
            enabled = channel.is_enabled(rs)
        except Exception as exc:  # noqa: BLE001
            configured, why, enabled = False, f"{type(exc).__name__}: {exc}", False
        statuses.append(
            {
                "key": channel.key,
                "display_name": channel.display_name,
                "description": channel.description,
                "enabled": enabled,
                "configured": configured,
                "message": why,
            }
        )
    return statuses
