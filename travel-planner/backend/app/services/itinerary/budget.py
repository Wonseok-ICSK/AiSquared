"""Budget semantics, local cost defaults and the plan budget summary (DESIGN §3, §10 step 5).

Budget values follow the spec: ``None`` = not specified, ``0``/``-1`` = unlimited ("search in a
reasonable range" → the planner uses mid-range defaults), positive = cap in
``budget_currency`` per ``budget_basis`` (``per_person`` or ``total`` for all travellers).

Plan item costs (``PlanItem.est_cost``) are **per person** in ``cost_currency`` (the plan's
``currency_local`` when unset). Two kinds of currency conversion are used deliberately:

* planning decisions (meal price caps, default costs) use the static offline table
  :func:`app.core.fx.static_rate` so the same inputs always give the same plan;
* the reported :class:`~app.schemas.BudgetSummary` uses :func:`app.core.fx.convert` (live
  Frankfurter rate, static fallback flagged in ``notes`` and ``fx``).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field

from app.core import fx
from app.schemas import BudgetLine, BudgetSummary, ItineraryPlan, PlanDay
from app.services.itinerary.models import PlanInputs

logger = logging.getLogger(__name__)

BUDGET_CATEGORIES = ("flight", "hotel", "food", "transport", "activities", "shopping", "other")

# Mid-range per-person prices in USD at price index 1.0 (hotel = per room-night).
BASE_USD: dict[str, float] = {
    "breakfast": 10.0,
    "lunch": 18.0,
    "dinner": 35.0,
    "cafe": 7.0,
    "transit_ride": 2.2,
    "hotel_night": 140.0,
}
ACTIVITY_USD: dict[str, float] = {
    "attraction": 0.0,
    "museum": 12.0,
    "historic": 4.0,
    "viewpoint": 16.0,
    "shopping": 0.0,
    "market": 0.0,
    "nature": 0.0,
    "park": 0.0,
    "theme_park": 75.0,
    "nightlife": 25.0,
    "restaurant": 18.0,
    "cafe": 7.0,
    "experience": 40.0,
    "neighborhood": 0.0,
}
AIRPORT_TRANSFER_USD_BASE = 6.0
AIRPORT_TRANSFER_USD_PER_KM = 0.28
PRICE_LEVEL_FACTOR = {0: 0.0, 1: 0.6, 2: 1.0, 3: 1.8, 4: 3.2}
"""Multiplier on the mid-range meal price for a Google-style price level."""

PRICE_INDEX: dict[str, float] = {
    # rough cost-of-travel index vs. a US mid-range baseline (1.0)
    "KR": 0.85, "JP": 0.85, "CN": 0.6, "TW": 0.65, "HK": 1.0, "MO": 0.95, "MN": 0.45,
    "TH": 0.45, "VN": 0.35, "PH": 0.45, "SG": 1.05, "MY": 0.45, "ID": 0.4, "KH": 0.4,
    "LA": 0.35, "MM": 0.35, "IN": 0.3, "LK": 0.35, "NP": 0.3, "MV": 1.2, "UZ": 0.4,
    "AE": 1.0, "QA": 1.0, "TR": 0.5, "IL": 1.1, "AU": 1.1, "NZ": 1.05, "FJ": 0.8, "GU": 1.2,
    "MP": 1.1, "PW": 1.1, "US": 1.2, "CA": 1.05, "MX": 0.6, "BR": 0.6, "AR": 0.55, "PE": 0.5,
    "GB": 1.15, "IE": 1.1, "FR": 1.1, "DE": 1.0, "IT": 1.0, "ES": 0.85, "PT": 0.8, "NL": 1.1,
    "BE": 1.05, "AT": 1.0, "CH": 1.5, "CZ": 0.7, "HU": 0.6, "PL": 0.6, "GR": 0.8, "HR": 0.8,
    "DK": 1.25, "SE": 1.1, "NO": 1.35, "FI": 1.1, "IS": 1.45, "RU": 0.5, "EG": 0.35,
    "MA": 0.45, "ZA": 0.5, "KE": 0.5,
}
DEFAULT_PRICE_INDEX = 0.9

MEAL_SHARE_OF_DAILY_FOOD = {"breakfast": 0.15, "lunch": 0.35, "dinner": 0.5, "cafe": 0.15}
LOCAL_SPENDING_SPLIT = {"food": 0.45, "activities": 0.25, "transport": 0.15}
"""Share of (total − flight − hotel) used as soft caps when only a total budget is set."""


def budget_value(value: float | None) -> tuple[float | None, bool]:
    """``(cap, unlimited)`` for a budget field: ``None`` → ``(None, False)`` (unspecified),
    ``0``/``-1`` → ``(None, True)`` (unlimited), positive → ``(value, False)``."""
    if value is None:
        return None, False
    if value <= 0:
        return None, True
    return float(value), False


def nice_amount(amount: float) -> float:
    """Round to two significant figures (``2417`` → ``2400``)."""
    return float(f"{amount:.2g}") if amount > 0 else 0.0


_nice = nice_amount


def _money(amount: float) -> float:
    return float(round(amount)) if abs(amount) >= 100 else round(amount, 2)


def static_convert(amount: float, from_ccy: str, to_ccy: str) -> float | None:
    """Offline approximate conversion (deterministic; ``None`` for unknown currencies)."""
    rate = fx.static_rate(from_ccy, to_ccy)
    return None if rate is None else amount * rate


@dataclass(frozen=True)
class CostDefaults:
    """Mid-range local prices (per person; ``hotel_night`` per room) in ``currency``."""

    currency: str
    price_index: float
    usd_rate: float
    breakfast: float
    lunch: float
    dinner: float
    cafe: float
    transit_ride: float
    hotel_night: float

    def _usd(self, usd: float) -> float:
        return _nice(usd * self.price_index * self.usd_rate)

    def meal(self, kind: str, price_level: int | None = None) -> float:
        """Typical meal price for ``kind`` (breakfast | lunch | dinner | cafe)."""
        base = {"breakfast": self.breakfast, "lunch": self.lunch, "dinner": self.dinner,
                "cafe": self.cafe}.get(kind, self.lunch)
        if price_level is None:
            return base
        return _nice(base * PRICE_LEVEL_FACTOR.get(price_level, 1.0))

    def activity(self, category: str) -> float:
        """Typical entrance/activity cost for a candidate category (0 = usually free)."""
        return self._usd(ACTIVITY_USD.get(category, 0.0))

    def airport_transfer(self, distance_km: float) -> float:
        """One-way public-transport airport transfer fare per person."""
        return self._usd(AIRPORT_TRANSFER_USD_BASE + AIRPORT_TRANSFER_USD_PER_KM * distance_km)


def local_cost_defaults(country: str, currency: str) -> CostDefaults:
    """Mid-range defaults for a destination, in ``currency`` (USD if the currency is unknown)."""
    rate = fx.static_rate("USD", currency)
    if rate is None:
        currency, rate = "USD", 1.0
    index = PRICE_INDEX.get((country or "").upper(), DEFAULT_PRICE_INDEX)
    values = {key: _nice(usd * index * rate) for key, usd in BASE_USD.items()}
    return CostDefaults(currency=currency, price_index=index, usd_rate=rate, **values)


# --------------------------------------------------------------------------- planning caps


@dataclass(frozen=True)
class SpendingCaps:
    """Soft per-person caps in local currency used while planning (``None`` = no cap)."""

    meal: dict[str, float | None] = field(default_factory=dict)
    activities_total: float | None = None
    source: str = "none"
    """``restaurant_budget`` | ``food_budget`` | ``total_budget`` | ``none``."""


def _per_person_local(inputs: PlanInputs, amount: float, currency: str | None) -> float | None:
    local = static_convert(amount, currency or inputs.budget_currency, inputs.currency)
    if local is None:
        return None
    return local / inputs.adults if inputs.budget_basis == "total" else local


def _hotel_per_person_local(inputs: PlanInputs, defaults: CostDefaults) -> float:
    cap, _ = budget_value(inputs.budget_hotel)
    if cap is not None:
        return _per_person_local(inputs, cap, None) or 0.0
    room_nights = defaults.hotel_night * inputs.nights * rooms_needed(inputs.adults)
    return (static_convert(room_nights, defaults.currency, inputs.currency) or 0.0) / inputs.adults


def _flight_per_person_local(inputs: PlanInputs) -> float:
    if inputs.flight_price_total is not None:
        local = static_convert(
            inputs.flight_price_total, inputs.flight_currency or inputs.budget_currency, inputs.currency
        )
        return (local or 0.0) / inputs.adults
    cap, _ = budget_value(inputs.budget_flight)
    if cap is None:
        return 0.0
    return _per_person_local(inputs, cap, None) or 0.0


def spending_caps(inputs: PlanInputs, defaults: CostDefaults) -> SpendingCaps:
    """Per-person planning caps: restaurant budget → food budget → share of the total budget
    left after flight and hotel; unlimited/unspecified → no cap (mid-range defaults)."""
    days = max(1, len(inputs.dates))
    meal: dict[str, float | None] = dict.fromkeys(MEAL_SHARE_OF_DAILY_FOOD)
    source = "none"
    activities_cap: float | None = None

    if inputs.restaurant_budget_max:
        per_meal = static_convert(
            inputs.restaurant_budget_max,
            inputs.restaurant_budget_currency or inputs.currency,
            inputs.currency,
        )
        if per_meal is not None:
            meal = {"breakfast": per_meal * 0.6, "lunch": per_meal, "dinner": per_meal,
                    "cafe": per_meal * 0.6}
            source = "restaurant_budget"

    remaining: float | None = None
    total_cap, _ = budget_value(inputs.budget_total)
    if total_cap is not None:
        total_pp = _per_person_local(inputs, total_cap, None)
        if total_pp is not None:
            remaining = max(
                0.0, total_pp - _flight_per_person_local(inputs) - _hotel_per_person_local(inputs, defaults)
            )

    if source == "none":
        food_cap, _ = budget_value(inputs.budget_food)
        daily: float | None = None
        if food_cap is not None:
            food_pp = _per_person_local(inputs, food_cap, None)
            daily = food_pp / days if food_pp is not None else None
            source = "food_budget" if daily is not None else source
        elif remaining is not None:
            daily = remaining * LOCAL_SPENDING_SPLIT["food"] / days
            source = "total_budget"
        if daily is not None and daily >= defaults.breakfast + defaults.lunch * 0.5:
            meal = {kind: daily * share for kind, share in MEAL_SHARE_OF_DAILY_FOOD.items()}
        elif daily is not None:
            source = "none"  # budget already exhausted: keep mid-range defaults, report over budget

    act_cap, _ = budget_value(inputs.budget_activities)
    if act_cap is not None:
        activities_cap = _per_person_local(inputs, act_cap, None)
    elif remaining:
        activities_cap = remaining * LOCAL_SPENDING_SPLIT["activities"]
    return SpendingCaps(meal=meal, activities_total=activities_cap, source=source)


def rooms_needed(adults: int) -> int:
    """Hotel rooms for the party (two adults share a room)."""
    return max(1, (adults + 1) // 2)


# --------------------------------------------------------------------------- plan costs


def item_cost_local(amount: float | None, currency: str | None, local: str) -> float:
    """Item cost converted to the local currency with the static table (0 when unknown)."""
    if not amount:
        return 0.0
    converted = static_convert(amount, currency or local, local)
    return converted if converted is not None else 0.0


def day_transport_cost(day: PlanDay, defaults: CostDefaults) -> float:
    """Per-person transport cost of a day in ``defaults.currency``: airport-transfer items
    (``kind == "transit"``) at their ``est_cost`` plus one fare per in-city transit hop."""
    total = 0.0
    for item in day.items:
        hop = item.travel_from_prev
        if item.kind == "transit":
            if item.est_cost is not None:
                total += item_cost_local(item.est_cost, item.cost_currency, defaults.currency)
            elif hop is not None:
                total += defaults.airport_transfer(hop.distance_km)
        elif hop is not None and hop.mode == "transit":
            total += defaults.transit_ride
    return total


def day_cost(day: PlanDay, local_currency: str, defaults: CostDefaults) -> float:
    """Per-person estimated cost of a day in local currency (items + transport)."""
    items = sum(
        item_cost_local(item.est_cost, item.cost_currency, local_currency)
        for item in day.items
        if item.kind != "transit"
    )
    transport = day_transport_cost(day, defaults)
    if defaults.currency != local_currency:
        transport = static_convert(transport, defaults.currency, local_currency) or 0.0
    return items + transport


class _Converter:
    """Converts into the budget currency via ``fx.convert`` and remembers rate sources."""

    def __init__(self, target: str) -> None:
        self.target = target
        self.sources: dict[str, str] = {}
        self.missing: set[str] = set()

    def __call__(self, amount: float, currency: str) -> float | None:
        src = currency.upper()
        if src == self.target or amount == 0:
            return amount
        try:
            _, source = fx.get_rate(src, self.target)
            converted = fx.convert(amount, src, self.target)
        except fx.FxError:
            self.missing.add(src)
            return None
        self.sources[f"{src}->{self.target}"] = source
        return converted

    def total(self, amounts: Iterable[tuple[float, str]]) -> float | None:
        """Sum of converted amounts (``None`` if nothing could be converted)."""
        values = [v for amount, ccy in amounts if (v := self(amount, ccy)) is not None]
        return sum(values) if values else None


def _plan_amounts(
    plan: ItineraryPlan, kinds: set[str], adults: int, local: str
) -> list[tuple[float, str]]:
    return [
        (item.est_cost * adults, item.cost_currency or local)
        for day in plan.days
        for item in day.items
        if item.kind in kinds and item.est_cost
    ]


_NOTES = {
    "en": {
        "basis_pp": "Amounts are per person ({adults} travellers).",
        "basis_total": "Amounts are totals for {adults} traveller(s).",
        "hotel_est": "Hotel estimated at a mid-range ~{price} {ccy}/night × {nights} night(s).",
        "hotel_budget": "Hotel estimate equals your hotel budget.",
        "flight_unknown": "Flight price unknown — not included.",
        "unlimited": "Unlimited categories ({names}) use mid-range estimates.",
        "fx_static": "Approximate offline exchange rates used for {pairs}.",
        "fx_missing": "No exchange rate for {codes}; those costs are excluded.",
        "discretionary": "Shopping/other are your allowances, not estimates.",
    },
    "ko": {
        "basis_pp": "금액은 1인 기준입니다 (여행자 {adults}명).",
        "basis_total": "금액은 여행자 {adults}명 전체 합계입니다.",
        "hotel_est": "숙박비는 중간 가격대 1박 약 {price} {ccy} × {nights}박으로 추정했습니다.",
        "hotel_budget": "숙박비 추정치는 입력한 숙박 예산과 같습니다.",
        "flight_unknown": "항공권 가격을 알 수 없어 제외했습니다.",
        "unlimited": "무제한 항목({names})은 중간 가격대 기준으로 추정했습니다.",
        "fx_static": "{pairs} 환율은 오프라인 근사값을 사용했습니다.",
        "fx_missing": "{codes} 환율을 찾을 수 없어 해당 비용은 제외했습니다.",
        "discretionary": "쇼핑/기타는 추정치가 아니라 입력한 예산입니다.",
    },
}


def build_budget_summary(
    inputs: PlanInputs,
    plan: ItineraryPlan,
    flight_cost: float | None = None,
    flight_currency: str | None = None,
) -> BudgetSummary:
    """Compare the plan's estimated costs with the trip budget, in ``inputs.budget_currency``.

    ``flight_cost`` = flight price for **all** travellers in ``flight_currency`` (defaults:
    ``inputs.flight_price_total`` / ``inputs.flight_currency`` / the budget currency). Lines:
    flight, hotel (budget if capped, else mid-range estimate per night), food / transport /
    activities (from plan items, × adults), shopping / other (the allowance itself).
    Amounts are expressed per ``inputs.budget_basis``. ``within_budget`` is ``None`` when no
    cap applies. Extra fields: ``basis``, ``adults``, ``fx`` (rate source per pair),
    ``unlimited_total``, ``remaining_after_flight``.
    """
    texts = _NOTES.get(inputs.language, _NOTES["en"])
    target = inputs.budget_currency
    adults = inputs.adults
    divisor = adults if inputs.budget_basis == "per_person" else 1
    local = plan.currency_local or inputs.currency
    defaults = local_cost_defaults(inputs.country, local)
    convert = _Converter(target)
    notes: list[str] = []

    def scaled(group_amount: float | None) -> float | None:
        return None if group_amount is None else _money(group_amount / divisor)

    estimates: dict[str, float | None] = {}
    amount = flight_cost if flight_cost is not None else inputs.flight_price_total
    currency = flight_currency or inputs.flight_currency or target
    estimates["flight"] = scaled(convert(amount, currency)) if amount is not None else None
    if amount is None:
        notes.append(texts["flight_unknown"])

    hotel_cap, _ = budget_value(inputs.budget_hotel)
    nights = 0 if getattr(plan, "layover", False) else inputs.nights  # airport-only stay
    if hotel_cap is not None:
        estimates["hotel"] = hotel_cap
        notes.append(texts["hotel_budget"])
    else:
        room_nights = defaults.hotel_night * nights * rooms_needed(adults)
        estimates["hotel"] = scaled(convert(room_nights, defaults.currency))
        notes.append(texts["hotel_est"].format(
            price=f"{defaults.hotel_night:,.0f}", ccy=defaults.currency, nights=nights
        ))

    estimates["food"] = scaled(convert.total(_plan_amounts(plan, {"meal"}, adults, local)) or 0.0)
    transport_local = sum(day_transport_cost(day, defaults) for day in plan.days) * adults
    estimates["transport"] = scaled(convert(transport_local, defaults.currency) or 0.0)
    estimates["activities"] = scaled(
        convert.total(_plan_amounts(plan, {"activity", "free"}, adults, local)) or 0.0
    )
    for name in ("shopping", "other"):
        estimates[name] = budget_value(getattr(inputs, f"budget_{name}"))[0]

    lines: list[BudgetLine] = []
    unlimited_names: list[str] = []
    for name in BUDGET_CATEGORIES:
        cap, unlimited = budget_value(getattr(inputs, f"budget_{name}"))
        if unlimited:
            unlimited_names.append(name)
        lines.append(BudgetLine(category=name, budget=cap, estimated=estimates[name], unlimited=unlimited))
    if any(line.budget is not None for line in lines if line.category in ("shopping", "other")):
        notes.append(texts["discretionary"])

    total_cap, total_unlimited = budget_value(inputs.budget_total)
    total_estimated = _money(sum(v for v in estimates.values() if v is not None))
    checks = [line.estimated <= line.budget for line in lines
              if line.budget is not None and line.estimated is not None]
    if total_cap is not None:
        checks.append(total_estimated <= total_cap)
    within = all(checks) if checks else None

    if unlimited_names or total_unlimited:
        notes.append(texts["unlimited"].format(names=", ".join(unlimited_names or ["total"])))
    static_pairs = sorted(pair for pair, source in convert.sources.items() if source == "static")
    if static_pairs:
        notes.append(texts["fx_static"].format(pairs=", ".join(static_pairs)))
    if convert.missing:
        notes.append(texts["fx_missing"].format(codes=", ".join(sorted(convert.missing))))
    basis_key = "basis_pp" if inputs.budget_basis == "per_person" else "basis_total"
    notes.insert(0, texts[basis_key].format(adults=adults))

    flight_est = estimates["flight"]
    return BudgetSummary(
        currency=target,
        lines=lines,
        total_budget=total_cap,
        total_estimated=total_estimated,
        within_budget=within,
        notes="\n".join(notes),
        basis=inputs.budget_basis,
        adults=adults,
        unlimited_total=total_unlimited,
        fx=dict(sorted(convert.sources.items())),
        remaining_after_flight=(
            _money(total_cap - flight_est) if total_cap is not None and flight_est is not None else None
        ),
    )


def over_budget_lines(summary: BudgetSummary) -> list[str]:
    """Categories whose estimate exceeds their cap (``"total"`` included)."""
    names = [line.category for line in summary.lines
             if line.budget is not None and line.estimated is not None and line.estimated > line.budget]
    if summary.total_budget is not None and (summary.total_estimated or 0) > summary.total_budget:
        names.append("total")
    return names
