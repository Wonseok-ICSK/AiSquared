from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy.orm import Session

from app.models import AlertRule, SearchRun, Trip
from app.services.alerts import default_alert_rules, evaluate_alerts, run_best_price
from app.settings_store import save_runtime_settings
from app.timeutil import utcnow
from monitoring_fakes import add_trip, make_trip

Step = float | None | tuple[float | None, str]
"""A run: its best per-person price, or ``(price, status)``."""


def finish_run(
    db: Session, trip: Trip, price: float | None, *, status: str = "ok", currency: str | None = None
) -> SearchRun:
    run = SearchRun(
        trip_id=trip.id,
        trigger="schedule",
        status=status,
        currency=currency or trip.currency,
        min_price_per_person=price,
        min_price_total=price * trip.adults if price is not None else None,
        finished_at=utcnow(),
        best_offer_id=None,
    )
    db.add(run)
    db.commit()
    return run


def run_steps(db: Session, trip: Trip, steps: list[Step]) -> list[list[str]]:
    fired: list[list[str]] = []
    for step in steps:
        price, status = step if isinstance(step, tuple) else (step, "ok")
        run = finish_run(db, trip, price, status=status)
        events = evaluate_alerts(db, trip, run)
        db.commit()
        fired.append([event.kind for event in events])
    return fired


def with_rule(db: Session, trip_kwargs: dict[str, Any] | None = None, **rule: Any) -> Trip:
    trip = add_trip(db, **(trip_kwargs or {}))
    db.add(AlertRule(trip_id=trip.id, **rule))
    db.commit()
    return trip


BT, PD, NL = ["below_target"], ["price_drop"], ["new_low"]

SEQUENCES = [
    pytest.param(
        {"kind": "below_target", "threshold": 300_000},
        [320_000, 290_000, 280_000, 285_000, 310_000, 295_000, 295_000],
        [[], BT, BT, [], [], BT, []],
        id="below_target: fire, improve, hold, re-arm, fire again",
    ),
    pytest.param(
        {"kind": "below_target", "threshold": 300_000},
        [300_000, 300_000],
        [BT, []],
        id="below_target: equal to threshold fires once",
    ),
    pytest.param(
        {"kind": "below_target", "threshold": 300_000},
        [None, 290_000, None, 280_000],
        [[], BT, [], BT],
        id="below_target: runs without price change nothing",
    ),
    pytest.param(
        {"kind": "price_drop", "drop_percent": 10.0},
        [300_000, 280_000, 250_000, 250_000, 225_000],
        [[], [], PD, [], PD],
        id="price_drop: vs previous run, >= percent",
    ),
    pytest.param(
        {"kind": "price_drop", "drop_percent": 10.0},
        [300_000, (None, "error"), 250_000],
        [[], [], PD],
        id="price_drop: error runs are not finished runs",
    ),
    pytest.param(
        {"kind": "price_drop", "drop_percent": 10.0},
        [300_000, None, 200_000],
        [[], [], []],
        id="price_drop: previous finished run without price",
    ),
    pytest.param(
        {"kind": "new_low"},
        [300_000, 310_000, 290_000, 290_000, 289_000],
        [[], [], NL, [], NL],
        id="new_low: strictly below every earlier best",
    ),
    pytest.param(
        {"kind": "new_low"},
        [None, 300_000, (250_000, "partial")],
        [[], [], NL],
        id="new_low: first priced run never fires; partial runs count",
    ),
]


@pytest.mark.parametrize(("rule", "steps", "expected"), SEQUENCES)
def test_alert_sequences(
    db: Session, rule: dict[str, Any], steps: list[Step], expected: list[list[str]]
) -> None:
    trip = with_rule(db, **rule)
    assert run_steps(db, trip, steps) == expected


def test_first_run_fires_nothing_but_below_target(db: Session) -> None:
    trip = add_trip(db, target_price=400_000)
    for rule in default_alert_rules(trip):
        trip.alert_rules.append(rule)
    db.add(AlertRule(trip_id=trip.id, kind="price_drop", drop_percent=5.0))
    db.commit()
    assert run_steps(db, trip, [350_000]) == [BT]


def test_rule_state_and_event_fields(db: Session) -> None:
    trip = with_rule(db, kind="below_target", threshold=300_000)
    finish_run(db, trip, 330_000)
    finish_run(db, trip, 320_000)
    run = finish_run(db, trip, 288_000)
    run.best_offer_id = 42
    (event,) = evaluate_alerts(db, trip, run)
    db.commit()
    assert event.kind == "below_target" and event.rule_id == trip.alert_rules[0].id
    assert (event.trip_id, event.run_id, event.offer_id) == (trip.id, run.id, 42)
    assert (event.price, event.basis, event.currency) == (288_000, "per_person", "KRW")
    assert event.threshold == 300_000
    assert event.previous_price == 320_000 and event.previous_lowest == 320_000
    assert event.drop_percent == 10.0
    assert event.reason == "Below your target price"
    rule = trip.alert_rules[0]
    db.refresh(rule)
    assert rule.armed is False and rule.last_triggered_price == 288_000
    assert rule.last_triggered_at is not None


def test_price_drop_event_reports_actual_drop(db: Session) -> None:
    trip = with_rule(db, kind="price_drop", drop_percent=10.0)
    finish_run(db, trip, 400_000)
    (event,) = evaluate_alerts(db, trip, finish_run(db, trip, 300_000))
    assert event.drop_percent == 25.0 and event.previous_price == 400_000
    assert event.reason == "Price dropped 25.0% since the last check"


