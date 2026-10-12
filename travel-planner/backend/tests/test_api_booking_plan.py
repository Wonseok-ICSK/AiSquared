"""``GET /api/offers/{id}/booking-plan``: the UI's booking step (``app.services.booking``)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models import FlightOffer as OfferRow
from app.providers.flights.base import FlightOffer
from app.services import booking
from monitoring_fakes import register  # noqa: F401  (fixture)
from test_api_book import (  # noqa: F401  (fixture)
    BOOKING_LINK,
    INBOUND,
    RETURN,
    SEARCH_LINK,
    Details,
    add_offer,
    details_provider,
    leg,
    load,
    with_booking_page,
)
from test_api_support import api, quiet, sched  # noqa: F401  (fixtures)

AIR_SEOUL_BACK = replace(
    leg("NRT", "ICN", RETURN, "RS704", "12:50", "15:35"),
    airlines=["Air Seoul"],
    segments=[
        replace(seg, carrier_code="RS", carrier_name="Air Seoul")
        for seg in leg("NRT", "ICN", RETURN, "RS704", "12:50", "15:35").segments
    ],
)


def plan(api: TestClient, offer_id: int) -> dict[str, Any]:
    response = api.get(f"/api/offers/{offer_id}/booking-plan")
    assert response.status_code == 200, response.text
    return response.json()


def with_air_seoul_return(offer: FlightOffer) -> FlightOffer:
    raw = {**offer.raw, "inbound_round_trip_total": 401_000.0}
    return replace(offer, inbound=AIR_SEOUL_BACK, booking_url=BOOKING_LINK, raw=raw)


def test_plan_looks_up_return_flight_one_airline_one_ticket(
    api: TestClient, db: Session, details_provider: Callable[..., Details]  # noqa: F811
) -> None:
    details = details_provider(with_booking_page)
    offer_id = add_offer(db, adults=2)

    body = plan(api, offer_id)

    assert len(details.calls) == 1
    assert body["booking_url"] == BOOKING_LINK and body["booking_page"] == "google_booking"
    assert body["adults"] == 2 and body["round_trip"] is True and body["return_known"] is True
    assert body["separate_tickets"] is False and body["resolved_total"] is None
    assert body["offer"]["id"] == offer_id
    assert body["offer"]["inbound"]["flight_numbers"] == ["BX266"]
    (ticket,) = body["tickets"]
    assert ticket["legs"] == ["outbound", "inbound"]
    assert ticket["carrier_code"] == "BX" and ticket["airline"] == "Air Busan"
    assert ticket["origin"] == "ICN" and ticket["destination"] == "NRT"
    assert ticket["depart_date"] == "2026-11-20" and ticket["return_date"] == RETURN.isoformat()
    assert ticket["handoff"] == "prefilled" and ticket["airline_url"] == "https://www.airbusan.com/"

    # stored like a "Book" click: the next plan (or click) needs no provider request
    assert load(offer_id).raw["booking_resolved"] is True
    plan(api, offer_id)
    assert len(details.calls) == 1


def test_plan_two_airlines_two_tickets(
    api: TestClient, db: Session, details_provider: Callable[..., Details]  # noqa: F811
) -> None:
    details_provider(with_air_seoul_return)
    offer_id = add_offer(db, adults=2)

    body = plan(api, offer_id)

    assert body["separate_tickets"] is True and body["resolved_total"] == 401_000.0
    out, back = body["tickets"]
    assert out["legs"] == ["outbound"] and out["flight_numbers"] == ["BX265"]
    assert out["return_date"] is None
    assert back["legs"] == ["inbound"] and back["carrier_code"] == "RS"
    assert back["airline"] == "Air Seoul" and back["flight_numbers"] == ["RS704"]
    assert back["origin"] == "NRT" and back["destination"] == "ICN"
    assert back["depart_date"] == RETURN.isoformat()
    assert back["departure_time"] == f"{RETURN.isoformat()}T12:50"
    assert back["handoff"] == "homepage" and back["airline_url"] == "https://flyairseoul.com/"


def test_plan_return_lookup_failed(
    api: TestClient, db: Session, details_provider: Callable[..., Details]  # noqa: F811
) -> None:
    details_provider(lambda offer: offer)  # no return flight found
    offer_id = add_offer(db)

    body = plan(api, offer_id)

    assert body["booking_url"] == SEARCH_LINK and body["booking_page"] == "google_search"
    assert body["return_known"] is False and body["separate_tickets"] is None
    (ticket,) = body["tickets"]
    assert ticket["legs"] == ["outbound"]


def test_plan_one_way_needs_no_lookup(
    api: TestClient, db: Session, details_provider: Callable[..., Details]  # noqa: F811
) -> None:
    details = details_provider(with_booking_page)
    offer_id = add_offer(db, round_trip=False, adults=1)

    body = plan(api, offer_id)

    assert details.calls == []
    assert body["round_trip"] is False and body["separate_tickets"] is None
    assert body["adults"] == 1 and body["booking_url"] == SEARCH_LINK
    (ticket,) = body["tickets"]
    assert ticket["legs"] == ["outbound"] and ticket["return_date"] is None


def test_plan_unlisted_airline_handoff_unknown(
    api: TestClient, db: Session, details_provider: Callable[..., Details]  # noqa: F811
) -> None:
    details_provider(with_booking_page)
    offer_id = add_offer(db)
    with SessionLocal() as session:  # rename the stored flights to an airline not in the table
        row = session.get(OfferRow, offer_id)
        out = dict(row.outbound)
        out["flight_numbers"] = ["XQ701"]
        out["segments"] = [
            {**out["segments"][0], "carrier_code": "XQ", "carrier_name": "SunExpress",
             "flight_number": "XQ701"}
        ]
        row.outbound = out
        session.commit()
    ticket = plan(api, offer_id)["tickets"][0]  # XQ out + BX back: two tickets
    assert ticket["carrier_code"] == "XQ" and ticket["airline"] == "SunExpress"
    assert ticket["handoff"] == "unknown" and ticket["airline_url"] is None


def test_plan_without_any_link(api: TestClient, db: Session) -> None:
    offer_id = add_offer(db, round_trip=False, booking_url=None, source_url=None)
    body = plan(api, offer_id)
    assert body["booking_url"] is None and body["booking_page"] == "none"


def test_plan_known_return_without_request(
    api: TestClient, db: Session, details_provider: Callable[..., Details]  # noqa: F811
) -> None:
    details = details_provider(with_booking_page)
    offer_id = add_offer(db, provider="fakegf", inbound=INBOUND)
    body = plan(api, offer_id)
    assert details.calls == [] and body["return_known"] is True
    assert body["separate_tickets"] is False


def test_plan_unknown_offer(api: TestClient) -> None:
    assert api.get("/api/offers/999999/booking-plan").status_code == 404


# --------------------------------------------------------------------------- helpers


@pytest.mark.parametrize(
    ("url", "kind"),
    [
        (BOOKING_LINK, "google_booking"),
        ("https://www.google.co.kr/travel/flights/booking/?tfs=x", "google_booking"),
        (SEARCH_LINK, "google_search"),
        ("https://www.google.com/travel/flights", "other"),
        ("https://www.jinair.com/booking", "other"),
        (None, "none"),
        ("", "none"),
    ],
)
def test_booking_page_kind(url: str | None, kind: str) -> None:
    assert booking.booking_page_kind(url) == kind


def test_leg_carriers_from_segments_or_flight_numbers() -> None:
    segs = [{"carrier_code": "ke"}, {"carrier_code": "KE"}, {"carrier_code": "DL"}, {}]
    assert booking.leg_carriers({"segments": segs, "flight_numbers": ["XX1"]}) == ["KE", "DL"]
    assert booking.leg_carriers({"flight_numbers": ["7C1107", "LJ263"]}) == ["7C", "LJ"]
    assert booking.leg_carriers(None) == []


@pytest.mark.parametrize(
    ("out", "back", "expected"),
    [
        (["LJ263"], ["RS704"], True),
        (["KE705"], ["KE706"], False),
        (["KE705", "DL27"], ["DL28"], False),
        (["KE705"], None, None),
        (["KE705"], [], None),
    ],
)
def test_separate_tickets(out: list[str], back: list[str] | None, expected: bool | None) -> None:
    inbound = None if back is None else {"flight_numbers": back}
    assert booking.separate_tickets({"flight_numbers": out}, inbound) is expected
