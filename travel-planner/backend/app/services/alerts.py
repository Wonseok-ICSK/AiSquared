"""Alert evaluation after a monitoring run (DESIGN §11).

``price`` below is the run's best matching price in the rule's basis
(``SearchRun.min_price_per_person`` or ``min_price_total``), compared only with values in
the same currency. A "finished" run is one with status ``ok`` or ``partial``.

* ``below_target``: fires when ``price <= threshold`` and (``armed`` or
  ``price < last_triggered_price``), then ``armed = False``; a later run with
  ``price > threshold`` re-arms it. ``threshold = None`` means the trip's ``target_price``
  in the trip's ``price_basis``. ``last_triggered_price`` is compared in today's currency
  and basis: the run that fired the rule supplies its best price in the current basis, and
  a rule whose last alert was in another currency (the trip's currency was changed) counts
  as armed.
* ``price_drop``: fires when the previous finished run's best price ``p0`` exists and
  ``(p0 - price) / p0 * 100 >= drop_percent``.
* ``new_low``: fires when at least one earlier finished run has a best price and
  ``price < min(earlier best prices)`` (so the very first run never fires).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AlertRule, SearchRun, Trip
from app.services.events import AlertEvent, PriceBasis
from app.settings_store import load_runtime_settings
from app.timeutil import utcnow

logger = logging.getLogger(__name__)

FINISHED_RUN_STATUSES: tuple[str, ...] = ("ok", "partial")
"""Run statuses whose best price counts as history for alerts."""
_EPSILON = 1e-9

_REASONS: dict[str, dict[str, str]] = {
    "en": {
        "below_target": "Below your target price",
        "price_drop": "Price dropped {percent:.1f}% since the last check",
        "new_low": "New lowest price",
    },
    "ko": {
        "below_target": "목표 가격 이하",
        "price_drop": "지난 확인 대비 {percent:.1f}% 하락",
        "new_low": "역대 최저가",
    },
}


def run_best_price(run: SearchRun, basis: str) -> float | None:
    """The run's best matching price in ``basis`` (``"total"`` or ``"per_person"``)."""
    return run.min_price_total if basis == "total" else run.min_price_per_person


@dataclass
class _Context:
    """What every rule evaluator needs for one run."""

    trip: Trip
    run: SearchRun
    earlier: list[SearchRun]  # earlier finished runs, newest first
    language: str
    now: datetime
    _lows: dict[str, float | None] = field(default_factory=dict)

    def price(self, basis: str) -> float | None:
        return run_best_price(self.run, basis)

    def previous_price(self, basis: str) -> float | None:
        """Best price of the previous finished run (``None`` if absent or other currency)."""
        if not self.earlier or self.earlier[0].currency != self.run.currency:
            return None
        return run_best_price(self.earlier[0], basis)

    def run_finished_by(self, moment: datetime) -> SearchRun | None:
        """The latest earlier finished run that had finished at ``moment``."""
        for run in self.earlier:
            if run.finished_at is not None and run.finished_at <= moment:
                return run
        return None

    def previous_lowest(self, basis: str) -> float | None:
        """Lowest best price over earlier finished runs in the same currency."""
        if basis not in self._lows:
            prices = [
                price
                for r in self.earlier
                if r.currency == self.run.currency
                and (price := run_best_price(r, basis)) is not None
            ]
            self._lows[basis] = min(prices) if prices else None
        return self._lows[basis]

    def reason(self, kind: str, **values: float) -> str:
        texts = _REASONS.get(self.language, _REASONS["en"])
        return texts[kind].format(**values)

    def event(self, rule: AlertRule, basis: str, price: float, **extra: float | None) -> AlertEvent:
        previous = self.previous_price(basis)
        drop = (
            round((previous - price) / previous * 100, 2)
            if previous and price < previous
            else None
        )
        values: dict[str, float | None] = {
            "previous_lowest": self.previous_lowest(basis),
            "previous_price": previous,
            "drop_percent": drop,
        }
        values.update(extra)
        reason = self.reason(rule.kind, percent=drop or 0.0)
        return AlertEvent(
            rule_id=rule.id,
            kind=rule.kind,  # type: ignore[arg-type]
            trip_id=self.trip.id,
            run_id=self.run.id,
            offer_id=self.run.best_offer_id,
            price=price,
            basis=basis,  # type: ignore[arg-type]
            currency=self.run.currency,
            reason=reason,
            **values,  # type: ignore[arg-type]
        )