def test_threshold_none_uses_trip_target_in_trip_basis(db: Session) -> None:
    trip = with_rule(
        db,
        {"target_price": 500_000, "price_basis": "total", "adults": 2},
        kind="below_target",
        threshold=None,
        price_basis="per_person",
    )
    (event,) = evaluate_alerts(db, trip, finish_run(db, trip, 240_000))
    assert (event.price, event.basis, event.threshold) == (480_000, "total", 500_000)


def test_threshold_none_without_target_never_fires(db: Session) -> None:
    trip = with_rule(db, kind="below_target", threshold=None)
    assert run_steps(db, trip, [1.0]) == [[]]


def test_rule_threshold_in_rule_basis(db: Session) -> None:
    trip = with_rule(
        db, {"adults": 2}, kind="below_target", threshold=500_000, price_basis="total"
    )
    assert run_steps(db, trip, [260_000, 245_000]) == [[], BT]


def test_currency_mismatch(db: Session) -> None:
    trip = add_trip(db, currency="KRW")
    db.add_all(
        [
            AlertRule(trip_id=trip.id, kind="new_low"),
            AlertRule(trip_id=trip.id, kind="price_drop", drop_percent=5.0),
            AlertRule(trip_id=trip.id, kind="below_target", threshold=1_000_000),
        ]
    )
    db.commit()
    usd_run = finish_run(db, trip, 100.0, currency="USD")
    assert evaluate_alerts(db, trip, usd_run) == []  # run currency differs from the trip
    # earlier USD history is ignored for KRW comparisons
    kinds = [e.kind for e in evaluate_alerts(db, trip, finish_run(db, trip, 300_000))]
    assert kinds == ["below_target"]


def test_disabled_rules_and_unfinished_runs(db: Session) -> None:
    trip = with_rule(db, kind="below_target", threshold=300_000, enabled=False)
    assert run_steps(db, trip, [100_000]) == [[]]
    trip2 = with_rule(db, kind="below_target", threshold=300_000)
    running = finish_run(db, trip2, 100_000, status="running")
    assert evaluate_alerts(db, trip2, running) == []
    assert evaluate_alerts(db, trip2, finish_run(db, trip2, 100_000, status="error")) == []


def test_several_rules_fire_in_one_run(db: Session) -> None:
    trip = add_trip(db, target_price=300_000)
    trip.alert_rules.extend(default_alert_rules(trip))
    trip.alert_rules.append(AlertRule(kind="price_drop", drop_percent=10.0))
    db.commit()
    assert run_steps(db, trip, [400_000, 280_000]) == [[], [*BT, *NL, *PD]]


def test_reasons_follow_language_setting(db: Session) -> None:
    save_runtime_settings(db, {"language": "ko"})
    trip = with_rule(db, kind="below_target", threshold=300_000)
    (event,) = evaluate_alerts(db, trip, finish_run(db, trip, 100_000))
    assert event.reason == "목표 가격 이하"


def test_run_best_price() -> None:
    run = SearchRun(min_price_total=600.0, min_price_per_person=300.0)
    assert run_best_price(run, "total") == 600.0
    assert run_best_price(run, "per_person") == 300.0


def test_default_alert_rules() -> None:
    with_target = default_alert_rules(make_trip(target_price=300_000, price_basis="total"))
    assert [(r.kind, r.threshold, r.price_basis, r.armed, r.enabled) for r in with_target] == [
        ("below_target", None, "total", True, True),
        ("new_low", None, "total", True, True),
    ]
    assert [r.kind for r in default_alert_rules(make_trip(target_price=None))] == ["new_low"]
    assert all(r.trip_id is None for r in with_target)  # caller attaches them


# --------------------------------------------------------------------------- after trip edits


def test_below_target_fires_again_after_currency_change(db: Session) -> None:
    trip = add_trip(db, currency="USD", target_price=250.0)
    trip.alert_rules.extend(default_alert_rules(trip))
    db.commit()
    assert run_steps(db, trip, [220.0]) == [BT]
    trip.currency, trip.target_price = "KRW", 300_000.0  # PUT /trips/{id}
    db.commit()
    # 290,000 KRW must not be compared with the 220 USD of the last alert; USD history
    # never counts for new_low either
    assert run_steps(db, trip, [290_000, 290_000, 285_000]) == [BT, [], [*BT, *NL]]


def test_below_target_compares_in_the_new_basis_after_basis_change(db: Session) -> None:
    trip = add_trip(db, adults=2, target_price=250_000, price_basis="per_person")
    db.add(AlertRule(trip_id=trip.id, kind="below_target", threshold=None))
    db.commit()
    assert run_steps(db, trip, [220_000]) == [BT]  # 220k per person = 440k total
    trip.price_basis, trip.target_price = "total", 500_000
    db.commit()
    # 460k total is worse than the alerted 440k total; 430k total beats it
    assert run_steps(db, trip, [230_000, 215_000]) == [[], BT]


def test_below_target_without_its_trigger_run_counts_as_armed(db: Session) -> None:
    trip = add_trip(db)
    db.add(
        AlertRule(
            trip_id=trip.id, kind="below_target", threshold=300_000, armed=False,
            last_triggered_price=100.0, last_triggered_at=utcnow() - timedelta(days=1),
        )
    )
    db.commit()
    assert run_steps(db, trip, [290_000, 290_000]) == [BT, []]


def test_below_target_without_trigger_time_compares_the_stored_price(db: Session) -> None:
    trip = add_trip(db)
    db.add(
        AlertRule(
            trip_id=trip.id, kind="below_target", threshold=300_000, armed=False,
            last_triggered_price=280_000, last_triggered_at=None,
        )
    )
    db.commit()
    assert run_steps(db, trip, [290_000, 270_000]) == [[], BT]
