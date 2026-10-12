"""Plain data objects shared between services (alerts -> notifications).

Kept in their own module so the alert engine and the notification package can be
developed and tested independently without importing each other.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

AlertKind = Literal["below_target", "price_drop", "new_low"]
PriceBasis = Literal["per_person", "total"]
NotificationKind = Literal["price_alert", "itinerary", "system"]


@dataclass(frozen=True)
class AlertEvent:
    """One alert rule that fired for one finished search run.

    All prices are expressed in ``basis`` (per person or total for all passengers)
    and ``currency``.
    """

    rule_id: int
    kind: AlertKind
    trip_id: int
    run_id: int
    offer_id: int | None  # best matching FlightOffer row of the run
    price: float  # current best price (in ``basis``)
    basis: PriceBasis
    currency: str
    threshold: float | None = None  # below_target: the target price used
    previous_lowest: float | None = None  # lowest best-price over earlier finished runs
    previous_price: float | None = None  # best price of the previous finished run
    drop_percent: float | None = None  # actual drop vs previous run, in percent
    reason: str = ""  # short human-readable reason, e.g. "Below your target price"


@dataclass
class NotificationMessage:
    """Channel-independent notification content.

    ``body`` is plain text (newline separated). ``url`` is the primary link
    (booking/search page). ``data`` carries structured values channels may use
    (e.g. prices for rich templates).
    """

    title: str
    body: str
    url: str | None = None
    url_label: str = "Open booking page"
    data: dict[str, Any] = field(default_factory=dict)
