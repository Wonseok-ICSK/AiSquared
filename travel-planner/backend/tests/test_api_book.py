"""``GET /api/offers/{id}/book``: redirect to the most specific booking page, resolving an
unknown return flight through the offer's provider on the first click."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import date
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api import offers as offers_api
from app.db import SessionLocal
from app.models import FlightOffer as OfferRow
from app.models import SearchRun, Trip
from app.providers.flights.base import FlightLeg, FlightOffer, FlightQuery, FlightSegment
from app.providers.flights.google_flights_tfs import (
    booking_link,
    decode_message,
    decode_tfs,
    selected_from_leg,
)
from monitoring_fakes import fake_provider_class, register  # noqa: F401  (fixture)
from test_api_support import api, quiet, sched  # noqa: F401  (fixtures)

DEPART = date(2026, 11, 20)
RETURN = date(2026, 11, 27)
SEARCH_LINK = "https://www.google.com/travel/flights/search?tfs=OUTBOUND-ONLY&hl=ko&gl=KR&curr=KRW"
BOOKING_LINK = "https://www.google.com/travel/flights/booking?tfs=BOTH-LEGS&hl=ko&gl=KR&curr=KRW"
RESOLVED_LINK = "https://www.google.com/travel/flights/search?tfs=RETURN-LIST&hl=ko"


def leg(
    dep: str, arr: str, day: date, number: str, start: str = "09:05", end: str = "11:25"
) -> FlightLeg:
    """A direct leg with one segment carrying a flight number (selectable for ``tfs``)."""
    carrier = number[:2]
    departure, arrival = f"{day.isoformat()}T{start}", f"{day.isoformat()}T{end}"
    return FlightLeg(
        departure_airport=dep,
        arrival_airport=arr,
        departure_time=departure,
        arrival_time=arrival,
        duration_minutes=140,
        stops=0,
        airlines=["Air Busan"],
        flight_numbers=[number],
        segments=[
            FlightSegment(
                carrier_code=carrier,
                carrier_name="Air Busan",
                flight_number=number,
                departure_airport=dep,
                arrival_airport=arr,
                departure_time=departure,
                arrival_time=arrival,
                duration_minutes=140,
            )
        ],
    )


OUTBOUND = leg("ICN", "NRT", DEPART, "BX265")
INBOUND = leg("NRT", "ICN", RETURN, "BX266", "12:40", "15:20")


def add_offer(
    db: Session,
    *,
    provider: str = "fakegf",
    adults: int = 2,
    round_trip: bool = True,
    inbound: FlightLeg | None = None,
    booking_url: str | None = SEARCH_LINK,
    source_url: str | None = SEARCH_LINK,
    raw: dict[str, Any] | None = None,
) -> int:
    """A trip + finished run + one stored offer; returns the offer id."""
    trip = Trip(
        name="Tokyo",
        origin="ICN",
        destination="NRT",
        trip_type="round_trip" if round_trip else "one_way",
        depart_date=DEPART,
        return_date=RETURN if round_trip else None,
        adults=adults,
        providers=[provider],
        schedule_times=["08:30"],
    )
    db.add(trip)
    db.flush()
    run = SearchRun(trip_id=trip.id, trigger="manual", status="ok", currency="KRW")
    db.add(run)
    db.flush()
    per_person = 189_900.0
    row = OfferRow(
        trip_id=trip.id,
        run_id=run.id,
        provider=provider,
        offer_key=f"ICN-NRT-{DEPART}T09:05-BX265|RT:{RETURN}|ECONOMY",
        origin="ICN",
        destination="NRT",
        depart_date=DEPART,
        return_date=RETURN if round_trip else None,
        airline="Air Busan",
        flight_number="BX265",
        departure_airport="ICN",
        arrival_airport="NRT",
        departure_time=OUTBOUND.departure_time,
        arrival_time=OUTBOUND.arrival_time,
        duration_minutes=140,
        stops=0,
        outbound=OUTBOUND.to_dict(),
        inbound=inbound.to_dict() if inbound is not None else None,
        cabin="economy",
        price_total=per_person * adults,
        price_per_person=per_person,
        currency="KRW",
        booking_url=booking_url,
        source_url=source_url,
        raw=raw if raw is not None else {"search": {"origin": "ICN", "destination": "NRT"}},
    )
    db.add(row)
    db.commit()
    return row.id


def load(offer_id: int) -> OfferRow:
    with SessionLocal() as session:
        row = session.get(OfferRow, offer_id)
        assert row is not None
        session.expunge(row)
        return row


def book(api: TestClient, offer_id: int) -> Any:
    return api.get(f"/api/offers/{offer_id}/book", follow_redirects=False)


class Details:
    """``get_flight_details`` stand-in recording the offers it receives."""

    def __init__(self, result: Callable[[FlightOffer], FlightOffer]) -> None:
        self.result = result
        self.calls: list[FlightOffer] = []

    def __call__(self, offer: FlightOffer) -> FlightOffer:
        self.calls.append(offer)
        return self.result(offer)


def with_booking_page(offer: FlightOffer) -> FlightOffer:
    raw = {**offer.raw, "inbound_source": "outbound_selected_search"}
    return FlightOffer(**{**offer.__dict__, "inbound": INBOUND, "booking_url": BOOKING_LINK,
                          "raw": raw})


@pytest.fixture
def details_provider(register: Callable[[type], None]) -> Callable[..., Details]:  # noqa: F811
    """``details_provider(fn, key="fakegf", configured=True)`` registers a fake provider."""

    def make(
        result: Callable[[FlightOffer], FlightOffer], key: str = "fakegf", configured: bool = True
    ) -> Details:
        details = Details(result)
        register(fake_provider_class(key, lambda q: [], details=details, configured=configured))
        return details

    return make


# --------------------------------------------------------------------------- resolution


def test_book_resolves_return_flight_and_stores_booking_page(
    api: TestClient, db: Session, details_provider: Callable[..., Details]  # noqa: F811
) -> None:
    details = details_provider(with_booking_page)
    offer_id = add_offer(db, adults=2)

    response = book(api, offer_id)
    assert response.status_code == 302
    assert response.headers["location"] == BOOKING_LINK

    # the provider got the rebuilt round-trip offer with dates and passengers
    (sent,) = details.calls
    assert sent.provider == "fakegf" and sent.adults == 2 and sent.cabin == "economy"
    assert sent.inbound is None and sent.return_date == RETURN.isoformat()
    assert sent.outbound.flight_numbers == ["BX265"] and sent.currency == "KRW"
    assert sent.raw["search"] == {"origin": "ICN", "destination": "NRT"}
    assert sent.offer_key() == load(offer_id).offer_key

    row = load(offer_id)
    assert row.booking_url == BOOKING_LINK
    assert row.inbound is not None and row.inbound["flight_numbers"] == ["BX266"]
    assert row.raw["booking_resolved"] is True
    assert row.raw["search_link"] == SEARCH_LINK
    assert row.raw["inbound_source"] == "outbound_selected_search"
    assert row.raw["search"] == {"origin": "ICN", "destination": "NRT"}
    assert row.offer_key.endswith(f"|RT:{RETURN}|ECONOMY")  # history keeps its key

    # the offer list now shows the return flight and the booking page
    trip_id = row.trip_id
    listed = api.get(f"/api/trips/{trip_id}/offers").json()
    assert listed[0]["booking_url"] == BOOKING_LINK and listed[0]["inbound"]["flight_numbers"]

    # later clicks redirect at once, without another provider request
    again = book(api, offer_id)
    assert again.status_code == 302 and again.headers["location"] == BOOKING_LINK
    assert len(details.calls) == 1


def test_book_falls_back_when_provider_finds_no_return(
    api: TestClient, db: Session, details_provider: Callable[..., Details]  # noqa: F811
) -> None:
    details = details_provider(lambda offer: offer)  # unchanged: lookup failed
    offer_id = add_offer(db)
    response = book(api, offer_id)
    assert response.status_code == 302 and response.headers["location"] == SEARCH_LINK
    row = load(offer_id)
    assert row.inbound is None and "booking_resolved" not in row.raw
    assert row.booking_url == SEARCH_LINK
    book(api, offer_id)
    assert len(details.calls) == 2  # not marked resolved: tried again on the next click


def test_book_falls_back_when_provider_raises(
    api: TestClient, db: Session, details_provider: Callable[..., Details]  # noqa: F811
) -> None:
    def boom(_offer: FlightOffer) -> FlightOffer:
        raise RuntimeError("browser crashed")

    details_provider(boom)
    offer_id = add_offer(db, booking_url=None)  # source_url only
    response = book(api, offer_id)
    assert response.status_code == 302 and response.headers["location"] == SEARCH_LINK
    assert "booking_resolved" not in load(offer_id).raw


def test_book_falls_back_on_timeout_and_stores_late_result(
    api: TestClient,
    db: Session,
    details_provider: Callable[..., Details],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = threading.Event()

    def slow(offer: FlightOffer) -> FlightOffer:
        release.wait(10)
        return with_booking_page(offer)

    details_provider(slow)
    monkeypatch.setattr(offers_api, "BOOK_RESOLVE_TIMEOUT_S", 0.2)
    offer_id = add_offer(db)
    response = book(api, offer_id)
    assert response.status_code == 302 and response.headers["location"] == SEARCH_LINK
    release.set()
    deadline = time.monotonic() + 10
    while not load(offer_id).raw.get("booking_resolved"):
        assert time.monotonic() < deadline, "late result was not stored"
        time.sleep(0.05)
    assert load(offer_id).booking_url == BOOKING_LINK
    assert book(api, offer_id).headers["location"] == BOOKING_LINK


def test_concurrent_clicks_share_one_lookup(
    db: Session, details_provider: Callable[..., Details]  # noqa: F811
) -> None:
    release = threading.Event()

    def slow(offer: FlightOffer) -> FlightOffer:
        release.wait(10)
        return with_booking_page(offer)

    details = details_provider(slow)
    offer_id = add_offer(db)
    provider_cls = fake_provider_class("x", lambda q: [], details=details)
    provider = provider_cls(None)  # type: ignore[arg-type]
    with SessionLocal() as session:
        offer = offers_api.offer_from_row(session.get(OfferRow, offer_id))
    first = offers_api._start_resolution(offer_id, offer, provider)
    second = offers_api._start_resolution(offer_id, offer, provider)
    assert first is second
    release.set()
    assert first.result(timeout=10) == BOOKING_LINK
    assert len(details.calls) == 1


# --------------------------------------------------------------------------- direct redirects


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        pytest.param({"booking_url": BOOKING_LINK}, BOOKING_LINK, id="already-booking-page"),
        pytest.param(
            {"raw": {"booking_resolved": True}, "booking_url": RESOLVED_LINK},
            RESOLVED_LINK,
            id="already-resolved",
        ),
        pytest.param({"round_trip": False}, SEARCH_LINK, id="one-way"),
        pytest.param(
            {"round_trip": False, "booking_url": None, "source_url": "https://example.test/src"},
            "https://example.test/src",
            id="one-way-source-url",
        ),
    ],
)
def test_book_redirects_without_lookup(
    api: TestClient,
    db: Session,
    details_provider: Callable[..., Details],  # noqa: F811
    kwargs: dict[str, Any],
    expected: str,
) -> None:
    details = details_provider(with_booking_page)
    offer_id = add_offer(db, **kwargs)
    response = book(api, offer_id)
    assert response.status_code == 302 and response.headers["location"] == expected
    assert details.calls == []


def test_book_provider_without_details_support_redirects(api: TestClient, db: Session) -> None:
    # the mock provider does not implement get_flight_details
    offer_id = add_offer(db, provider="mock")
    response = book(api, offer_id)
    assert response.status_code == 302 and response.headers["location"] == SEARCH_LINK
    assert "booking_resolved" not in load(offer_id).raw


@pytest.mark.parametrize("provider", ["not-installed", "unconfigured"])
def test_book_unusable_provider_redirects(
    api: TestClient,  # noqa: F811
    db: Session,
    details_provider: Callable[..., Details],
    provider: str,
) -> None:
    details = details_provider(with_booking_page, key="unconfigured", configured=False)
    offer_id = add_offer(db, provider=provider)
    response = book(api, offer_id)
    assert response.status_code == 302 and response.headers["location"] == SEARCH_LINK
    assert details.calls == []


def test_book_404_and_409(api: TestClient, db: Session) -> None:
    assert book(api, 424242).status_code == 404
    offer_id = add_offer(db, booking_url=None, source_url=None)
    response = book(api, offer_id)
    assert response.status_code == 409
    assert response.json() == {"detail": "This offer has no booking link"}


# --------------------------------------------------------------------------- Google booking links


def passengers(url: str) -> int:
    tfs = parse_qs(urlsplit(url).query)["tfs"][0]
    return sum(1 for field, _ in decode_message(decode_tfs(tfs)) if field == 8)


def expected_link(adults: int) -> str:
    query = FlightQuery("ICN", "NRT", DEPART, RETURN, adults=adults, currency="KRW")
    out, back = selected_from_leg(OUTBOUND), selected_from_leg(INBOUND)
    assert out and back
    return booking_link(query, out, back)


def test_known_return_flight_builds_google_booking_page_offline(
    api: TestClient, db: Session
) -> None:
    # e.g. the best offer, whose return flight monitoring looked up when an alert fired
    offer_id = add_offer(db, provider="google_flights", inbound=INBOUND, adults=2)
    response = book(api, offer_id)
    assert response.status_code == 302
    location = response.headers["location"]
    assert location == expected_link(2)
    assert location.startswith("https://www.google.com/travel/flights/booking?tfs=")
    assert "hl=ko" in location and "curr=KRW" in location
    assert passengers(location) == 2
    row = load(offer_id)
    assert row.booking_url == location and row.raw["booking_resolved"] is True
    assert row.raw["search_link"] == SEARCH_LINK


def test_known_return_flight_of_other_provider_keeps_its_link(api: TestClient, db: Session) -> None:
    offer_id = add_offer(db, provider="mock", inbound=INBOUND)
    assert book(api, offer_id).headers["location"] == SEARCH_LINK


def test_serpapi_style_resolution_upgrades_to_google_booking_page(
    api: TestClient, db: Session, details_provider: Callable[..., Details]  # noqa: F811
) -> None:
    # SerpApi fills the return flight but keeps Google's search URL as its link
    def fill_inbound(offer: FlightOffer) -> FlightOffer:
        return FlightOffer(**{**offer.__dict__, "inbound": INBOUND})

    details_provider(fill_inbound, key="serpapi")
    offer_id = add_offer(db, provider="serpapi", adults=1, raw={})
    response = book(api, offer_id)
    assert response.status_code == 302
    assert response.headers["location"] == expected_link(1)
    assert passengers(response.headers["location"]) == 1
    row = load(offer_id)
    assert row.raw["booking_resolved"] is True and row.inbound is not None


def test_is_google_booking_page() -> None:
    assert offers_api.is_google_booking_page(BOOKING_LINK)
    assert offers_api.is_google_booking_page("https://www.google.co.kr/travel/flights/booking?x=1")
    assert not offers_api.is_google_booking_page(SEARCH_LINK)
    assert not offers_api.is_google_booking_page("https://example.test/travel/flights/booking")
    assert not offers_api.is_google_booking_page(None)


def test_google_flights_provider_resolution_network_stubbed(
    api: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    """The real GoogleFlightsProvider (created like a search creates it) resolves the return
    flight; only its page fetch is stubbed. Mirrors the live ICN-NRT BX265/BX266 case."""
    from app.providers.flights import google_flights
    from app.providers.flights.google_flights_parse import ReturnOption

    seen: list[tuple[FlightQuery, tuple[Any, ...]]] = []

    def return_options(
        self: google_flights.GoogleFlightsProvider, query: FlightQuery, outbound: Any
    ) -> list[ReturnOption]:
        seen.append((query, tuple(outbound)))
        return [ReturnOption(price=379_800.0, leg=INBOUND, group="best")]

    monkeypatch.setattr(google_flights.GoogleFlightsProvider, "_return_options", return_options)
    offer_id = add_offer(db, provider="google_flights", adults=2)
    response = book(api, offer_id)
    assert response.status_code == 302
    location = response.headers["location"]
    assert location == expected_link(2) and passengers(location) == 2

    ((query, outbound),) = seen
    assert (query.origin, query.destination) == ("ICN", "NRT")
    assert (query.depart_date, query.return_date, query.adults) == (DEPART, RETURN, 2)
    assert [(f.carrier_code, f.number) for f in outbound] == [("BX", "265")]
    row = load(offer_id)
    assert row.inbound is not None and row.inbound["flight_numbers"] == ["BX266"]
    assert row.raw["booking_resolved"] is True and row.raw["search_link"] == SEARCH_LINK
