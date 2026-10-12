"""Dispatcher: in-app row, per-channel results, failure isolation, merged price alerts, tests."""

from __future__ import annotations

import json
import time
from datetime import date
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import AlertRule, FlightOffer, Itinerary, Notification, SearchRun, Trip
from app.services.events import AlertEvent, NotificationMessage
from app.services.notifications import desktop, dispatcher
from app.services.notifications.base import ChannelError, get_channel
from app.services.notifications.dispatcher import (
    channel_statuses,
    notify,
    notify_itinerary_ready,
    notify_price_alert,
    send_test,
)
from app.settings_store import load_runtime_settings, save_runtime_settings

MESSAGE = NotificationMessage(
    title="Hello", body="Body text", url="https://example.com/x", url_label="Open"
)
TOKEN = "123:tok"


@pytest.fixture(autouse=True)
def toasts(monkeypatch: pytest.MonkeyPatch) -> list[NotificationMessage]:
    """Never show real toasts: record them instead."""
    shown: list[NotificationMessage] = []
    monkeypatch.setattr(desktop, "_is_windows", lambda: True)
    monkeypatch.setattr(desktop, "toast_support", lambda: (True, ""))
    monkeypatch.setattr(desktop, "register_aumid", lambda icon_path=None: True)
    monkeypatch.setattr(desktop, "_show_toast", shown.append)
    return shown


def stored(db: Session, notification_id: int) -> Notification:
    db.expire_all()
    row = db.get(Notification, notification_id)
    assert row is not None
    return row


def enable_external(db: Session) -> None:
    save_runtime_settings(
        db,
        {
            "notifications": {
                "telegram": {"enabled": True, "bot_token": TOKEN, "chat_id": "42"},
                "ntfy": {"enabled": True, "topic": "aisq-topic"},
                "email": {
                    "enabled": True,
                    "smtp_host": "smtp.gmail.com",
                    "username": "me@gmail.com",
                    "password": "app-pw",
                    "to_addrs": ["me@gmail.com"],
                },
                "kakao": {"enabled": True},
            }
        },
    )


def test_notify_stores_row_and_reports_every_channel(
    db: Session, toasts: list[NotificationMessage]
) -> None:
    notification = notify(db, kind="system", message=MESSAGE)
    assert notification.id is not None
    row = stored(db, notification.id)
    assert (row.kind, row.title, row.body, row.url) == ("system", "Hello", "Body text", MESSAGE.url)
    assert row.data == {"kind": "system"}
    assert row.read_at is None
    assert list(row.channel_results.items()) == [
        ("inapp", "sent"),
        ("desktop", "sent"),
        ("telegram", "skipped: disabled"),
        ("email", "skipped: disabled"),
        ("ntfy", "skipped: disabled"),
        ("kakao", "skipped: disabled"),
    ]
    assert len(toasts) == 1 and toasts[0].data["kind"] == "system"
    assert MESSAGE.data == {}  # the caller's message is not mutated


