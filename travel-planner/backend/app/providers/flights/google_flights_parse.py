"""Pure parsers for Google Flights result pages (DESIGN §9, §13.1).

Two data sources live in the server-rendered HTML (plain HTTP and ``page.content()`` alike):

1. ``<script class="ds:1">AF_initDataCallback({key: 'ds:1', hash: '5', data:[...], ...})`` — the
   JSON payload (index map in unpublished research notes, verified 2026-10-06):
   ``payload[2][0]`` = "Top departing flights", ``payload[3][0]`` = "Other departing flights";
   each row ``k``: ``k[0]`` itinerary, ``k[1] = [[None, PRICE], booking_token]`` (``k[1][0] == []``
   when Google shows "Total price is unavailable"); ``payload[5]`` = price insights;
   ``payload[25][0][1]`` = cheapest listed price.
2. The aria-labels of the result rows (``li div[role=link][aria-label]``) — the fallback when
   ds:1 is missing or its layout changed (:func:`parse_dom_rows`; English and Korean labels).
   :func:`ds1_state` tells which of the two a page needs.

Prices are the round-trip (or one-way) **total for all passengers** in the requested currency
(2 adults = exactly 2x the 1-adult price); ``price_per_person`` = total / adults. A round-trip
search lists *outbound* flights, each priced at the cheapest round trip starting with it, so
offers carry ``inbound=None`` and ``return_date`` (DESIGN §5). Unpriced rows are skipped.

Fixed-index parsing is brittle by nature: every row is parsed defensively (a malformed row is
skipped and counted) and :class:`Ds1FormatError` signals "layout changed, use the fallback".
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from selectolax.lexbor import LexborHTMLParser, LexborNode

from app.providers.flights.base import (
    FlightLeg,
    FlightOffer,
    FlightQuery,
    FlightSegment,
    normalize_flight_number,
)
from app.providers.flights.google_flights_tfs import (
    SelectedFlight,
    booking_link,
    max_stops_value,
    place_codes,
    selected_from_leg,
    user_link,
)

logger = logging.getLogger(__name__)

PROVIDER_KEY = "google_flights"
SOURCE_DS1 = "ds1"
SOURCE_DOM = "dom"
DS1_OK, DS1_NO_RESULTS = "ok", "no_results"
DS1_MISSING, DS1_UNDECODABLE, DS1_UNRECOGNISED = "missing", "undecodable", "unrecognised"
DS1_USABLE: frozenset[str] = frozenset({DS1_OK, DS1_NO_RESULTS})
""":func:`ds1_state` values that need no DOM fallback."""
GROUPS: tuple[tuple[int, str], ...] = ((2, "top"), (3, "other"))
"""``(payload index, name)`` of the two result groups; both are always parsed."""
PRICE_HISTORY_MAX_POINTS = 90
"""``payload[5][10][0]`` holds about two months (61 daily points) of ``[epoch_ms, total]``."""
ROW_SELECTOR = 'li div[role="link"][aria-label]'

# Itinerary (row[0]) indexes.
_IT_AIRLINES, _IT_SEGMENTS = 1, 2
_IT_FROM, _IT_DEP_DATE, _IT_DEP_TIME = 3, 4, 5
_IT_TO, _IT_ARR_DATE, _IT_ARR_TIME = 6, 7, 8
_IT_MINUTES, _IT_LAYOVERS, _IT_EXTRAS, _IT_CABINS = 9, 13, 22, 29
_IT_CO2_GRAMS = 7  # inside _IT_EXTRAS
# Segment indexes.
_SEG_OPERATOR, _SEG_FROM, _SEG_TO = 2, 3, 6
_SEG_DEP_TIME, _SEG_ARR_TIME, _SEG_MINUTES, _SEG_AIRCRAFT = 8, 10, 11, 17
_SEG_DEP_DATE, _SEG_ARR_DATE, _SEG_FLIGHT = 20, 21, 22
# Layover entry: [minutes, arrival IATA, departure IATA, ?, arrival name, city, dep. name, city].
_LAY_ARRIVAL, _LAY_DEPARTURE = 1, 2
# Price insights (payload[5]): [code, [_, current], [_, typical], [_, diff], [_, low], [_, high],
# ..., [10] history [[[epoch_ms, price], ...]] (absent for some searches)].
_INS_CODE, _INS_CURRENT, _INS_TYPICAL, _INS_LOW, _INS_HIGH, _INS_HISTORY = 0, 1, 2, 4, 5, 10
_P_INSIGHTS, _P_CHEAPEST = 5, 25

_DS1_RE = re.compile(r"AF_initDataCallback\(\s*\{\s*key:\s*'ds:1'")
_DATA_RE = re.compile(r"\bdata\s*:")
_ERROR_STATUS_RE = re.compile(r"errorHasStatus\s*:\s*true")
_IATA_RE = re.compile(r"^[A-Z]{3}$")
_JSON = json.JSONDecoder()

_BLOCK_MARKERS: tuple[tuple[str, str], ...] = (
    ('id="captcha-form"', "Google captcha page"),
    ("unusual traffic from your computer network", "Google 'unusual traffic' page"),
    ("/httpservice/retry/enablejs", "Google JavaScript challenge page"),
    ('action="https://consent.google.com', "Google consent wall"),
)
_BLOCK_STATUSES = frozenset({403, 429})


class Ds1FormatError(ValueError):
    """The ds:1 payload does not have the expected result layout (use the DOM fallback)."""


# --------------------------------------------------------------------------- small helpers


def _at(value: Any, *path: int) -> Any:
    """``value[i][j]...`` or ``None`` when any step is missing / not a list."""
    current = value
    for index in path:
        if not isinstance(current, list) or not -len(current) <= index < len(current):
            return None
        current = current[index]
    return current


def _int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _money(value: Any) -> float | None:
    number = _int(value)
    return float(number) if number is not None and number > 0 else None


def _iata(value: Any) -> str:
    code = value.strip().upper() if isinstance(value, str) else ""
    if not _IATA_RE.match(code):
        raise ValueError(f"not an IATA airport code: {value!r}")
    return code


def _text(value: Any) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


def _local_iso(day: Any, clock: Any) -> str:
    """``[2026, 11, 20]`` + ``[13, 55]`` -> ``"2026-11-20T13:55"``.

    Google omits zero components: ``[8]`` = 08:00, ``[None, 31]`` = 00:31, ``None``/``[]`` = 00:00.
    """
    if not isinstance(day, list) or len(day) < 3:
        raise ValueError(f"bad date {day!r}")
    padded = [*(clock if isinstance(clock, list) else []), None, None]
    hour, minute = padded[0] or 0, padded[1] or 0
    value = date(int(day[0]), int(day[1]), int(day[2]))
    if not (0 <= int(hour) <= 23 and 0 <= int(minute) <= 59):
        raise ValueError(f"bad time {clock!r}")
    return f"{value.isoformat()}T{int(hour):02d}:{int(minute):02d}"


def expected_airports(code: str) -> frozenset[str] | None:
    """Airports a query code may match (metro expanded); ``None`` = do not validate (city id)."""
    places = place_codes(code)
    if any(not _IATA_RE.match(place) for place in places):
        return None
    return frozenset(places)


def search_descriptor(query: FlightQuery) -> dict[str, Any]:
    """The original search (stored in ``raw["search"]``) so follow-up fetches repeat it."""
    return {
        "origin": query.origin,
        "destination": query.destination,
        "direct_only": query.direct_only,
        "max_stops": max_stops_value(query),
    }


# --------------------------------------------------------------------------- page checks


def detect_block(html: str, url: str = "", status: int | None = None) -> str | None:
    """Why the page is a block / consent / captcha page, or ``None`` for a normal page.

    Checks HTTP 429/403, a ``/sorry/`` or ``consent.google.com`` URL, and well-known markers of
    Google's captcha, "unusual traffic", JavaScript-challenge and consent pages. (Normal result
    pages mention "recaptcha" in a config list, so that word alone is not a signal.)
    """
    if status in _BLOCK_STATUSES:
        return f"HTTP {status} from Google"
    lowered = (url or "").lower()
    if "/sorry/" in lowered:
        return "Google 'unusual traffic' check (/sorry/ page)"
    if "consent.google.com" in lowered:
        return "Google consent wall (consent.google.com)"
    for marker, reason in _BLOCK_MARKERS:
        if marker in html:
            return reason
    return None


def has_ds1(html: str) -> bool:
    """True when the page contains the ds:1 ``AF_initDataCallback`` script."""
    return _DS1_RE.search(html) is not None


def extract_ds1(html: str) -> list[Any] | None:
    """The ds:1 payload of a results page.

    Returns ``None`` when the ds:1 script is missing or its data cannot be decoded, and ``[]``
    when Google answered "no flights": an error payload (``..., errorHasStatus: true``, e.g. a
    past date) or an empty one (``data:[]``, e.g. an unknown route). Otherwise the payload list
    (check it with :func:`parse_offers`).
    """
    match = _DS1_RE.search(html)
    if match is None:
        return None
    end = html.find("</script>", match.end())
    script = html[match.end() : end if end >= 0 else len(html)]
    data = _DATA_RE.search(script)
    if data is None:
        return None
    body = script[data.end() :].lstrip()
    try:
        payload, consumed = _JSON.raw_decode(body)
    except ValueError:
        return [] if _ERROR_STATUS_RE.search(body) else None
    if _ERROR_STATUS_RE.search(body[consumed:]):
        logger.info("Google Flights answered with an error status (no flights for this search)")
        return []
    return payload if isinstance(payload, list) else None


def is_no_results(payload: list[Any]) -> bool:
    """True for the empty / error payload :func:`extract_ds1` returns as ``[]``."""
    return len(payload) == 0


def ds1_state(html: str) -> str:
    """How usable the page's ds:1 script is, without logging per-row problems.

    ``"missing"`` (no ds:1 script), ``"undecodable"`` (script present, data not JSON),
    ``"no_results"`` (Google's empty / error answer), ``"unrecognised"`` (decodes, but no
    result groups or no row with the known layout) or ``"ok"``.
    """
    payload = extract_ds1(html)
    if payload is None:
        return DS1_UNDECODABLE if has_ds1(html) else DS1_MISSING
    if is_no_results(payload):
        return DS1_NO_RESULTS
    try:
        rows = [row for _, row in iter_rows(payload)]
    except Ds1FormatError:
        return DS1_UNRECOGNISED
    if rows and not any(_row_parses(row) for row in rows):
        return DS1_UNRECOGNISED
    return DS1_OK


def _row_parses(row: Any) -> bool:
    try:
        parse_itinerary(row, "check")
    except (ValueError, TypeError, IndexError):
        return False
    return True


# --------------------------------------------------------------------------- ds:1 rows


@dataclass
class Itinerary:
    """One ds:1 result row: an outbound (or return) leg and its listed total price."""

    leg: FlightLeg
    price: float | None  # total for all passengers; None = "price unavailable"
    group: str  # "top" | "other"
    extras: dict[str, Any] = field(default_factory=dict)


def _segment(raw: Any) -> FlightSegment:
    flight = _at(raw, _SEG_FLIGHT) or []
    carrier = _text(_at(flight, 0))
    number = _text(_at(flight, 1))
    return FlightSegment(
        carrier_code=carrier.upper() if carrier else None,
        carrier_name=_text(_at(flight, 3)),
        flight_number=normalize_flight_number(carrier + number) if carrier and number else None,
        departure_airport=_iata(_at(raw, _SEG_FROM)),
        arrival_airport=_iata(_at(raw, _SEG_TO)),
        departure_time=_local_iso(_at(raw, _SEG_DEP_DATE), _at(raw, _SEG_DEP_TIME)),
        arrival_time=_local_iso(_at(raw, _SEG_ARR_DATE), _at(raw, _SEG_ARR_TIME)),
        duration_minutes=_int(_at(raw, _SEG_MINUTES)),
        operating_carrier=_text(_at(raw, _SEG_OPERATOR)),
        aircraft=_text(_at(raw, _SEG_AIRCRAFT)),
    )


def _airport_changes(segments: Sequence[FlightSegment], layovers: Any) -> list[str]:
    """``"KIX-ITM"`` for every connection where the traveller changes airports."""
    changes: list[str] = []
    for entry in layovers if isinstance(layovers, list) else []:
        arrival, departure = _at(entry, _LAY_ARRIVAL), _at(entry, _LAY_DEPARTURE)
        if isinstance(arrival, str) and isinstance(departure, str) and arrival != departure:
            changes.append(f"{arrival}-{departure}")
    for first, second in zip(segments, segments[1:], strict=False):
        change = f"{first.arrival_airport}-{second.departure_airport}"
        if first.arrival_airport != second.departure_airport and change not in changes:
            changes.append(change)
    return changes


def _row_price(row: Any) -> float | None:
    return _money(_at(row, 1, 0, 1))


def parse_itinerary(row: Any, group: str) -> Itinerary:
    """One ds:1 row -> :class:`Itinerary` (``ValueError``/``TypeError`` when malformed)."""
    itinerary = _at(row, 0)
    raw_segments = _at(itinerary, _IT_SEGMENTS)
    if not isinstance(raw_segments, list) or not raw_segments:
        raise ValueError("itinerary without segments")
    segments = [_segment(raw) for raw in raw_segments]
    layovers = _at(itinerary, _IT_LAYOVERS)
    airlines = [name for name in (_at(itinerary, _IT_AIRLINES) or []) if _text(name)]
    leg = FlightLeg(
        departure_airport=_iata(_at(itinerary, _IT_FROM) or segments[0].departure_airport),
        arrival_airport=_iata(_at(itinerary, _IT_TO) or segments[-1].arrival_airport),
        departure_time=_local_iso(
            _at(itinerary, _IT_DEP_DATE), _at(itinerary, _IT_DEP_TIME)
        ),
        arrival_time=_local_iso(_at(itinerary, _IT_ARR_DATE), _at(itinerary, _IT_ARR_TIME)),
        duration_minutes=_int(_at(itinerary, _IT_MINUTES)),
        stops=len(segments) - 1,
        airlines=list(dict.fromkeys(airlines))
        or list(dict.fromkeys(s.carrier_name for s in segments if s.carrier_name)),
        flight_numbers=[s.flight_number for s in segments if s.flight_number],
        segments=segments,
        layovers=[
            code
            for code in (_at(entry, _LAY_ARRIVAL) for entry in layovers or [])
            if isinstance(code, str)
        ]
        or [s.arrival_airport for s in segments[:-1]],
    )
    return Itinerary(leg=leg, price=_row_price(row), group=group, extras=_extras(itinerary, leg))


def _extras(itinerary: Any, leg: FlightLeg) -> dict[str, Any]:
    extras: dict[str, Any] = {}
    changes = _airport_changes(leg.segments, _at(itinerary, _IT_LAYOVERS))
    if changes:
        extras["self_transfer"] = True
        extras["airport_changes"] = changes
    cabins = _at(itinerary, _IT_CABINS)
    if isinstance(cabins, list) and cabins:
        extras["mixed_cabin"] = True
    co2 = _int(_at(itinerary, _IT_EXTRAS, _IT_CO2_GRAMS))
    if co2:
        extras["co2_grams"] = co2
    return extras


def iter_rows(payload: list[Any]) -> Iterator[tuple[str, Any]]:
    """``(group, row)`` for every row of both result groups (payload[2] and payload[3])."""
    if len(payload) <= max(index for index, _ in GROUPS):
        raise Ds1FormatError(f"ds:1 payload has {len(payload)} entries; expected result groups")
    for index, name in GROUPS:
        section = payload[index]
        if section is None:
            continue
        rows = _at(section, 0)
        if rows is None:
            continue
        if not isinstance(rows, list):
            raise Ds1FormatError(f"ds:1 group {index} is not a list")
        for row in rows:
            yield name, row


def parse_itineraries(payload: list[Any]) -> list[Itinerary]:
    """Every parsable row of both groups, in page order (priced or not).

    Malformed rows are skipped (logged); :class:`Ds1FormatError` when the payload has no result
    groups or rows exist but none could be parsed.
    """
    parsed: list[Itinerary] = []
    failed = 0
    for group, row in iter_rows(payload):
        try:
            parsed.append(parse_itinerary(row, group))
        except (ValueError, TypeError, IndexError) as exc:
            failed += 1
            logger.debug("Skipping malformed Google Flights row: %s", exc)
    if failed and not parsed:
        raise Ds1FormatError(f"none of the {failed} ds:1 rows could be parsed")
    if failed:
        logger.warning("Google Flights: skipped %d malformed result row(s)", failed)
    return parsed


# --------------------------------------------------------------------------- price insights


def price_level(current: float | None, low: float | None, high: float | None) -> str | None:
    """``"low"``/``"typical"``/``"high"`` from the typical range (Google's code is unreliable)."""
    if current is None or low is None or high is None:
        return None
    if current < low:
        return "low"
    if current > high:
        return "high"
    return "typical"


def _price_history(value: Any) -> list[list[float]]:
    """``[[[epoch_ms, total], ...]]`` (extra nesting as seen live) -> ``[[epoch_s, total]]``."""
    entries = value if isinstance(value, list) else []
    if entries and isinstance(_at(entries, 0, 0), list):
        entries = entries[0]
    points: list[list[float]] = []
    for entry in entries:
        stamp, price = _int(_at(entry, 0)), _money(_at(entry, 1))
        if stamp is not None and price is not None:
            points.append([stamp // 1000, price])  # epoch seconds, like SerpApi
    return points[-PRICE_HISTORY_MAX_POINTS:]


def parse_price_insights(payload: list[Any]) -> dict[str, Any] | None:
    """``payload[5]`` (+ ``payload[25]``) as a SerpApi-like dict, ``None`` if absent.

    Keys: ``lowest_price``, ``typical_price``, ``typical_price_range`` ``[low, high]``,
    ``price_level`` (derived from the range), ``level_code`` (Google's raw code),
    ``cheapest_listed`` and, when Google sent it, ``price_history`` ``[[epoch_s, total], ...]``.
    All amounts are totals for all passengers.
    """
    block = _at(payload, _P_INSIGHTS)
    cheapest = _money(_at(payload, _P_CHEAPEST, 0, 1))
    if not isinstance(block, list):
        return {"cheapest_listed": cheapest} if cheapest is not None else None
    current = _money(_at(block, _INS_CURRENT, 1))
    low, high = _money(_at(block, _INS_LOW, 1)), _money(_at(block, _INS_HIGH, 1))
    insights: dict[str, Any] = {
        "lowest_price": current,
        "typical_price": _money(_at(block, _INS_TYPICAL, 1)),
        "typical_price_range": [low, high],
        "price_level": price_level(current, low, high),
        "level_code": _int(_at(block, _INS_CODE)),
    }
    if cheapest is not None:
        insights["cheapest_listed"] = cheapest
    history = _price_history(_at(block, _INS_HISTORY))
    if history:
        insights["price_history"] = history
    return insights


def _insights_summary(insights: dict[str, Any] | None) -> dict[str, Any] | None:
    if not insights:
        return None
    return {key: value for key, value in insights.items() if key != "price_history"}


# --------------------------------------------------------------------------- offers


def _matches(leg: FlightLeg, origins: frozenset[str] | None, dests: frozenset[str] | None) -> bool:
    return (origins is None or leg.departure_airport in origins) and (
        dests is None or leg.arrival_airport in dests
    )


def offer_links(
    query: FlightQuery, selected: Sequence[SelectedFlight] | None
) -> tuple[str, str]:
    """``(booking_url, source_url)`` (hl=ko, user-facing) for an outbound of ``query``.

    ``source_url`` is the search itself. ``booking_url`` selects the outbound: for round trips
    the return-flight list of that outbound, for one-way trips the "Booking options" page.
    """
    source = user_link(query)
    if not selected:
        return source, source
    if query.is_round_trip:
        return user_link(query, outbound=selected), source
    return booking_link(query, selected), source


def _make_offer(
    query: FlightQuery,
    leg: FlightLeg,
    price: float,
    raw: dict[str, Any],
    selected: Sequence[SelectedFlight] | None,
    provider_key: str,
) -> FlightOffer:
    adults = max(query.adults, 1)
    booking, source = offer_links(query, selected)
    return FlightOffer(
        provider=provider_key,
        outbound=leg,
        inbound=None,
        price_total=price,
        price_per_person=round(price / adults, 2),
        currency=query.currency.upper(),
        cabin=query.cabin,
        adults=adults,
        booking_url=booking,
        source_url=source,
        return_date=query.return_date.isoformat() if query.return_date is not None else None,
        raw=raw,
    )


def _keep_cheapest(offers: dict[str, FlightOffer], offer: FlightOffer) -> None:
    key = offer.offer_key()
    if key not in offers or offer.price_total < offers[key].price_total:
        offers[key] = offer


def _attach_history(offers: list[FlightOffer], insights: dict[str, Any] | None) -> None:
    """Store the full insights (with ``price_history``) once per search, on its cheapest offer."""
    if not offers or not insights or "price_history" not in insights:
        return
    cheapest = min(offers, key=lambda offer: offer.price_total)
    cheapest.raw["price_insights"] = dict(insights)


def parse_offers(
    payload: list[Any], query: FlightQuery, *, provider_key: str = PROVIDER_KEY
) -> list[FlightOffer]:
    """Priced offers of a ds:1 payload (both groups), cheapest per ``offer_key``, page order.

    Unpriced rows and rows whose airports do not match the query (metro codes expanded) are
    skipped. ``raw`` holds ``source``/``group``/``search``, the insights summary, and the
    ``self_transfer``/``airport_changes``/``mixed_cabin``/``co2_grams`` flags when present.
    ``[]`` for the no-results payload; :class:`Ds1FormatError` for an unknown layout.
    """
    if is_no_results(payload):
        return []
    itineraries = parse_itineraries(payload)
    insights = parse_price_insights(payload)
    summary = _insights_summary(insights)
    origins, dests = expected_airports(query.origin), expected_airports(query.destination)
    offers: dict[str, FlightOffer] = {}
    unpriced = mismatched = 0
    for itinerary in itineraries:
        if itinerary.price is None:
            unpriced += 1
            continue
        if not _matches(itinerary.leg, origins, dests):
            mismatched += 1
            continue
        raw: dict[str, Any] = {
            "source": SOURCE_DS1,
            "group": itinerary.group,
            "search": search_descriptor(query),
            **itinerary.extras,
        }
        if summary:
            raw["price_insights"] = dict(summary)
        selected = selected_from_leg(itinerary.leg)
        offer = _make_offer(query, itinerary.leg, itinerary.price, raw, selected, provider_key)
        _keep_cheapest(offers, offer)
    if unpriced or mismatched:
        logger.debug(
            "Google Flights: skipped %d unpriced and %d off-route row(s)", unpriced, mismatched
        )
    result = list(offers.values())
    _attach_history(result, insights)
    return result


@dataclass(frozen=True)
class ReturnOption:
    """A return flight listed after selecting an outbound, priced at the round-trip total."""

    price: float
    leg: FlightLeg
    group: str


def parse_return_options(payload: list[Any], query: FlightQuery) -> list[ReturnOption]:
    """Priced return flights (destination -> origin) of an outbound-selected search, page order."""
    if is_no_results(payload):
        return []
    origins, dests = expected_airports(query.destination), expected_airports(query.origin)
    return [
        ReturnOption(itinerary.price, itinerary.leg, itinerary.group)
        for itinerary in parse_itineraries(payload)
        if itinerary.price is not None and _matches(itinerary.leg, origins, dests)
    ]


# --------------------------------------------------------------------------- DOM fallback


@dataclass
class RowLabel:
    """What a result row's aria-label says (English or Korean)."""

    price: float | None
    airlines: list[str]
    stops: int | None
    departure_time: str | None  # local ISO
    arrival_time: str | None
    duration_minutes: int | None
    airport_change: bool = False
    self_transfer: bool = False


_SPACES_RE = re.compile(r"\s+")  # also U+202F / U+00A0 (str.isspace)
_MONTHS = {
    name: index
    for index, name in enumerate(
        ("january", "february", "march", "april", "may", "june", "july", "august",
         "september", "october", "november", "december"),
        start=1,
    )
}
_EN_PRICE_RE = re.compile(r"\bFrom ([\d,]+) ")
_EN_UNPRICED = "Total price is unavailable"
_EN_STOPS_RE = re.compile(r"\b(?:Nonstop|(\d+) stops?) flight with (.+?)\. (?=Operated by|Leaves)")
_EN_TIMES_RE = re.compile(
    r"Leaves .+? at (?P<dep_time>\d{1,2}:\d{2} ?[AP]M) on (?:[A-Za-z]+, )?"
    r"(?P<dep_month>[A-Za-z]+) (?P<dep_day>\d{1,2}) and arrives at .+? at "
    r"(?P<arr_time>\d{1,2}:\d{2} ?[AP]M) on (?:[A-Za-z]+, )?(?P<arr_month>[A-Za-z]+) "
    r"(?P<arr_day>\d{1,2})\."
)
_EN_DURATION_RE = re.compile(r"Total duration (?:(\d+) hr)? ?(?:(\d+) min)?\.")
_EN_TRANSFER = "Transfer here from"
_KO_PRICE_RE = re.compile(r"최저 ([\d,]+) ")
_KO_UNPRICED = "총 가격 정보가 제공되지 않습니다"
_KO_STOPS_RE = re.compile(r"(?:^|\. )([^.]+?)의 (?:직항|(\d+)회 경유) 항공편입니다")
_KO_TIMES_RE = re.compile(
    r"(?P<dep_month>\d{1,2})월 (?P<dep_day>\d{1,2}) (?P<dep_ampm>오전|오후) "
    r"(?P<dep_time>\d{1,2}:\d{2})에 .+?에서 출발하여 (?:\S+요일, )?"
    r"(?P<arr_month>\d{1,2})월 (?P<arr_day>\d{1,2}) (?P<arr_ampm>오전|오후) "
    r"(?P<arr_time>\d{1,2}:\d{2})에 .+?에 도착합니다"
)
_KO_DURATION_RE = re.compile(r"총 비행 시간은 (?:(\d+)시간)? ?(?:(\d+)분)?입니다")
_KO_TRANSFER = "여기에서 환승합니다"
_SELF_TRANSFER_RE = re.compile(r"self[- ]transfer|셀프 환승|자가 환승", re.IGNORECASE)
_TIM_RE = re.compile(r"itinerary=([A-Za-z0-9,\-]+)")
_TIM_SEGMENT_RE = re.compile(r"^([A-Z]{3})-([A-Z]{3})-([A-Z0-9]{2})-(\d{1,4}[A-Z]?)-(\d{8})$")
_ROUTE_CODE_RE = re.compile(r"^([A-Z]{3})")
_PRICE_ARIA_RE = re.compile(r"^([\d,]+) ")


def _infer_date(month: int, day: int, near: date) -> date:
    """The ``month``/``day`` closest to ``near`` (labels omit the year)."""
    candidates = []
    for year in (near.year - 1, near.year, near.year + 1):
        try:
            candidates.append(date(year, month, day))
        except ValueError:
            continue
    if not candidates:
        raise ValueError(f"invalid month/day {month}/{day}")
    return min(candidates, key=lambda value: abs(value - near))


def _clock_24h(clock: str, afternoon: bool) -> str:
    hour, minute = (int(part) for part in clock.split(":"))
    if not (1 <= hour <= 12 and 0 <= minute <= 59):
        raise ValueError(f"bad 12-hour time {clock!r}")
    return f"{hour % 12 + (12 if afternoon else 0):02d}:{minute:02d}"


def _minutes(hours: str | None, minutes: str | None) -> int | None:
    if hours is None and minutes is None:
        return None
    return int(hours or 0) * 60 + int(minutes or 0)


def _split_airlines(text: str) -> list[str]:
    parts = re.split(r", | and ", text)
    return [part.strip() for part in parts if part.strip()]


def _en_label(text: str, near: date) -> RowLabel | None:
    price_match = _EN_PRICE_RE.search(text)
    if price_match is None and _EN_UNPRICED not in text:
        return None
    stops = _EN_STOPS_RE.search(text)
    times = _EN_TIMES_RE.search(text)
    duration = _EN_DURATION_RE.search(text)
    dep = arr = None
    if times:
        dep_day = _infer_date(_MONTHS[times["dep_month"].lower()], int(times["dep_day"]), near)
        arr_day = _infer_date(_MONTHS[times["arr_month"].lower()], int(times["arr_day"]), dep_day)
        dep_clock = times["dep_time"].replace(" ", "")
        arr_clock = times["arr_time"].replace(" ", "")
        dep = f"{dep_day}T{_clock_24h(dep_clock[:-2], dep_clock.endswith('PM'))}"
        arr = f"{arr_day}T{_clock_24h(arr_clock[:-2], arr_clock.endswith('PM'))}"
    return RowLabel(
        price=_money(int(price_match.group(1).replace(",", ""))) if price_match else None,
        airlines=_split_airlines(stops.group(2)) if stops else [],
        stops=(int(stops.group(1)) if stops.group(1) else 0) if stops else None,
        departure_time=dep,
        arrival_time=arr,
        duration_minutes=_minutes(*duration.groups()) if duration else None,
        airport_change=_EN_TRANSFER in text,
        self_transfer=_SELF_TRANSFER_RE.search(text) is not None,
    )


def _ko_label(text: str, near: date) -> RowLabel | None:
    price_match = _KO_PRICE_RE.search(text)
    if price_match is None and _KO_UNPRICED not in text:
        return None
    stops = _KO_STOPS_RE.search(text)
    times = _KO_TIMES_RE.search(text)
    duration = _KO_DURATION_RE.search(text)
    dep = arr = None
    if times:
        dep_day = _infer_date(int(times["dep_month"]), int(times["dep_day"]), near)
        arr_day = _infer_date(int(times["arr_month"]), int(times["arr_day"]), dep_day)
        dep = f"{dep_day}T{_clock_24h(times['dep_time'], times['dep_ampm'] == '오후')}"
        arr = f"{arr_day}T{_clock_24h(times['arr_time'], times['arr_ampm'] == '오후')}"
    return RowLabel(
        price=_money(int(price_match.group(1).replace(",", ""))) if price_match else None,
        airlines=[p.strip() for p in stops.group(1).split(",") if p.strip()] if stops else [],
        stops=(int(stops.group(2)) if stops.group(2) else 0) if stops else None,
        departure_time=dep,
        arrival_time=arr,
        duration_minutes=_minutes(*duration.groups()) if duration else None,
        airport_change=_KO_TRANSFER in text,
        self_transfer=_SELF_TRANSFER_RE.search(text) is not None,
    )


def parse_row_label(label: str, near: date) -> RowLabel | None:
    """Parse a result row's aria-label (``hl=en`` or ``hl=ko``); ``None`` if it is not one.

    ``near`` (the search date) supplies the year the label omits.
    """
    text = _SPACES_RE.sub(" ", label).strip()
    try:
        if "Leaves " in text or text.startswith(("From ", _EN_UNPRICED)):
            return _en_label(text, near)
        return _ko_label(text, near)
    except (KeyError, ValueError) as exc:
        logger.debug("Unparsable Google Flights row label: %s", exc)
        return None


def _row_item(node: LexborNode) -> LexborNode:
    """The ``li`` containing a row's link node (or the node itself)."""
    current: LexborNode | None = node
    while current is not None and current.tag != "li":
        current = current.parent
    return current or node


def _tim_segments(item: LexborNode) -> list[SelectedFlight]:
    """Segments from the row's travelimpactmodel.org link (``ICN-NRT-YP-735-20261120,...``)."""
    node = item.css_first("[data-travelimpactmodelwebsiteurl]")
    url = (node.attributes.get("data-travelimpactmodelwebsiteurl") or "") if node else ""
    match = _TIM_RE.search(url)
    if match is None:
        return []
    flights: list[SelectedFlight] = []
    for part in match.group(1).split(","):
        seg = _TIM_SEGMENT_RE.match(part.strip().upper())
        if seg is None:
            return []
        origin, dest, carrier, number, day = seg.groups()
        flights.append(
            SelectedFlight(origin, f"{day[:4]}-{day[4:6]}-{day[6:]}", dest, carrier, number)
        )
    return flights


def _route_codes(item: LexborNode) -> list[str]:
    codes: list[str] = []
    for node in item.css("div.QylvBf"):
        match = _ROUTE_CODE_RE.match(node.text(strip=True))
        if match:
            codes.append(match.group(1))
    return codes


def _dom_price(item: LexborNode) -> float | None:
    node = item.css_first("div.YMlIz.FpEdX span[role=text][aria-label]")
    match = _PRICE_ARIA_RE.match(node.attributes.get("aria-label") or "") if node else None
    return _money(int(match.group(1).replace(",", ""))) if match else None


def is_result_label(label: str) -> bool:
    """True for a result row's aria-label (priced or "price unavailable", English or Korean)."""
    text = _SPACES_RE.sub(" ", label)
    return any(
        marker in text for marker in (_EN_UNPRICED, _KO_UNPRICED)
    ) or bool(_EN_PRICE_RE.search(text) or _KO_PRICE_RE.search(text))


def _unique_rows(html: str) -> list[tuple[str, LexborNode]]:
    """``(label, li)`` per unique result row (each row is rendered twice: Best/Cheapest panels)."""
    rows: list[tuple[str, LexborNode]] = []
    seen: set[str] = set()
    for node in LexborHTMLParser(html).css(ROW_SELECTOR):
        label = node.attributes.get("aria-label") or ""
        if label in seen or not is_result_label(label):
            continue
        seen.add(label)
        rows.append((label, _row_item(node)))
    return rows


def count_result_rows(html: str) -> int:
    """Number of unique result rows (priced or not) the DOM shows."""
    return len(_unique_rows(html))


def _dom_leg(label: RowLabel, flights: list[SelectedFlight], codes: list[str]) -> FlightLeg | None:
    if flights:
        codes = [flights[0].departure_airport, flights[-1].arrival_airport]
    if len(codes) < 2 or label.departure_time is None or label.arrival_time is None:
        return None
    stops = label.stops if label.stops is not None else max(len(flights) - 1, 0)
    return FlightLeg(
        departure_airport=codes[0],
        arrival_airport=codes[-1],
        departure_time=label.departure_time,
        arrival_time=label.arrival_time,
        duration_minutes=label.duration_minutes,
        stops=stops,
        airlines=label.airlines,
        flight_numbers=[normalize_flight_number(f.carrier_code + f.number) for f in flights],
        segments=[],
        layovers=[f.arrival_airport for f in flights[:-1]],
    )


@dataclass
class DomParseResult:
    """The aria-label fallback's offers plus how each unique result row was classified.

    ``unparsable`` counts rows that may carry a price but whose label / airports / times could
    not be read (a DOM change); ``unpriced`` and ``off_route`` rows are legitimately skipped.
    """

    offers: list[FlightOffer] = field(default_factory=list)
    rows: int = 0
    unpriced: int = 0
    off_route: int = 0
    unparsable: int = 0


def _is_unpriced_label(text: str) -> bool:
    flat = _SPACES_RE.sub(" ", text)
    return _EN_UNPRICED in flat or _KO_UNPRICED in flat


def parse_dom_rows(
    html: str, query: FlightQuery, *, provider_key: str = PROVIDER_KEY
) -> DomParseResult:
    """Offers from the result rows' aria-labels (fallback when ds:1 is unusable), with counts.

    Airport codes and flight numbers come from each row's travelimpactmodel.org link (else the
    route codes shown in the row); legs have no per-segment details (``segments == []``) and
    ``raw["selected"]`` keeps ``[from, date, to, carrier, number]`` per flight for follow-up
    links. Unpriced, off-route and unparsable rows are skipped (and counted); cheapest offer
    per ``offer_key``.
    """
    origins, dests = expected_airports(query.origin), expected_airports(query.destination)
    offers: dict[str, FlightOffer] = {}
    result = DomParseResult()
    for text, item in _unique_rows(html):
        result.rows += 1
        label = parse_row_label(text, query.depart_date)
        price = None
        if label is not None:
            price = label.price if label.price is not None else _dom_price(item)
        if price is None:
            if _is_unpriced_label(text):
                result.unpriced += 1
            else:
                result.unparsable += 1
            continue
        flights = _tim_segments(item)
        leg = _dom_leg(label, flights, _route_codes(item)) if label is not None else None
        if leg is None:
            result.unparsable += 1
            continue
        if not _matches(leg, origins, dests):
            result.off_route += 1
            continue
        raw: dict[str, Any] = {"source": SOURCE_DOM, "search": search_descriptor(query)}
        if flights:
            raw["selected"] = [flight.to_list() for flight in flights]
        if label is not None and (label.airport_change or label.self_transfer):
            raw["self_transfer"] = True
        _keep_cheapest(
            offers, _make_offer(query, leg, price, raw, tuple(flights) or None, provider_key)
        )
    result.offers = list(offers.values())
    if result.unparsable:
        logger.warning(
            "Google Flights: %d of %d result row(s) could not be read from their labels",
            result.unparsable,
            result.rows,
        )
    return result


def parse_dom_offers(
    html: str, query: FlightQuery, *, provider_key: str = PROVIDER_KEY
) -> list[FlightOffer]:
    """Just the offers of :func:`parse_dom_rows`."""
    return parse_dom_rows(html, query, provider_key=provider_key).offers

