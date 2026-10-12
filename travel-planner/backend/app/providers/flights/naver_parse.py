"""Pure parsing of Naver Flights ``searchFlights`` results (DESIGN §13.1).

Input is the **final** SSE event of ``POST flight-api.naver.com/flight/international/searchFlights``
(``status.isCompleted == true``), as captured in-page by ``naver_capture.js``. Shape (verified
2026-10-06, see ``unpublished research notes``)::

    {uniqueId, status: {searchKey, isCompleted, airlinesCodeMap, airportsCodeMap,
                        fareTypesCodeMap, lowestFare: {direct, a01, oneStop?}, expireAt, ...},
     itineraries: [{itineraryId, duration (s), sequence (1 out / 2 back), segments: [...]}],
     fareMappings: [{itineraryIds: "<outId>-<inId>", fares: [...], curation: [...]}],
     isExpired, popularFlights}

Rules applied here:

* One offer per fare mapping, priced with its cheapest ``fareType == "A01"`` fare ("성인/모든
  결제수단": any payment method, no card condition). ``A01/Bxx`` fares need a specific card or
  Naver Pay (often "이용실적 충족시"), ``A07`` is a disability fare: both are never used as the
  price. ``fareMappings`` is sorted by the cheapest fare of *any* type, so ``fareMappings[0]`` is
  not necessarily the cheapest A01 itinerary. Mappings without an A01 fare are skipped.
* ``adult.totalFare`` is KRW **per adult** including tax and fuel surcharge (qCharge), so
  ``price_total = price_per_person * adults``.
* Stops per leg = ``(len(segments) - 1) + sum(len(segment.hiddenStops))``.
* Some partners are outside Naver's Korean OTA list (``bookingPartners``): e.g. BDC219, SKP206,
  ATK201, GTG204 (tax/qCharge reported as 0, non-round converted amounts, baggage UNKNOWN) and
  airline-direct codes such as A7C136. Their offers carry
  ``raw["outside_korean_ota_list"] = True``; the seller is the partner name when known, else
  the code.

Naver data must never be sent to an LLM (flight.naver.com/robots.txt bans AI training and RAG
use); every offer carries ``raw["no_llm"] = True`` so downstream code can tell.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Collection, Mapping, Sequence
from typing import Any

from app.providers.flights.base import (
    FlightLeg,
    FlightOffer,
    FlightQuery,
    FlightSegment,
    ProviderError,
    normalize_flight_number,
)

logger = logging.getLogger(__name__)

PROVIDER_KEY = "naver"
CURRENCY = "KRW"
A01 = "A01"
"""Adult fare payable with any payment method (no card / Naver Pay condition)."""
MAX_OFFERS = 30

KOREAN_OTA_PARTNERS: dict[str, str] = {
    "AGD217": "Agoda",
    "YNJ035": "NOL",
    "INT005": "NOL인터파크투어",
    "HDP010": "PRIVIA 여행",
    "NIT015": "내일투어",
    "YEL009": "노랑풍선",
    "HDT028": "더현대트래블",
    "LTT017": "롯데관광",
    "JTB016": "롯데제이티비",
    "LOT011": "롯데카드여행",
    "MRT023": "마이리얼트립",
    "MOT007": "모두투어",
    "SCR031": "쏘카",
    "KRT006": "여행이지",
    "ONT003": "온라인투어",
    "WHY002": "와이페이모어",
    "WEB001": "웹투어",
    "JAU020": "자유투어",
    "JJD026": "제주도닷컴",
    "VGT027": "참좋은여행",
    "TOT024": "투어2000",
    "TOV025": "투어비스",
    "TBK033": "트래블버킷",
    "TRP016": "트리플",
    "CTR013": "트립닷컴",
    "HAT004": "하나투어",
}
"""Naver's ``bookingPartners`` list (26 Korean OTAs, captured live 2026-10-06). Fallback for
when the live list cannot be loaded; the provider refreshes it (cached for a week)."""

BAGGAGE_TEXT: dict[str, str] = {
    "FREE": "Checked baggage included",
    "PARTIAL": "Checked baggage included on some flights",
    "CHARGE": "Checked baggage costs extra",
}
"""``fares[].baggageFeeType`` -> human text (``UNKNOWN`` -> no baggage text)."""

_DATETIME_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})$")
_DATE_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})$")
_TIME_RE = re.compile(r"^(\d{2})(\d{2})$")


class NaverParseError(ProviderError):
    """The captured event does not have the expected structure."""


# --------------------------------------------------------------------------- small helpers


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _whole(value: float) -> float | int:
    """``284020.0`` -> ``284020`` (keeps ``raw`` tidy); non-integral values unchanged."""
    return int(value) if float(value).is_integer() else value


def local_iso(point: Mapping[str, Any]) -> str | None:
    """Segment endpoint -> airport-local ``"YYYY-MM-DDTHH:MM"`` (never converted).

    Uses ``dateTime`` (``"YYYYMMDDHHMM"``), else ``date`` + ``time``; ``None`` if neither parses.
    """
    match = _DATETIME_RE.match(str(point.get("dateTime") or ""))
    if match:
        year, month, day, hour, minute = match.groups()
        return f"{year}-{month}-{day}T{hour}:{minute}"
    day_match = _DATE_RE.match(str(point.get("date") or ""))
    time_match = _TIME_RE.match(str(point.get("time") or ""))
    if day_match and time_match:
        year, month, day = day_match.groups()
        hour, minute = time_match.groups()
        return f"{year}-{month}-{day}T{hour}:{minute}"
    return None


def hidden_stop_code(stop: Any) -> str | None:
    """Airport code of a ``hiddenStops`` entry (format unverified: str or dict), else ``None``."""
    if isinstance(stop, str):
        return stop.strip().upper() or None
    if isinstance(stop, Mapping):
        for key in ("airportCode", "airport", "code"):
            value = stop.get(key)
            if isinstance(value, Mapping):
                value = value.get("airportCode")
            if isinstance(value, str) and value.strip():
                return value.strip().upper()
    return None


def _hidden_stops(segment: Mapping[str, Any]) -> list[Any]:
    stops = segment.get("hiddenStops")
    return list(stops) if isinstance(stops, list) else []


def _carrier(segment: Mapping[str, Any], key: str) -> tuple[str | None, str | None]:
    """``(airlineCode, flightNumber)`` of ``marketingCarrier``/``operatingCarrier``."""
    carrier = segment.get(key)
    if not isinstance(carrier, Mapping):
        return None, None
    code = str(carrier.get("airlineCode") or "").strip().upper() or None
    number = str(carrier.get("flightNumber") or "").strip() or None
    return code, number


def _minutes(seconds: Any) -> int | None:
    value = _number(seconds)
    return int(value // 60) if value is not None and value >= 0 else None


# --------------------------------------------------------------------------- legs


def parse_segment(segment: Mapping[str, Any], airlines: Mapping[str, str]) -> FlightSegment | None:
    """One ``segments[]`` entry -> :class:`FlightSegment` (``None`` without airports/times)."""
    departure, arrival = segment.get("departure"), segment.get("arrival")
    if not isinstance(departure, Mapping) or not isinstance(arrival, Mapping):
        return None
    dep_code = str(departure.get("airportCode") or "").strip().upper()
    arr_code = str(arrival.get("airportCode") or "").strip().upper()
    dep_time, arr_time = local_iso(departure), local_iso(arrival)
    if not (dep_code and arr_code and dep_time and arr_time):
        return None
    code, number = _carrier(segment, "marketingCarrier")
    op_code, _op_number = _carrier(segment, "operatingCarrier")
    return FlightSegment(
        carrier_code=code,
        carrier_name=airlines.get(code, code) if code else None,
        flight_number=normalize_flight_number(f"{code}{number}") if code and number else None,
        departure_airport=dep_code,
        arrival_airport=arr_code,
        departure_time=dep_time,
        arrival_time=arr_time,
        duration_minutes=_minutes(segment.get("flightDuration")),
        operating_carrier=airlines.get(op_code, op_code) if op_code and op_code != code else None,
        aircraft=str(segment.get("aircraftCode") or "").strip() or None,
    )


def leg_stops(segments: Sequence[Mapping[str, Any]]) -> int:
    """Connections plus hidden (technical) stops: ``(n - 1) + sum(len(hiddenStops))``."""
    if not segments:
        return 0
    return (len(segments) - 1) + sum(len(_hidden_stops(s)) for s in segments)


def _layovers(
    raw_segments: Sequence[Mapping[str, Any]], segments: list[FlightSegment]
) -> list[str]:
    """Stop airports in flight order: each segment's hidden stops, then its arrival connection."""
    layovers: list[str] = []
    for index, (raw, parsed) in enumerate(zip(raw_segments, segments, strict=True)):
        layovers.extend(code for code in map(hidden_stop_code, _hidden_stops(raw)) if code)
        if index < len(segments) - 1:
            layovers.append(parsed.arrival_airport)
    return layovers


