from __future__ import annotations

import dataclasses
import logging
import sys
import threading
import time
import types
from collections.abc import Callable
from datetime import date, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import session_scope
from app.models import AlertRule, Notification, SearchRun, Trip
from app.models import FlightOffer as OfferRow
from app.providers.flights import mock as mock_module
from app.providers.flights.base import (
    FlightOffer,
    FlightQuery,
    ProviderBlocked,
    ProviderError,
)
from app.services import monitoring
from app.services import scheduler as scheduler_module
from app.services.monitoring import (
    BLOCK_EVENT,
    INTERRUPTED,
    RunInProgressError,
    TripNotFoundError,
    cancel_running_checks,
    create_run,
    execute_run,
    is_trip_running,
    recover_stale_runs,
    run_trip_check,
    start_trip_check,
    trim_raw,
)
from app.services.scheduler import MonitorScheduler
from app.settings_store import save_runtime_settings
from app.timeutil import now_local, utcnow
from monitoring_fakes import (  # noqa: F401  (register is a fixture)
    add_trip,
    days,
    fake_provider_class,
    make_leg,
    offers_for_query,
    register,
)

DEPART = now_local().date() + days(45)
RETURN = DEPART + days(4)


def trip_in_db(db: Session, **overrides: Any) -> Trip:
    values: dict[str, Any] = {"depart_date": DEPART, "return_date": RETURN, "providers": ["mock"]}
    values.update(overrides)
    return add_trip(db, **values)


def fixed_prices(key: str, prices: list[float]) -> Callable[[FlightQuery], list[FlightOffer]]:
    return lambda query: offers_for_query(query, prices, key)


def load_run(run_id: int) -> SearchRun:
    with session_scope() as db:
        run = db.get(SearchRun, run_id)
        assert run is not None
        return run


def run_offers(db: Session, run_id: int) -> list[OfferRow]:
    db.expire_all()
    return list(db.scalars(select(OfferRow).where(OfferRow.run_id == run_id).order_by(OfferRow.id)))


def wait_finished(run_id: int, timeout: float = 15.0) -> SearchRun:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = load_run(run_id)
        if run.status != "running":
            return run
        time.sleep(0.05)
    raise AssertionError(f"run {run_id} still running after {timeout} s")


@pytest.fixture
def notifier(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, ...]]:
    """Records ``notify_price_alert`` calls (no real dispatcher involved)."""
    calls: list[tuple[Any, ...]] = []

    def record(
        db: Session, trip: Trip, run: SearchRun, best: OfferRow | None, events: list
    ) -> None:
        calls.append((trip.id, run.id, best.id if best else None, [e.kind for e in events], best))

    monkeypatch.setattr(monitoring, "_load_notifier", lambda: record)
    return calls


# --------------------------------------------------------------------------- runs


def test_create_run(db: Session) -> None:
    trip = trip_in_db(db, currency="JPY")
    run = create_run(db, trip, "manual")
    stored = load_run(run.id)
    assert (stored.status, stored.trigger, stored.currency) == ("running", "manual", "JPY")
    assert stored.started_at is not None and stored.finished_at is None


def test_run_with_mock_persists_flattened_offers(db: Session, notifier: list) -> None:
    trip = trip_in_db(db, flex_depart_days=1)
    run_id = run_trip_check(trip.id, "manual")
    assert run_id is not None
    run = load_run(run_id)
    assert run.status == "ok" and run.error is None and run.finished_at is not None
    assert (run.combinations_total, run.combinations_searched) == (3, 3)
    assert run.provider_stats["mock"]["status"] == "ok"
    rows = run_offers(db, run_id)
    assert len(rows) == run.offers_found == run.offers_matching > 0
    for row in rows:
        out = row.outbound
        assert row.airline == ", ".join(out["airlines"])
        assert row.flight_number == " / ".join(out["flight_numbers"])
        assert (row.origin, row.destination) == (out["departure_airport"], out["arrival_airport"])
        assert row.departure_time == out["departure_time"] and row.departure_time.startswith(
            row.depart_date.isoformat()
        )
        assert row.return_date == RETURN and row.inbound is not None
        assert row.stops == max(out["stops"], row.inbound["stops"])
        assert row.previous_price is None and row.lowest_price == row.price_per_person
        assert row.raw == {"mock": True, "index": row.raw["index"]}
        assert row.searched_at is not None and row.provider == "mock"
    best = min(rows, key=lambda r: r.price_per_person)
    assert run.best_offer_id == best.id
    assert run.min_price_per_person == best.price_per_person
    assert run.min_price_total == min(r.price_total for r in rows)
    db.expire_all()
    stored_trip = db.get(Trip, trip.id)
    assert stored_trip.last_status == "ok" and stored_trip.last_error is None
    assert stored_trip.last_checked_at == run.finished_at
    assert run.log[-1].startswith(f"{len(rows)} offer(s) found")
    assert notifier == []  # no alert rules