def test_one_failure_never_blocks_others(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    enable_external(db)
    sent: list[str] = []

    def channel_error(message: NotificationMessage, rs: Any) -> None:
        raise ChannelError("Telegram error 403: blocked")

    def crash(message: NotificationMessage, rs: Any) -> None:
        raise RuntimeError("kaput")

    monkeypatch.setattr(get_channel("telegram"), "send", channel_error)
    monkeypatch.setattr(get_channel("ntfy"), "send", crash)
    monkeypatch.setattr(get_channel("email"), "send", lambda m, rs: sent.append(m.title))
    notification = notify(db, kind="system", message=MESSAGE)
    assert stored(db, notification.id).channel_results == {
        "inapp": "sent",
        "desktop": "sent",
        "telegram": "failed: Telegram error 403: blocked",
        "email": "sent",
        "ntfy": "failed: RuntimeError: kaput",
        "kakao": (
            "skipped: REST API key is not set (Kakao Developers > [앱] > [플랫폼 키])"
        ),
    }
    assert sent == ["Hello"]


def test_broken_status_check_is_isolated(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(rs: Any) -> tuple[bool, str]:
        raise ValueError("bad config")

    monkeypatch.setattr(get_channel("desktop"), "is_configured", broken)
    notification = notify(db, kind="system", message=MESSAGE)
    assert notification.channel_results["desktop"] == "failed: ValueError: bad config"
    assert notification.channel_results["inapp"] == "sent"


def test_slow_channel_times_out(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    enable_external(db)
    monkeypatch.setattr(dispatcher, "SEND_TIMEOUT_S", 0.3)
    monkeypatch.setattr(get_channel("telegram"), "send", lambda m, rs: time.sleep(1.5))
    monkeypatch.setattr(get_channel("ntfy"), "send", lambda m, rs: None)
    monkeypatch.setattr(get_channel("email"), "send", lambda m, rs: None)
    started = time.monotonic()
    notification = notify(db, kind="system", message=MESSAGE)
    assert time.monotonic() - started < 1.4
    results = stored(db, notification.id).channel_results
    assert results["telegram"] == "failed: timed out after 0.3 s"
    assert results["ntfy"] == "sent" and results["email"] == "sent"


def test_inapp_failure_still_sends_externally(
    db: Session, toasts: list[NotificationMessage]
) -> None:
    notification = notify(db, kind="system", message=MESSAGE, trip_id=999_999)  # FK violation
    assert notification.id is None
    assert notification.channel_results["inapp"].startswith("failed: database error")
    assert notification.channel_results["desktop"] == "sent"
    assert db.scalar(select(func.count()).select_from(Notification)) == 0


def test_unserializable_data_is_coerced(db: Session) -> None:
    message = NotificationMessage(title="T", body="B", data={"when": date(2026, 11, 20)})
    notification = notify(db, kind="system", message=message)
    assert stored(db, notification.id).data == {"when": "2026-11-20", "kind": "system"}


# --------------------------------------------------------------------------- price alerts


def seed_trip(db: Session) -> tuple[Trip, SearchRun, FlightOffer]:
    trip = Trip(
        name="Seoul → Tokyo",
        origin="SEL",
        destination="TYO",
        trip_type="round_trip",
        depart_date=date(2026, 11, 20),
        return_date=date(2026, 11, 23),
        adults=1,
        target_price=300_000.0,
        price_basis="per_person",
        currency="KRW",
    )
    db.add(trip)
    db.flush()
    for price, status in ((350_000.0, "ok"), (320_000.0, "partial"), (100_000.0, "error")):
        db.add(
            SearchRun(
                trip_id=trip.id,
                status=status,
                min_price_per_person=price,
                min_price_total=price,
                currency="KRW",
            )
        )
    run = SearchRun(trip_id=trip.id, status="ok", min_price_per_person=278_000.0, currency="KRW")
    db.add(run)
    db.flush()
    offer = FlightOffer(
        trip_id=trip.id,
        run_id=run.id,
        provider="mock",
        offer_key="ICN-NRT",
        origin="ICN",
        destination="NRT",
        depart_date=date(2026, 11, 20),
        return_date=date(2026, 11, 23),
        airline="Korean Air",
        flight_number="KE703",
        departure_airport="ICN",
        arrival_airport="NRT",
        departure_time="2026-11-20T09:05",
        arrival_time="2026-11-20T11:30",
        price_total=278_000.0,
        price_per_person=278_000.0,
        currency="KRW",
        booking_url="https://example.com/book",
    )
    db.add(offer)
    for kind in ("below_target", "price_drop", "new_low"):
        db.add(AlertRule(trip_id=trip.id, kind=kind, drop_percent=10.0))
    db.commit()
    return trip, run, offer


def alert(trip: Trip, run: SearchRun, kind: str, rule_id: int, **extra: Any) -> AlertEvent:
    return AlertEvent(
        rule_id=rule_id,
        kind=kind,  # type: ignore[arg-type]
        trip_id=trip.id,
        run_id=run.id,
        offer_id=None,
        price=278_000.0,
        basis="per_person",
        currency="KRW",
        **extra,
    )


def test_notify_price_alert_none_without_events(db: Session) -> None:
    trip, run, offer = seed_trip(db)
    assert notify_price_alert(db, trip, run, offer, []) is None
    assert db.scalar(select(func.count()).select_from(Notification)) == 0


def test_notify_price_alert_merges_events_into_one_notification(
    db: Session, toasts: list[NotificationMessage]
) -> None:
    trip, run, offer = seed_trip(db)
    rules = {r.kind: r.id for r in trip.alert_rules}
    events = [
        alert(trip, run, "new_low", rules["new_low"]),
        alert(trip, run, "price_drop", rules["price_drop"], previous_price=320_000.0,
              drop_percent=13.125),
        alert(trip, run, "below_target", rules["below_target"], threshold=300_000.0),
    ]
    notification = notify_price_alert(db, trip, run, offer, events)
    assert notification is not None
    assert db.scalar(select(func.count()).select_from(Notification)) == 1
    row = stored(db, notification.id)
    assert row.kind == "price_alert" and row.trip_id == trip.id
    assert row.alert_rule_id == rules["below_target"]
    assert row.title == "Flight Price Alert"
    assert row.url == "https://example.com/book"
    # previous lowest comes from earlier finished runs (error runs ignored)
    assert "Previous Lowest Price:\nKRW 320,000" in row.body
    assert (
        "The current price is below your target price.\n"
        "The price dropped 13.1% since the last check.\n"
        "This is a new lowest price."
    ) in row.body
    assert row.data["rule_ids"] == [rules["below_target"], rules["price_drop"], rules["new_low"]]
    assert row.data["subject"].endswith("(below target, down 13.1%, new low)")
    assert toasts[0].title == "Flight Price Alert"
    assert json.dumps(row.data)  # JSON-safe


def test_notify_price_alert_uses_runtime_language(db: Session) -> None:
    trip, run, offer = seed_trip(db)
    save_runtime_settings(db, {"language": "ko"})
    rule = trip.alert_rules[0]
    notification = notify_price_alert(
        db, trip, run, offer, [alert(trip, run, "below_target", rule.id, threshold=300_000.0)]
    )
    assert notification is not None and notification.title == "항공권 가격 알림"
    assert notification.body.startswith("서울 (ICN) ↔ 도쿄 (NRT)")


def test_notify_price_alert_survives_formatter_failure(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    trip, run, offer = seed_trip(db)

    def boom(*args: Any, **kwargs: Any) -> NotificationMessage:
        raise KeyError("oops")

    monkeypatch.setattr(dispatcher, "format_price_alert", boom)
    rule = trip.alert_rules[0]
    event = alert(trip, run, "below_target", rule.id, reason="Below your target price")
    notification = notify_price_alert(db, trip, run, offer, [event])
    assert notification is not None and notification.id is not None
    assert notification.title == "Flight Price Alert"
    assert "KRW 278,000" in notification.body and "Below your target price" in notification.body
    assert notification.url == "https://example.com/book"


# --------------------------------------------------------------------------- test sends


def test_send_test_inapp_creates_system_notification(db: Session) -> None:
    assert send_test(db, "inapp") == "sent"
    rows = db.scalars(select(Notification)).all()
    assert len(rows) == 1 and rows[0].kind == "system"
    assert rows[0].title == "AiSquared Travel test notification"
    assert rows[0].channel_results == {"inapp": "sent"}


@respx.mock
def test_send_test_external_channel_ignores_enabled_flag(db: Session) -> None:
    assert send_test(db, "telegram") == (
        "skipped: bot token is not set (create a bot with @BotFather)"
    )
    save_runtime_settings(
        db, {"notifications": {"telegram": {"enabled": False, "bot_token": TOKEN, "chat_id": "1"}}}
    )
    route = respx.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {}})
    )
    assert send_test(db, "telegram") == "sent"
    assert json.loads(route.calls.last.request.content)["text"].startswith(
        "AiSquared Travel test notification"
    )
    route.mock(return_value=httpx.Response(401, json={"ok": False, "error_code": 401}))
    result = send_test(db, "telegram")
    assert result.startswith("failed: Telegram error 401") and TOKEN not in result
    assert db.scalar(select(func.count()).select_from(Notification)) == 0


def test_send_test_desktop_and_unknown(db: Session, toasts: list[NotificationMessage]) -> None:
    assert send_test(db, "desktop") == "sent"
    assert toasts[0].title == "AiSquared Travel test notification"
    assert send_test(db, "sms") == "failed: unknown channel 'sms'"


def test_channel_statuses(db: Session) -> None:
    statuses = channel_statuses(load_runtime_settings(db))
    keys = [s["key"] for s in statuses]
    assert keys == ["inapp", "desktop", "telegram", "email", "ntfy", "kakao"]
    inapp = statuses[0]
    assert inapp["enabled"] and inapp["configured"] and inapp["message"] == ""
    kakao = statuses[-1]
    assert kakao["enabled"] is False and kakao["configured"] is False
    assert "no push alert" in kakao["description"]


def test_notify_itinerary_ready(db: Session, toasts: list[NotificationMessage]) -> None:
    trip, _run, _offer = seed_trip(db)
    itinerary = Itinerary(trip_id=trip.id, status="done", generator="rule_based", plan={})
    db.add(itinerary)
    db.commit()
    notification = notify_itinerary_ready(db, trip, itinerary)
    row = stored(db, notification.id)
    assert row.kind == "itinerary" and row.trip_id == trip.id
    assert row.title == "Itinerary ready"
    assert row.url == f"http://127.0.0.1:8765/itineraries/{itinerary.id}"
    assert row.data["itinerary_id"] == itinerary.id
    assert toasts[0].title == "Itinerary ready"