def _leg_duration(
    itinerary: Mapping[str, Any], raw_segments: Sequence[Mapping[str, Any]]
) -> int | None:
    """``itinerary.duration`` (seconds, incl. ground time) in minutes, else the sum of parts."""
    total = _minutes(itinerary.get("duration"))
    if total is not None:
        return total
    parts = [_number(s.get("flightDuration")) for s in raw_segments]
    parts += [_number(s.get("groundDuration")) or 0.0 for s in raw_segments]
    if any(part is None for part in parts):
        return None
    return int(sum(part for part in parts if part is not None) // 60)


def parse_leg(itinerary: Mapping[str, Any], airlines: Mapping[str, str]) -> FlightLeg | None:
    """One ``itineraries[]`` entry (one direction) -> :class:`FlightLeg` (``None`` if unusable)."""
    raw_segments = [s for s in itinerary.get("segments") or [] if isinstance(s, Mapping)]
    parsed = [parse_segment(s, airlines) for s in raw_segments]
    if not parsed or any(s is None for s in parsed):
        return None
    segments: list[FlightSegment] = [s for s in parsed if s is not None]
    return FlightLeg(
        departure_airport=segments[0].departure_airport,
        arrival_airport=segments[-1].arrival_airport,
        departure_time=segments[0].departure_time,
        arrival_time=segments[-1].arrival_time,
        duration_minutes=_leg_duration(itinerary, raw_segments),
        stops=leg_stops(raw_segments),
        airlines=list(dict.fromkeys(s.carrier_name for s in segments if s.carrier_name)),
        flight_numbers=[s.flight_number for s in segments if s.flight_number],
        segments=segments,
        layovers=_layovers(raw_segments, segments),
    )


# --------------------------------------------------------------------------- fares


def _fare_price(fare: Mapping[str, Any]) -> float | None:
    adult = fare.get("adult")
    price = _number(adult.get("totalFare")) if isinstance(adult, Mapping) else None
    return price if price is not None and price > 0 else None


def _fares(mapping: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [
        fare
        for fare in mapping.get("fares") or []
        if isinstance(fare, Mapping) and _fare_price(fare) is not None
    ]


def cheapest_a01_fare(
    mapping: Mapping[str, Any], partners: Collection[str] | None = None
) -> Mapping[str, Any] | None:
    """Cheapest confirmed fare with ``fareType == "A01"`` exactly (``None`` if there is none).

    With ``partners``, only fares sold by those partner codes are considered.
    """
    candidates = [
        fare
        for fare in _fares(mapping)
        if fare.get("fareType") == A01
        and fare.get("isConfirmed", True) is not False
        and (partners is None or fare.get("partnerCode") in partners)
    ]
    return min(candidates, key=lambda fare: _fare_price(fare) or 0.0, default=None)


def cheapest_fare(mapping: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """Cheapest fare of any type (card / Naver Pay conditional ones included)."""
    return min(_fares(mapping), key=lambda fare: _fare_price(fare) or 0.0, default=None)


def _fare_type_label(fare_types: Mapping[str, Any], fare_type: str) -> str | None:
    entry = fare_types.get(fare_type)
    name = entry.get("name") if isinstance(entry, Mapping) else None
    return str(name) if name else None


def _conditional_fare(
    mapping: Mapping[str, Any], a01_price: float, fare_types: Mapping[str, Any]
) -> dict[str, Any] | None:
    """The mapping's cheapest non-A01 fare when it undercuts A01 (informational, conditional)."""
    fare = cheapest_fare(mapping)
    price = _fare_price(fare) if fare is not None else None
    if fare is None or price is None or price >= a01_price or fare.get("fareType") == A01:
        return None
    fare_type = str(fare.get("fareType") or "")
    return {
        "price_per_person": _whole(price),
        "fare_type": fare_type,
        "label": _fare_type_label(fare_types, fare_type),
        "partner_code": fare.get("partnerCode"),
    }


# --------------------------------------------------------------------------- event


def _require_list(event: Mapping[str, Any], key: str) -> list[Any]:
    value = event.get(key)
    if not isinstance(value, list):
        raise NaverParseError(f"Naver search result has no '{key}' list")
    return value


def _status(event: Mapping[str, Any]) -> Mapping[str, Any]:
    status = event.get("status")
    if not isinstance(status, Mapping):
        raise NaverParseError("Naver search result has no 'status' object")
    return status


def _code_map(status: Mapping[str, Any], key: str) -> dict[str, Any]:
    value = status.get(key)
    return dict(value) if isinstance(value, Mapping) else {}


def _itinerary_index(event: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {
        str(item["itineraryId"]): item
        for item in _require_list(event, "itineraries")
        if isinstance(item, Mapping) and item.get("itineraryId")
    }


def _mapping_legs(
    mapping: Mapping[str, Any], itineraries: Mapping[str, Mapping[str, Any]]
) -> list[Mapping[str, Any]] | None:
    """The mapping's itineraries ordered by ``sequence`` (``None`` if one is missing).

    Leg ids never contain ``-``; connecting segments inside one leg are joined with ``+``.
    """
    ids = [part for part in str(mapping.get("itineraryIds") or "").split("-") if part]
    legs = [itineraries.get(leg_id) for leg_id in ids]
    if not legs or any(leg is None for leg in legs):
        return None
    found = [leg for leg in legs if leg is not None]
    return sorted(found, key=lambda leg: _number(leg.get("sequence")) or 0.0)


def _stops_allowed(offer: FlightOffer, query: FlightQuery) -> bool:
    legs = [offer.outbound] + ([offer.inbound] if offer.inbound else [])
    worst = max(leg.stops for leg in legs)
    if query.direct_only:
        return worst == 0
    return query.max_stops is None or query.max_stops < 0 or worst <= query.max_stops


def _offer_raw(
    mapping: Mapping[str, Any],
    fare: Mapping[str, Any],
    price: float,
    status: Mapping[str, Any],
    partner_name: str | None,
    korean_partners: Collection[str],
) -> dict[str, Any]:
    adult = fare.get("adult") if isinstance(fare.get("adult"), Mapping) else {}
    code = str(fare.get("partnerCode") or "")
    fare_types = _code_map(status, "fareTypesCodeMap")
    raw: dict[str, Any] = {
        "partner_code": code or None,
        "partner_name": partner_name,
        "outside_korean_ota_list": code not in korean_partners,
        "fare_type_label": _fare_type_label(fare_types, A01),
        "baggage_fee_type": fare.get("baggageFeeType"),
        "tax": adult.get("tax"),
        "q_charge": adult.get("qCharge"),
        "itinerary_ids": mapping.get("itineraryIds"),
        "curation": list(mapping.get("curation") or []),
        "search_key": status.get("searchKey"),
        "no_llm": True,  # robots.txt of flight.naver.com bans AI/RAG use of this data
    }
    conditional = _conditional_fare(mapping, price, fare_types)
    if conditional is not None:
        raw["conditional_min_fare"] = conditional
    return raw


def offer_from_mapping(
    mapping: Mapping[str, Any],
    itineraries: Mapping[str, Mapping[str, Any]],
    status: Mapping[str, Any],
    query: FlightQuery,
    *,
    search_url: str | None,
    partner_names: Mapping[str, str],
    korean_partners: Collection[str],
    korean_ota_only: bool = False,
) -> FlightOffer | None:
    """One fare mapping -> :class:`FlightOffer` priced with its cheapest A01 fare.

    ``korean_ota_only`` limits the choice to partners in ``korean_partners``. ``None`` when
    the mapping has no (such) A01 fare or its itineraries are missing/incomplete.
    """
    fare = cheapest_a01_fare(mapping, korean_partners if korean_ota_only else None)
    price = _fare_price(fare) if fare is not None else None
    raw_legs = _mapping_legs(mapping, itineraries)
    if fare is None or price is None or raw_legs is None:
        return None
    airlines = {str(k): str(v) for k, v in _code_map(status, "airlinesCodeMap").items()}
    legs = [parse_leg(leg, airlines) for leg in raw_legs]
    if any(leg is None for leg in legs):
        return None
    parsed: list[FlightLeg] = [leg for leg in legs if leg is not None]
    adults = max(query.adults, 1)
    code = str(fare.get("partnerCode") or "")
    name = partner_names.get(code)
    return FlightOffer(
        provider=PROVIDER_KEY,
        outbound=parsed[0],
        inbound=parsed[1] if len(parsed) > 1 else None,
        price_total=price * adults,
        price_per_person=price,
        currency=CURRENCY,
        cabin=query.cabin,
        adults=adults,
        fare_type=A01,
        baggage=BAGGAGE_TEXT.get(str(fare.get("baggageFeeType") or "")),
        seller=name or code or None,
        booking_url=search_url,
        source_url=search_url,
        provider_offer_id=str(mapping.get("itineraryIds") or "") or None,
        raw=_offer_raw(mapping, fare, price, status, name, korean_partners),
    )


def min_a01_price(event: Mapping[str, Any]) -> float | None:
    """Cheapest A01 fare over every fare mapping of the event (``None`` without any)."""
    prices = [
        _fare_price(fare)
        for mapping in _require_list(event, "fareMappings")
        if isinstance(mapping, Mapping)
        for fare in [cheapest_a01_fare(mapping)]
        if fare is not None
    ]
    return min((p for p in prices if p is not None), default=None)


def check_lowest_fare(event: Mapping[str, Any]) -> bool:
    """Cross-check the cheapest parsed A01 fare against ``status.lowestFare.a01``.

    Logs a warning and returns ``False`` on a mismatch (e.g. the cheapest A01 itinerary fell
    outside the page's ``limit``); ``True`` when they agree or Naver reports no ``a01`` value.
    """
    lowest = _status(event).get("lowestFare")
    expected = _number(lowest.get("a01")) if isinstance(lowest, Mapping) else None
    found = min_a01_price(event)
    if expected is None or (found is not None and abs(found - expected) < 0.5):
        return True
    logger.warning(
        "Naver: cheapest parsed A01 fare %s differs from status.lowestFare.a01 %s",
        f"{found:,.0f}" if found is not None else "none",
        f"{expected:,.0f}",
    )
    return False


def parse_final_event(
    event: Mapping[str, Any],
    query: FlightQuery,
    *,
    search_url: str | None = None,
    partner_names: Mapping[str, str] | None = None,
    korean_partners: Collection[str] | None = None,
    korean_ota_only: bool = False,
    max_offers: int = MAX_OFFERS,
) -> list[FlightOffer]:
    """The final ``searchFlights`` event -> at most ``max_offers`` cheapest A01 offers.

    ``partner_names`` maps partner codes to names (default :data:`KOREAN_OTA_PARTNERS`);
    ``korean_partners`` is the set of codes in Naver's Korean OTA list (default: the keys of
    ``partner_names``); ``korean_ota_only`` prices every itinerary with its cheapest A01 fare
    from those partners only. Offers whose stops exceed ``query.direct_only``/``max_stops`` are
    dropped; one offer per ``offer_key`` (cheapest kept). Raises :class:`NaverParseError` when
    the event lacks ``status``/``itineraries``/``fareMappings`` or is marked ``isExpired``
    (stale prices: Naver's own client discards such a snapshot and searches again).
    """
    status = _status(event)
    mappings = _require_list(event, "fareMappings")
    if event.get("isExpired") is True:
        raise NaverParseError("Naver search result has expired (isExpired); prices are stale")
    if status.get("isCompleted") is not True:
        logger.warning("Naver: parsing a search snapshot that is not complete")
    names = dict(KOREAN_OTA_PARTNERS if partner_names is None else partner_names)
    korean = set(names if korean_partners is None else korean_partners)
    itineraries = _itinerary_index(event)

    offers: dict[str, FlightOffer] = {}
    skipped = 0
    for mapping in mappings:
        offer = (
            offer_from_mapping(
                mapping,
                itineraries,
                status,
                query,
                search_url=search_url,
                partner_names=names,
                korean_partners=korean,
                korean_ota_only=korean_ota_only,
            )
            if isinstance(mapping, Mapping)
            else None
        )
        if offer is None or not _stops_allowed(offer, query):
            skipped += 1
            continue
        key = offer.offer_key()
        if key not in offers or offer.price_per_person < offers[key].price_per_person:
            offers[key] = offer
    if skipped:
        logger.debug(
            "Naver: skipped %d fare mappings (no A01 fare, incomplete or too many stops)", skipped
        )
    check_lowest_fare(event)
    ranked = sorted(offers.values(), key=lambda offer: offer.price_per_person)
    return ranked[: max(0, max_offers)]


def partner_names_from_graphql(payload: Any) -> dict[str, str]:
    """``bookingPartners`` GraphQL items ``[{otaId, expName}]`` -> ``{otaId: expName}``."""
    names: dict[str, str] = {}
    for item in payload if isinstance(payload, list) else []:
        if not isinstance(item, Mapping):
            continue
        code, name = str(item.get("otaId") or "").strip(), str(item.get("expName") or "").strip()
        if code and name:
            names[code] = name
    return names