def test_previous_and_lowest_price_across_runs(
    db: Session, monkeypatch: pytest.MonkeyPatch, notifier: list
) -> None:
    trip = trip_in_db(db)
    history: list[dict[str, float]] = []
    for day in (date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 3)):
        monkeypatch.setattr(mock_module, "_today", lambda day=day: day)
        run_id = run_trip_check(trip.id, "schedule")
        rows = run_offers(db, run_id)
        prices = {row.offer_key: row.price_per_person for row in rows}
        for row in rows:
            seen = [h[row.offer_key] for h in history]
            assert row.previous_price == (seen[-1] if seen else None)
            assert row.lowest_price == min([*seen, row.price_per_person])
        history.append(prices)
    assert len({frozenset(h) for h in history}) == 1  # same flights every day
    assert history[0] != history[1]  # ... at drifting prices


def test_history_is_per_provider_and_currency(db: Session, register, notifier: list) -> None:
    register(fake_provider_class("alpha", fixed_prices("alpha", [300_000])))
    register(fake_provider_class("beta", fixed_prices("beta", [280_000])))
    trip = trip_in_db(db, providers=["alpha", "beta"])
    run_trip_check(trip.id)
    second = run_offers(db, run_trip_check(trip.id))
    assert len({row.offer_key for row in second}) == 1  # same flight, two sources
    assert {row.provider: row.previous_price for row in second} == {
        "alpha": 300_000, "beta": 280_000
    }


def test_history_works_for_overlong_offer_keys(db: Session, register, notifier: list) -> None:
    def many_segments(query: FlightQuery) -> list[FlightOffer]:
        (offer,) = offers_for_query(query, [300_000], "long")
        numbers = tuple(f"KE{n}" for n in range(100, 260))
        offer.outbound = dataclasses.replace(offer.outbound, flight_numbers=list(numbers))
        return [offer]

    register(fake_provider_class("long", many_segments))
    trip = trip_in_db(db, providers=["long"])
    first = run_offers(db, run_trip_check(trip.id))
    second = run_offers(db, run_trip_check(trip.id))
    assert len(first[0].offer_key) == 512 and first[0].offer_key == second[0].offer_key
    assert second[0].previous_price == 300_000


def test_filters_and_best_offer(db: Session, register, notifier: list) -> None:
    register(fake_provider_class("fixed", fixed_prices("fixed", [300_000, 250_000, 400_000])))
    trip = trip_in_db(db, providers=["fixed"], max_price=350_000, outbound_depart_from="08:30")
    run = load_run(run_trip_check(trip.id))
    rows = run_offers(db, run.id)
    # 08:00 (300k) is too early, 10:00 (400k) too expensive, 09:00 (250k) matches
    assert [(r.price_per_person, r.matches_filters) for r in rows] == [
        (300_000, False), (250_000, True), (400_000, False)
    ]
    assert (run.offers_found, run.offers_matching) == (3, 1)
    assert run.best_offer_id == rows[1].id and run.min_price_per_person == 250_000
    assert any("2 offer(s) excluded by trip filters" in line for line in run.log)


def test_best_offer_uses_trip_basis(db: Session, register, notifier: list) -> None:
    register(fake_provider_class("fixed", fixed_prices("fixed", [300_000, 250_000])))
    trip = trip_in_db(db, providers=["fixed"], adults=2, price_basis="total")
    run = load_run(run_trip_check(trip.id))
    assert (run.min_price_per_person, run.min_price_total) == (250_000, 500_000)
    assert run.log[-1].endswith("best 500,000 KRW total (fixed)")


def test_no_matching_offer(db: Session, register, notifier: list) -> None:
    register(fake_provider_class("fixed", fixed_prices("fixed", [300_000])))
    trip = trip_in_db(db, providers=["fixed"], max_price=100_000)
    db.add(AlertRule(trip_id=trip.id, kind="below_target", threshold=1_000_000))
    db.commit()
    run = load_run(run_trip_check(trip.id))
    assert run.status == "ok" and run.best_offer_id is None and run.min_price_total is None
    assert notifier == []


