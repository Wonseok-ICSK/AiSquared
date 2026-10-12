"""The UI's booking step (``GET /api/offers/{id}/booking-plan``).

Google Flights is where an offer is booked: its "Booking options" page lists the airline and
travel agencies for both flights, and each "Continue" hands over to that seller's site. Most
low round-trip prices from Korea mix airlines (e.g. Jin Air out, Air Seoul back), which are two
separate tickets booked one after the other. :func:`build_plan` turns a stored offer into
that picture: the tickets to buy and, per airline, how Google's hand-over opens its site.

:data:`AIRLINE_SITES` records hand-overs checked by following Google's "Continue" in a real
browser (2026-10-12, ICN-NRT, 2 adults). ``prefilled`` airlines open their flight list or fare
page with route, date and passengers; ``homepage`` ones open their home page, where the
traveller searches again. Airlines not listed are ``unknown``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date
from typing import Any, Literal, NamedTuple
from urllib.parse import urlsplit

from app.models import FlightOffer as OfferRow
from app.schemas import BookingPage, BookingPlanOut, BookingTicketOut, FlightOfferOut

GOOGLE_BOOKING_PATH = "/travel/flights/booking"
GOOGLE_SEARCH_PATH = "/travel/flights/search"
RESOLVED_TOTAL_KEY = "inbound_round_trip_total"


class AirlineSite(NamedTuple):
    url: str
    handoff: Literal["prefilled", "homepage", "unknown"]


AIRLINE_SITES: Mapping[str, AirlineSite] = {
    "7C": AirlineSite("https://www.jejuair.net/ko/", "prefilled"),
    "BX": AirlineSite("https://www.airbusan.com/", "prefilled"),
    "KE": AirlineSite("https://www.koreanair.com/", "prefilled"),
    "LJ": AirlineSite("https://www.jinair.com/", "prefilled"),
    "MM": AirlineSite("https://www.flypeach.com/kr/", "prefilled"),
    "TW": AirlineSite("https://www.trinityairways.com/", "prefilled"),
    "YP": AirlineSite("https://www.airpremia.com/", "prefilled"),
    "ZG": AirlineSite("https://www.zipair.net/ko/", "prefilled"),
    "RS": AirlineSite("https://flyairseoul.com/", "homepage"),
}


def booking_page_kind(url: str | None) -> BookingPage:
    """Google "Booking options", a Google Flights search, another site, or no link."""
    if not url:
        return "none"
    parts = urlsplit(url)
    if ".google." in f".{parts.netloc.lower()}":
        path = parts.path.rstrip("/")
        if path == GOOGLE_BOOKING_PATH:
            return "google_booking"
        if path == GOOGLE_SEARCH_PATH:
            return "google_search"
    return "other"


def leg_carriers(leg: Mapping[str, Any] | None) -> list[str]:
    """Marketing carrier codes of a stored leg, in flight order (from segments, else the
    flight numbers' two-character prefixes)."""
    if not leg:
        return []
    codes = [
        str(seg.get("carrier_code")).upper()
        for seg in leg.get("segments") or []
        if isinstance(seg, Mapping) and seg.get("carrier_code")
    ]
    if not codes:
        codes = [str(n)[:2].upper() for n in leg.get("flight_numbers") or [] if n]
    return list(dict.fromkeys(codes))


def separate_tickets(outbound: Mapping[str, Any], inbound: Mapping[str, Any] | None) -> bool | None:
    """True when no airline flies both directions (normally two bookings), ``None`` while
    the return flight is unknown."""
    if inbound is None:
        return None
    out, back = set(leg_carriers(outbound)), set(leg_carriers(inbound))
    if not out or not back:
        return None
    return out.isdisjoint(back)


def _airline_name(leg: Mapping[str, Any]) -> str | None:
    for seg in leg.get("segments") or []:
        if isinstance(seg, Mapping) and seg.get("carrier_name"):
            return str(seg["carrier_name"])
    airlines = leg.get("airlines") or []
    return str(airlines[0]) if airlines else None


def _site(codes: Iterable[str]) -> AirlineSite:
    """The site of the first carrier whose hand-over is known; ``unknown`` otherwise."""
    for code in codes:
        if code in AIRLINE_SITES:
            return AIRLINE_SITES[code]
    return AirlineSite("", "unknown")


def _ticket(
    legs: list[Literal["outbound", "inbound"]],
    leg: Mapping[str, Any],
    depart: date,
    return_date: date | None = None,
) -> BookingTicketOut:
    codes = leg_carriers(leg)
    site = _site(codes)
    return BookingTicketOut(
        legs=legs,
        carrier_code=codes[0] if codes else None,
        airline=_airline_name(leg),
        flight_numbers=[str(n) for n in leg.get("flight_numbers") or []],
        origin=str(leg.get("departure_airport") or ""),
        destination=str(leg.get("arrival_airport") or ""),
        depart_date=depart,
        return_date=return_date,
        departure_time=str(leg.get("departure_time") or ""),
        arrival_time=str(leg.get("arrival_time") or ""),
        handoff=site.handoff,
        airline_url=site.url or None,
    )


def _leg_date(leg: Mapping[str, Any], fallback: date) -> date:
    try:
        return date.fromisoformat(str(leg.get("departure_time") or "")[:10])
    except ValueError:
        return fallback


def build_plan(row: OfferRow, booking_url: str | None, adults: int) -> BookingPlanOut:
    """The booking picture of a stored offer whose return flight was looked up (if it could
    be): one ticket per direction when the airlines differ, else one ticket for the trip."""
    outbound: Mapping[str, Any] = row.outbound or {}
    inbound: Mapping[str, Any] | None = row.inbound or None
    round_trip = row.return_date is not None
    separate = separate_tickets(outbound, inbound) if round_trip else None
    depart = _leg_date(outbound, row.depart_date)

    tickets: list[BookingTicketOut]
    if inbound is not None:
        back = _leg_date(inbound, row.return_date or depart)
        if separate:
            tickets = [_ticket(["outbound"], outbound, depart), _ticket(["inbound"], inbound, back)]
        else:
            tickets = [_ticket(["outbound", "inbound"], outbound, depart, back)]
    else:
        tickets = [_ticket(["outbound"], outbound, depart)]

    resolved_total = (row.raw or {}).get(RESOLVED_TOTAL_KEY)
    return BookingPlanOut(
        offer=FlightOfferOut.model_validate(row),
        booking_url=booking_url,
        booking_page=booking_page_kind(booking_url),
        adults=adults,
        round_trip=round_trip,
        return_known=inbound is not None,
        separate_tickets=separate,
        tickets=tickets,
        resolved_total=float(resolved_total) if isinstance(resolved_total, (int, float)) else None,
    )