def evaluate_alerts(db: Session, trip: Trip, run: SearchRun) -> list[AlertEvent]:
    """Evaluate the trip's enabled alert rules against a finished ``run``.

    Updates ``armed`` / ``last_triggered_at`` / ``last_triggered_price`` on the rules
    (flushed, not committed) and returns one :class:`AlertEvent` per rule that fired.
    Runs that are not finished (``ok``/``partial``), have no matching offer, or whose
    currency differs from the trip's produce no events.
    """
    if run.status not in FINISHED_RUN_STATUSES:
        return []
    if run.currency != trip.currency:
        logger.info(
            "Run %s is in %s but trip %s is in %s; alerts skipped",
            run.id, run.currency, trip.id, trip.currency,
        )
        return []
    rules = db.scalars(
        select(AlertRule).where(AlertRule.trip_id == trip.id).order_by(AlertRule.id)
    ).all()
    rules = [rule for rule in rules if rule.enabled]
    if not rules:
        return []

    ctx = _Context(
        trip=trip,
        run=run,
        earlier=_earlier_finished_runs(db, run),
        language=_language(db),
        now=utcnow(),
    )
    events: list[AlertEvent] = []
    for rule in rules:
        evaluator = _EVALUATORS.get(rule.kind)
        if evaluator is None:
            logger.warning("Unknown alert rule kind %r (rule %s)", rule.kind, rule.id)
            continue
        event = evaluator(rule, ctx)
        if event is not None:
            rule.last_triggered_at = ctx.now
            rule.last_triggered_price = event.price
            events.append(event)
    db.flush()
    if events:
        logger.info(
            "Trip %s run %s fired alerts: %s", trip.id, run.id, ", ".join(e.kind for e in events)
        )
    return events


def _below_target(rule: AlertRule, ctx: _Context) -> AlertEvent | None:
    threshold, basis = _target(rule, ctx.trip)
    if threshold is None:
        return None
    price = ctx.price(basis)
    if price is None:
        return None
    if price > threshold:
        rule.armed = True  # re-arm once the price is back above the threshold
        return None
    if not (rule.armed or _beats_last_trigger(rule, ctx, basis, price)):
        return None
    rule.armed = False
    return ctx.event(rule, basis, price, threshold=threshold)


def _beats_last_trigger(rule: AlertRule, ctx: _Context, basis: str, price: float) -> bool:
    """Whether a disarmed ``below_target`` rule may fire again at ``price``.

    The reference is the best price, in ``basis``, of the run that last fired the rule
    (found by ``last_triggered_at``), so a change of the trip's/rule's price basis compares
    like with like. When that run used another currency, or cannot be found, the stored
    price is not comparable and the rule behaves as armed (returns True).
    """
    if rule.last_triggered_price is None:
        return False
    if rule.last_triggered_at is None:  # no trigger time recorded: plain comparison
        return price < rule.last_triggered_price
    trigger_run = ctx.run_finished_by(rule.last_triggered_at)
    if trigger_run is None or trigger_run.currency != ctx.run.currency:
        logger.info(
            "Alert rule %s: last alert is not comparable with %s prices; treated as armed",
            rule.id, ctx.run.currency,
        )
        return True
    reference = run_best_price(trigger_run, basis)
    return reference is None or price < reference - _EPSILON


def _target(rule: AlertRule, trip: Trip) -> tuple[float | None, str]:
    """``(threshold, basis)``: the rule's own threshold, else the trip's target price."""
    if rule.threshold is not None:
        return rule.threshold, rule.price_basis or "per_person"
    return trip.target_price, trip.price_basis or "per_person"


def _price_drop(rule: AlertRule, ctx: _Context) -> AlertEvent | None:
    basis = rule.price_basis or "per_person"
    price, previous = ctx.price(basis), ctx.previous_price(basis)
    if price is None or not previous or previous <= 0 or rule.drop_percent is None:
        return None
    drop = (previous - price) / previous * 100
    if drop + _EPSILON < rule.drop_percent:
        return None
    return ctx.event(rule, basis, price)


def _new_low(rule: AlertRule, ctx: _Context) -> AlertEvent | None:
    basis = rule.price_basis or "per_person"
    price, lowest = ctx.price(basis), ctx.previous_lowest(basis)
    if price is None or lowest is None or price >= lowest:
        return None
    return ctx.event(rule, basis, price)


_EVALUATORS: dict[str, Callable[[AlertRule, _Context], AlertEvent | None]] = {
    "below_target": _below_target,
    "price_drop": _price_drop,
    "new_low": _new_low,
}


def _earlier_finished_runs(db: Session, run: SearchRun) -> list[SearchRun]:
    """Finished runs of the same trip created before ``run``, newest first."""
    stmt = (
        select(SearchRun)
        .where(
            SearchRun.trip_id == run.trip_id,
            SearchRun.id < run.id,
            SearchRun.status.in_(FINISHED_RUN_STATUSES),
        )
        .order_by(SearchRun.id.desc())
    )
    return list(db.scalars(stmt))


def _language(db: Session) -> str:
    try:
        return load_runtime_settings(db).language
    except Exception:  # noqa: BLE001 - reason text language is cosmetic
        logger.warning("Could not load runtime settings; alert reasons in English")
        return "en"


def default_alert_rules(trip: Trip) -> list[AlertRule]:
    """Rules attached to a new trip: ``below_target`` (when it has a target price, following
    ``trip.target_price``) and ``new_low`` (always), both in the trip's price basis.

    The rules are not attached; the caller adds them (``trip.alert_rules.extend(...)``).
    """
    basis: PriceBasis = trip.price_basis or "per_person"  # type: ignore[assignment]
    rules: list[AlertRule] = []
    if trip.target_price is not None:
        rules.append(
            AlertRule(
                kind="below_target", enabled=True, threshold=None, price_basis=basis, armed=True
            )
        )
    rules.append(AlertRule(kind="new_low", enabled=True, price_basis=basis, armed=True))
    return rules