# --------------------------------------------------------------------------- statuses


def test_partial_and_error_statuses(db: Session, register, notifier: list) -> None:
    def failing(query: FlightQuery) -> list[FlightOffer]:
        raise ProviderError("upstream 503")

    register(fake_provider_class("good", fixed_prices("good", [300_000])))
    register(fake_provider_class("bad", failing))

    partial = load_run(run_trip_check(trip_in_db(db, providers=["good", "bad"]).id))
    assert partial.status == "partial" and "bad: upstream 503" in partial.error
    assert partial.offers_found == 1 and partial.provider_stats["bad"]["status"] == "error"

    trip = trip_in_db(db, providers=["bad"])
    failed = load_run(run_trip_check(trip.id))
    assert failed.status == "error" and failed.error.startswith("All provider searches failed")
    db.expire_all()
    stored = db.get(Trip, trip.id)
    assert stored.last_status == "error" and "upstream 503" in stored.last_error


def test_no_configured_provider_is_error(db: Session, register) -> None:
    register(fake_provider_class("nokey", lambda q: [], configured=False))
    run = load_run(run_trip_check(trip_in_db(db, providers=["nokey"]).id))
    assert run.status == "error" and run.error == "No usable flight provider: nokey: needs key"
    assert run.provider_stats == {
        "nokey": {"status": "skipped", "offers": 0, "error": "needs key", "duration_s": 0.0,
                  "queries": 0}
    }


def test_internal_error_marks_run_failed(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(*_args: object) -> None:
        raise RuntimeError("disk full")

    monkeypatch.setattr(monitoring, "_persist_offers", explode)
    trip = trip_in_db(db)
    run = load_run(run_trip_check(trip.id))
    assert run.status == "error" and run.error == "internal error: disk full"
    db.expire_all()
    assert db.get(Trip, trip.id).last_status == "error"


def test_trip_deleted_during_search(db: Session, register) -> None:
    trip = trip_in_db(db, providers=["deleter"])
    trip_id = trip.id

    def delete_trip(query: FlightQuery) -> list[FlightOffer]:
        with session_scope() as other:
            other.delete(other.get(Trip, trip_id))
        return offers_for_query(query, [300_000], "deleter")

    register(fake_provider_class("deleter", delete_trip))
    assert run_trip_check(trip_id, "manual") is not None
    db.expire_all()
    assert db.get(Trip, trip_id) is None and db.scalars(select(SearchRun)).all() == []


def test_scheduled_check_skips_inactive_trip(db: Session, notifier: list) -> None:
    trip = trip_in_db(db, active=False)
    assert run_trip_check(trip.id, "schedule") is None
    assert db.scalars(select(SearchRun)).all() == []
    assert run_trip_check(trip.id, "manual") is not None  # manual checks still run
    assert run_trip_check(999_999, "schedule") is None


# --------------------------------------------------------------------------- alerts & notifications


def test_alert_dispatches_notification(db: Session, notifier: list) -> None:
    trip = trip_in_db(db)
    db.add(AlertRule(trip_id=trip.id, kind="below_target", threshold=10_000_000))
    db.commit()
    run = load_run(run_trip_check(trip.id))
    ((trip_id, run_id, best_id, kinds, _best),) = notifier
    assert (trip_id, run_id, best_id) == (trip.id, run.id, run.best_offer_id)
    assert kinds == ["below_target"]
    assert any(line.startswith("Alerts fired: Below your target price") for line in run.log)
    run_trip_check(trip.id)  # rule is disarmed now: no second notification at the same price
    assert len(notifier) == 1


def test_dispatcher_module_is_imported_lazily(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []
    fake = types.ModuleType("app.services.notifications.dispatcher")
    fake.notify_price_alert = (  # type: ignore[attr-defined]
        lambda db, trip, run, best, events: calls.append(run.id)
    )
    monkeypatch.setitem(sys.modules, "app.services.notifications.dispatcher", fake)
    trip = trip_in_db(db)
    db.add(AlertRule(trip_id=trip.id, kind="below_target", threshold=10_000_000))
    db.commit()
    run_id = run_trip_check(trip.id)
    assert calls == [run_id]


def test_missing_dispatcher_only_warns(
    db: Session, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setitem(sys.modules, "app.services.notifications.dispatcher", None)
    trip = trip_in_db(db)
    db.add(AlertRule(trip_id=trip.id, kind="below_target", threshold=10_000_000))
    db.commit()
    with caplog.at_level(logging.WARNING, logger="app.services.monitoring"):
        run = load_run(run_trip_check(trip.id))
    assert run.status == "ok"
    assert "Notification dispatcher unavailable" in caplog.text


def test_notification_failure_never_breaks_the_run(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_args: object) -> None:
        raise RuntimeError("smtp down password=hunter2")

    monkeypatch.setattr(monitoring, "_load_notifier", lambda: boom)
    trip = trip_in_db(db)
    rule = AlertRule(trip_id=trip.id, kind="below_target", threshold=10_000_000)
    db.add(rule)
    db.commit()
    run = load_run(run_trip_check(trip.id))
    assert run.status == "ok"
    db.refresh(rule)
    assert rule.armed is False  # the alert itself was recorded


def _gf_style(query: FlightQuery) -> list[FlightOffer]:
    """Google-Flights-like: outbound only, priced as a round trip."""
    offers = offers_for_query(query, [250_000], "gfstyle")
    return [
        dataclasses.replace(o, inbound=None, return_date=query.return_date.isoformat())
        for o in offers
    ]


def test_best_offer_enriched_when_alert_fires(db: Session, register, notifier: list) -> None:
    detail_calls: list[str] = []

    def details(offer: FlightOffer) -> FlightOffer:
        detail_calls.append(offer.offer_key())
        inbound = make_leg("NRT", "ICN", RETURN, "18:00", "20:30", stops=1, numbers=("KE706",))
        return dataclasses.replace(offer, inbound=inbound, baggage="1 x 23kg")

    register(fake_provider_class("gfstyle", _gf_style, details=details))
    trip = trip_in_db(db, providers=["gfstyle"], return_depart_to="12:00")
    first = load_run(run_trip_check(trip.id))
    assert detail_calls == []  # no alert, no detail fetch
    assert run_offers(db, first.id)[0].inbound is None

    db.add(AlertRule(trip_id=trip.id, kind="below_target", threshold=300_000))
    db.commit()
    run = load_run(run_trip_check(trip.id))
    (row,) = run_offers(db, run.id)
    assert len(detail_calls) == 1
    assert row.inbound["departure_time"].endswith("T18:00") and row.stops == 1
    assert row.baggage == "1 x 23kg"
    assert row.offer_key.split("|")[1] == f"RT:{RETURN.isoformat()}"  # key stays stable
    assert notifier[0][4].inbound is not None  # notifier got the enriched row
    assert any("misses trip filters" in line for line in run.log)


def test_enrichment_failure_or_timeout_still_notifies(
    db: Session, register, notifier: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = threading.Event()

    def slow(offer: FlightOffer) -> FlightOffer:
        release.wait(5)
        return offer

    monkeypatch.setattr(monitoring, "ENRICH_TIMEOUT_S", 0.2)
    register(fake_provider_class("gfstyle", _gf_style, details=slow))
    trip = trip_in_db(db, providers=["gfstyle"])
    db.add(AlertRule(trip_id=trip.id, kind="below_target", threshold=300_000))
    db.commit()
    started = time.monotonic()
    run = load_run(run_trip_check(trip.id))
    release.set()
    assert time.monotonic() - started < 4
    assert run.status == "ok" and len(notifier) == 1
    assert run_offers(db, run.id)[0].inbound is None


# --------------------------------------------------------------------------- concurrency


def test_one_check_per_trip(db: Session, register, notifier: list) -> None:
    gate = threading.Event()
    entered = threading.Event()

    def blocking(query: FlightQuery) -> list[FlightOffer]:
        entered.set()
        gate.wait(10)
        return offers_for_query(query, [300_000], "slow")

    register(fake_provider_class("slow", blocking))
    trip = trip_in_db(db, providers=["slow"])
    try:
        run_id = start_trip_check(trip.id)
        assert entered.wait(5)
        assert load_run(run_id).status == "running"
        assert is_trip_running(trip.id)
        assert start_trip_check(trip.id) == run_id  # same run returned
        assert run_trip_check(trip.id, "schedule") is None  # scheduled check skipped
    finally:
        gate.set()
    assert wait_finished(run_id).status == "ok"
    deadline = time.monotonic() + 5
    while is_trip_running(trip.id) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not is_trip_running(trip.id)
    next_id = start_trip_check(trip.id)
    assert next_id != run_id and wait_finished(next_id).status == "ok"
    assert len(db.scalars(select(SearchRun)).all()) == 2


def test_start_trip_check_unknown_trip_releases_claim() -> None:
    with pytest.raises(TripNotFoundError):
        start_trip_check(424242)
    assert not is_trip_running(424242)


def test_start_trip_check_while_another_is_starting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(monitoring, "CLAIM_WAIT_S", 0.1)
    assert monitoring._guard.try_claim(777)  # claimed, but no run id published yet
    try:
        with pytest.raises(RunInProgressError):
            start_trip_check(777)
    finally:
        monitoring._guard.release(777)


def test_recover_stale_runs(db: Session) -> None:
    stale_trip, busy_trip = trip_in_db(db), trip_in_db(db)
    stale = create_run(db, stale_trip, "schedule")
    busy = create_run(db, busy_trip, "manual")
    done = create_run(db, stale_trip, "manual")
    done.status = "ok"
    db.commit()
    assert monitoring._guard.try_claim(busy_trip.id)  # a check in progress in this process
    try:
        assert recover_stale_runs() == 1
    finally:
        monitoring._guard.release(busy_trip.id)
    fixed = load_run(stale.id)
    assert (fixed.status, fixed.error) == ("error", INTERRUPTED)
    assert fixed.finished_at is not None and "interrupted" in fixed.log[-1]
    assert load_run(busy.id).status == "running"
    assert load_run(done.id).status == "ok"


def test_execute_run_ignores_finished_runs(db: Session) -> None:
    run = create_run(db, trip_in_db(db), "manual")
    run.status = "ok"
    db.commit()
    execute_run(run.id)
    execute_run(123_456)  # unknown run: no error
    assert load_run(run.id).status == "ok"


# --------------------------------------------------------------------------- helpers


def test_trim_raw() -> None:
    assert trim_raw(None) == {}
    assert trim_raw({"a": 1, "when": date(2026, 1, 1)}) == {"a": 1, "when": "2026-01-01"}
    big = {"small": "x", "huge": "y" * 50_000, "list": list(range(10))}
    trimmed = trim_raw(big, max_chars=1_000)
    assert trimmed == {"small": "x", "list": list(range(10)), "_trimmed": ["huge"]}


# --------------------------------------------------------------------------- departed trips


def test_scheduled_check_of_departed_trip_deactivates_it(
    db: Session, monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    today = now_local().date()
    trip = trip_in_db(db, depart_date=today - days(1), return_date=today + days(3))
    sched = MonitorScheduler(settings)
    monkeypatch.setattr(scheduler_module, "_instance", sched)
    assert sched.sync_trip(trip, today=today - days(2))  # jobs made while still upcoming
    assert run_trip_check(trip.id, "schedule") is None
    db.expire_all()
    assert db.scalars(select(SearchRun)).all() == []  # no error run
    assert db.get(Trip, trip.id).active is False
    assert sched.next_run_for_trip(trip.id) is None  # its jobs are gone
    assert run_trip_check(trip.id, "manual") is not None  # explicit checks still run


def test_flexible_dates_keep_a_departed_trip_monitored(db: Session, notifier: list) -> None:
    today = now_local().date()
    trip = trip_in_db(
        db, depart_date=today - days(1), return_date=today + days(5), flex_depart_days=3
    )
    run = load_run(run_trip_check(trip.id, "schedule"))
    assert run.status == "ok" and run.combinations_total == 3  # today+0..+2 (past ones noted)
    db.expire_all()
    assert db.get(Trip, trip.id).active is True


# --------------------------------------------------------------------------- shutdown


def test_cancel_running_checks_interrupts_the_run(db: Session, register) -> None:
    entered, release = threading.Event(), threading.Event()
    calls: list[date] = []

    def blocking(query: FlightQuery) -> list[FlightOffer]:
        calls.append(query.depart_date)
        entered.set()
        release.wait(10)
        return offers_for_query(query, [300_000], "slow")

    register(fake_provider_class("slow", blocking))
    trip = trip_in_db(db, providers=["slow"], flex_depart_days=1)
    try:
        run_id = start_trip_check(trip.id)
        assert entered.wait(5)
        assert cancel_running_checks() == 1
        run = wait_finished(run_id, timeout=5)  # no waiting for the hung query
    finally:
        release.set()
    assert (run.status, run.error) == ("error", INTERRUPTED)
    assert run.log[-1] == "Run interrupted: the application is shutting down"
    assert run_offers(db, run_id) == []
    db.expire_all()
    stored = db.get(Trip, trip.id)
    assert stored.last_checked_at is None and stored.last_status is None  # catch-up later
    time.sleep(0.3)
    assert len(calls) == 1  # the provider sent none of its 2 remaining queries


def test_detail_fetch_runs_on_a_daemon_thread(db: Session, register, notifier: list) -> None:
    flags: list[bool] = []

    def details(offer: FlightOffer) -> FlightOffer:
        flags.append(threading.current_thread().daemon)
        return offer

    register(fake_provider_class("gfstyle", _gf_style, details=details))
    trip = trip_in_db(db, providers=["gfstyle"])
    db.add(AlertRule(trip_id=trip.id, kind="below_target", threshold=300_000))
    db.commit()
    run_trip_check(trip.id)
    assert flags == [True]


# --------------------------------------------------------------------------- provider blocks


@pytest.fixture
def system_notices(monkeypatch: pytest.MonkeyPatch) -> list[Notification]:
    """Stands in for ``dispatcher.notify``: stores the in-app row like the real one."""
    stored: list[Notification] = []

    def notify(
        db: Session,
        *,
        kind: str,
        message: Any,
        trip_id: int | None = None,
        alert_rule_id: int | None = None,
    ) -> Notification:
        row = Notification(
            kind=kind,
            title=message.title,
            body=message.body,
            url=message.url,
            data=dict(message.data),
            trip_id=trip_id,
        )
        db.add(row)
        db.commit()
        stored.append(row)
        return row

    monkeypatch.setattr(monitoring, "_load_system_notifier", lambda: notify)
    return stored


def _blocking_provider(register: Callable[[Any], None], key: str) -> None:
    def blocked(query: FlightQuery) -> list[FlightOffer]:
        raise ProviderBlocked("HTTP 429 /sorry/ page")

    register(fake_provider_class(key, blocked))


def test_block_is_reported_once_per_provider_per_day(
    db: Session, register, system_notices: list[Notification]
) -> None:
    _blocking_provider(register, "gblock")
    register(fake_provider_class("good", fixed_prices("good", [300_000])))
    yesterday = Notification(
        kind="system",
        title="old",
        body="",
        created_at=utcnow() - timedelta(hours=25),
        data={"event": BLOCK_EVENT, "provider": "gblock"},
    )
    db.add(yesterday)
    db.commit()
    first = trip_in_db(db, name="Tokyo", providers=["gblock", "good"])
    run = load_run(run_trip_check(first.id))
    assert run.status == "partial"
    (notice,) = system_notices
    assert notice.kind == "system" and notice.trip_id == first.id
    assert notice.title == "Gblock blocked automated searches"
    assert "Tokyo (ICN → NRT)" in notice.body and "HTTP 429 /sorry/ page" in notice.body
    assert notice.data == {
        "event": BLOCK_EVENT,
        "provider": "gblock",
        "trip_id": first.id,
        "run_id": run.id,
        "reason": "HTTP 429 /sorry/ page",
    }
    other = trip_in_db(db, providers=["gblock"])
    assert load_run(run_trip_check(other.id)).status == "error"  # error runs report too ...
    run_trip_check(first.id)
    assert len(system_notices) == 1  # ... but not twice a day for the same provider
    _blocking_provider(register, "nblock")
    run_trip_check(trip_in_db(db, providers=["nblock"]).id)
    assert [n.data["provider"] for n in system_notices] == ["gblock", "nblock"]


def test_block_notice_in_korean(
    db: Session, register, system_notices: list[Notification]
) -> None:
    save_runtime_settings(db, {"language": "ko"})
    _blocking_provider(register, "gblock")
    run_trip_check(trip_in_db(db, providers=["gblock"]).id)
    assert system_notices[0].title == "Gblock 자동 검색 차단"


def test_block_notice_failure_never_breaks_the_run(
    db: Session, register, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("smtp down")

    monkeypatch.setattr(monitoring, "_load_system_notifier", lambda: boom)
    _blocking_provider(register, "gblock")
    register(fake_provider_class("good", fixed_prices("good", [300_000])))
    run = load_run(run_trip_check(trip_in_db(db, providers=["gblock", "good"]).id))
    assert run.status == "partial" and run.offers_found == 1


def test_system_notifier_is_the_dispatchers_notify() -> None:
    from app.services.notifications import dispatcher

    assert monitoring._load_system_notifier() is dispatcher.notify
