"""Itinerary service end-to-end: request validation, the keyless rule-based pipeline, the
Claude pipeline with a fake Anthropic client (respx for Nominatim / Open-Meteo / Overpass /
Frankfurter), fallbacks, refinements (Claude and rule-based), background runs and recovery."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import anthropic
import httpx
import httpx2
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import fx
from app.models import FlightOffer, Itinerary, Notification, SearchRun, TravelPreference, Trip
from app.schemas import ItineraryCreate, ItineraryPlan
from app.services.itinerary import geocode, service
from app.services.itinerary import weather as weather_mod
from app.services.itinerary.candidates import OVERPASS_URLS
from app.services.itinerary.models import Candidate
from app.services.itinerary.service import (
    InvalidItineraryRequest,
    ItineraryNotFoundError,
    TripNotFoundError,
    create_itinerary,
    create_refinement,
    generate_itinerary,
    parse_feedback,
    recover_stale_itineraries,
    start_generation,
)
from app.services.notifications import desktop
from app.settings_store import save_runtime_settings
from tests.itinerary_llm_fakes import FakeAnthropic, message, research_payload, tool_use

OVERPASS_FIXTURE = Path(__file__).parent / "fixtures" / "itinerary" / "overpass_asakusa.json"
ASAKUSA = {"lat": "35.7118", "lon": "139.7966", "display_name": "Asakusa, Taito, Tokyo"}
REQ = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


# --------------------------------------------------------------------------- fixtures


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _fx(request: httpx.Request) -> httpx.Response:
    base, quote = request.url.params["base"], request.url.params["quotes"]
    return httpx.Response(200, json=[{"base": base, "quote": quote, "rate": fx.static_rate(base, quote)}])


def _nominatim(request: httpx.Request) -> httpx.Response:
    query = request.url.params.get("q", "")
    return httpx.Response(200, json=[ASAKUSA] if query == "Asakusa" else [])


def _meteo(request: httpx.Request) -> httpx.Response:
    start = date.fromisoformat(request.url.params["start_date"])
    end = date.fromisoformat(request.url.params["end_date"])
    days = [(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)]
    n = len(days)
    daily: dict[str, Any] = {"time": days, "temperature_2m_max": [16.0] * n, "temperature_2m_min": [8.0] * n,
                             "precipitation_sum": [0.0] * n}
    if "forecast" in request.url.path:
        daily |= {"weather_code": [1] * n, "precipitation_probability_max": [10] * n}
    return httpx.Response(200, json={"daily": daily})


@pytest.fixture
def network(monkeypatch: pytest.MonkeyPatch) -> Iterator[respx.MockRouter]:
    """All third-party HTTP mocked (no sleeping for the Nominatim rate limit)."""
    clock = FakeClock()
    monkeypatch.setattr(geocode, "_monotonic", clock.monotonic)
    monkeypatch.setattr(geocode, "_sleep", clock.sleep)
    geocode.reset_rate_limiter()
    weather_mod.clear_cache()
    overpass = json.loads(OVERPASS_FIXTURE.read_text(encoding="utf-8"))
    with respx.mock(assert_all_called=False) as router:
        router.get(fx.FRANKFURTER_V2_URL).mock(side_effect=_fx)
        router.get(geocode.NOMINATIM_URL, name="nominatim").mock(side_effect=_nominatim)
        router.get(weather_mod.FORECAST_URL).mock(side_effect=_meteo)
        router.get(weather_mod.ARCHIVE_URL).mock(side_effect=_meteo)
        router.post(OVERPASS_URLS[0], name="overpass").mock(return_value=httpx.Response(200, json=overpass))
        yield router
    geocode.reset_rate_limiter()
    weather_mod.clear_cache()


@pytest.fixture(autouse=True)
def toasts(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Desktop notifications are captured instead of shown."""
    shown: list[Any] = []
    monkeypatch.setattr(desktop, "_is_windows", lambda: True)
    monkeypatch.setattr(desktop, "toast_support", lambda: (True, ""))
    monkeypatch.setattr(desktop, "register_aumid", lambda icon_path=None: True)
    monkeypatch.setattr(desktop, "_show_toast", shown.append)
    return shown


@pytest.fixture
def steps(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Progress steps reported by the pipeline (the real progress update still runs)."""
    seen: list[str] = []
    original = service._progress

    def record(job: Any, step: str) -> None:
        seen.append(step)
        original(job, step)

    monkeypatch.setattr(service, "_progress", record)
    return seen


def make_trip(db: Session, *, prefs: bool = True, trip_type: str = "round_trip",
              return_date: date | None = date(2026, 11, 23)) -> Trip:
    trip = Trip(name="Tokyo", origin="ICN", destination="TYO", trip_type=trip_type,
                depart_date=date(2026, 11, 20), return_date=return_date, adults=2, currency="KRW")
    if prefs:
        trip.preferences = TravelPreference(
            budget_total=3_000_000, budget_flight=1_000_000, hotel_area="Asakusa", food_wishlist=["sushi"],
            style_weights={"culture": 40, "food": 40, "sightseeing": 20},
        )
    db.add(trip)
    db.commit()
    return trip


def make_offer(db: Session, trip: Trip, *, inbound: bool = True, return_date: date | None = date(2026, 11, 23)) -> FlightOffer:
    run = SearchRun(trip_id=trip.id, status="ok", currency="KRW")
    db.add(run)
    db.flush()
    outbound = {"departure_airport": "ICN", "arrival_airport": "NRT", "departure_time": "2026-11-20T11:30",
                "arrival_time": "2026-11-20T14:00", "duration_minutes": 150, "stops": 0,
                "airlines": ["Korean Air"], "flight_numbers": ["KE703"]}
    back = {"departure_airport": "HND", "arrival_airport": "GMP", "departure_time": "2026-11-23T19:00:00",
            "arrival_time": "2026-11-23T21:20", "duration_minutes": 140, "stops": 0,
            "airlines": ["Korean Air"], "flight_numbers": ["KE2104"]}
    offer = FlightOffer(
        trip_id=trip.id, run_id=run.id, provider="google_flights", offer_key="k1", origin="ICN", destination="NRT",
        depart_date=date(2026, 11, 20), return_date=return_date, airline="Korean Air", flight_number="KE703",
        departure_airport="ICN", arrival_airport="NRT", departure_time="2026-11-20T11:30",
        arrival_time="2026-11-20T14:00", duration_minutes=150, stops=0, outbound=outbound,
        inbound=back if inbound else None, cabin="economy", price_total=812_345, price_per_person=406_172.5,
        currency="KRW",
    )
    db.add(offer)
    db.commit()
    return offer


def manual(**overrides: Any) -> ItineraryCreate:
    data: dict[str, Any] = {"arrival_at": "2026-11-20T14:00", "departure_at": "2026-11-23T19:00"}
    data.update(overrides)
    return ItineraryCreate(**data)


def reload(db: Session, itinerary_id: int) -> Itinerary:
    db.expire_all()
    row = db.get(Itinerary, itinerary_id)
    assert row is not None
    return row


def set_key(db: Session, **extra: Any) -> None:
    save_runtime_settings(db, {"api_keys": {"anthropic": "sk-ant-test"}, **extra})


def plan_reply(key: str, title: str = "AI plan") -> Any:
    """A scripted compose/refine reply that mirrors the itinerary sent in the prompt."""

    def reply(call: dict[str, Any]) -> Any:
        prompt = call["messages"][0]["content"][0]["text"]
        payload = json.loads(prompt[prompt.index("{", prompt.index("</feedback>") if "</feedback>" in prompt else 0):])
        days = [{
            "date": day["date"], "theme": "AI theme",
            "items": [{"start": i["start"], "end": i["end"], "kind": i["kind"], "title": f"AI {i['title']}",
                       "candidate_id": i["candidate_id"], "area": i["area"], "description": "Nice.", "tips": "",
                       "est_cost": -1} for i in day["items"]],
        } for day in payload[key]]
        return message(tool_use("toolu_plan", "submit_itinerary",
                                {"title": title, "summary": "AI summary", "days": days, "warnings": []}),
                       stop="tool_use")

    return reply


def research_reply() -> Any:
    return message(tool_use("toolu_r", "submit_candidates", research_payload()), stop="tool_use",
                   usage={"input_tokens": 5000, "output_tokens": 3000, "cache_creation_input_tokens": 0,
                          "cache_read_input_tokens": 0,
                          "server_tool_use": {"web_search_requests": 4, "web_fetch_requests": 0}})


# --------------------------------------------------------------------------- create


def test_create_from_offer_uses_outbound_arrival_and_inbound_departure(db: Session) -> None:
    trip = make_trip(db)
    offer = make_offer(db, trip)
    row = create_itinerary(db, trip.id, ItineraryCreate(offer_id=offer.id, notes="Anniversary trip"))
    assert row.status == "pending" and row.offer_id == offer.id
    assert (row.arrival_at, row.departure_at) == ("2026-11-20T14:00", "2026-11-23T19:00")
    assert (row.arrival_airport, row.departure_airport) == ("NRT", "HND")
    assert row.hotel_area == "Asakusa"
    request = row.request
    assert request["source"] == "offer" and request["use_ai"] is True and request["warnings"] == []
    assert request["inputs"]["city"] == "Tokyo" and request["inputs"]["notes"] == "Anniversary trip"
    assert request["inputs"]["style_weights"]["culture"] == 40
    assert request["inputs"]["budget_total"] == 3_000_000 and request["inputs"]["flight_price_total"] == 812_345
    assert request["flight"]["outbound"]["flight_numbers"] == ["KE703"]
    assert request["flight"]["inbound"]["departure_airport"] == "HND"
    assert "price_total" not in json.dumps(request["flight"])


def test_unknown_inbound_assumes_an_evening_departure(db: Session) -> None:
    trip = make_trip(db)
    offer = make_offer(db, trip, inbound=False)
    row = create_itinerary(db, trip.id, ItineraryCreate(offer_id=offer.id))
    assert row.departure_at == "2026-11-23T18:00" and row.departure_airport == "NRT"
    assert row.request["warnings"] == ["Return flight time unknown — assumed a departure at 18:00 on 2026-11-23."]
    assert row.request["flight"]["inbound_assumed"] is True


def test_manual_times_and_overrides(db: Session) -> None:
    trip = make_trip(db)
    row = create_itinerary(db, trip.id, manual(hotel_area="Shinjuku", use_ai=False))
    assert row.request["source"] == "manual" and row.request["use_ai"] is False
    assert row.arrival_airport is None and row.hotel_area == "Shinjuku"
    offer = make_offer(db, trip)
    both = create_itinerary(db, trip.id, manual(offer_id=offer.id, departure_at="2026-11-22T21:00"))
    assert both.departure_at == "2026-11-22T21:00" and both.arrival_airport == "NRT"


def test_create_validation_errors(db: Session) -> None:
    with pytest.raises(TripNotFoundError):
        create_itinerary(db, 999, manual())
    bare = make_trip(db, prefs=False)
    with pytest.raises(InvalidItineraryRequest, match="preferences"):
        create_itinerary(db, bare.id, manual())
    trip = make_trip(db)
    other = make_trip(db)
    foreign = make_offer(db, other)
    with pytest.raises(InvalidItineraryRequest, match="not found for this trip"):
        create_itinerary(db, trip.id, ItineraryCreate(offer_id=foreign.id))
    one_way = make_trip(db, trip_type="one_way", return_date=None)
    offer = make_offer(db, one_way, inbound=False, return_date=None)
    with pytest.raises(InvalidItineraryRequest, match="no return"):
        create_itinerary(db, one_way.id, ItineraryCreate(offer_id=offer.id))
    with pytest.raises(InvalidItineraryRequest, match="30 days"):
        create_itinerary(db, trip.id, manual(departure_at="2027-01-20T10:00"))
    assert db.scalars(select(Itinerary)).all() == []


# --------------------------------------------------------------------------- generate


def test_generate_without_key_is_rule_based(db: Session, network: respx.MockRouter, steps: list[str],
                                            toasts: list[Any]) -> None:
    trip = make_trip(db)
    row = create_itinerary(db, trip.id, ItineraryCreate(offer_id=make_offer(db, trip).id))
    generate_itinerary(row.id)
    row = reload(db, row.id)
    assert row.status == "done" and row.error is None and row.progress is None
    assert row.generator == "rule_based" and row.model is None
    assert row.usage["requests"] == 0 and row.usage["est_usd"] == 0
    plan = ItineraryPlan.model_validate(row.plan)
    assert plan.generator == "rule_based" and [d.date.day for d in plan.days] == [20, 21, 22, 23]
    assert any(i.kind == "activity" for d in plan.days for i in d.items)
    assert row.request["candidates"] and row.request["inputs"]["hotel_lat"] == pytest.approx(35.7118)
    assert steps == ["research", "geocode", "weather", "routes"]
    assert 1 <= network.routes["overpass"].call_count <= 2  # centre (+ hotel area) boxes
    notes = db.scalars(select(Notification)).all()
    assert len(notes) == 1 and notes[0].kind == "itinerary" and notes[0].trip_id == trip.id
    assert len(toasts) == 1


def test_generate_with_claude(db: Session, network: respx.MockRouter, steps: list[str]) -> None:
    set_key(db, language="ko")
    trip = make_trip(db)
    row = create_itinerary(db, trip.id, ItineraryCreate(offer_id=make_offer(db, trip).id))
    fake = FakeAnthropic(research_reply(), plan_reply("draft_itinerary"))
    generate_itinerary(row.id, client=fake)
    row = reload(db, row.id)
    assert row.status == "done", row.error
    assert row.generator == "claude" and row.model == "claude-opus-5-5"
    plan = ItineraryPlan.model_validate(row.plan)
    assert plan.title == "AI plan" and plan.generator == "claude"
    assert all(i.title.startswith("AI ") for d in plan.days for i in d.items)
    assert row.usage["requests"] == 2 and row.usage["web_searches"] == 4 and row.usage["est_usd"] > 0
    assert steps == ["research", "geocode", "weather", "routes", "write"]
    assert row.request["language"] == "ko"
    stored = [Candidate.model_validate(c) for c in row.request["candidates"]]
    assert len(stored) == 18 and all(c.coord_source == "llm" for c in stored)
    assert network.routes["overpass"].call_count == 0
    research_call, compose_call = fake.calls
    assert research_call["model"] == "claude-opus-5-5" and research_call["output_config"] == {"effort": "high"}
    for call in fake.calls:  # the flight price never reaches Claude
        assert "812345" not in json.dumps(call, ensure_ascii=False)
    assert "draft_itinerary" in compose_call["messages"][0]["content"][0]["text"]


def test_fatal_research_error_falls_back_to_osm_and_skips_compose(db: Session, network: respx.MockRouter) -> None:
    set_key(db)
    trip = make_trip(db)
    row = create_itinerary(db, trip.id, manual())
    error = anthropic.AuthenticationError("invalid x-api-key", response=httpx2.Response(401, request=REQ), body=None)
    fake = FakeAnthropic(error)
    generate_itinerary(row.id, client=fake)
    row = reload(db, row.id)
    assert row.status == "done" and row.generator == "rule_based" and row.model is None
    assert len(fake.calls) == 1 and network.routes["overpass"].call_count >= 1
    assert any("Anthropic API key is invalid" in w for w in row.plan["warnings"])


def test_compose_failure_keeps_the_draft(db: Session, network: respx.MockRouter) -> None:
    set_key(db)
    trip = make_trip(db)
    row = create_itinerary(db, trip.id, manual())
    refused = message(stop="refusal", stop_details={"type": "refusal", "category": None})
    generate_itinerary(row.id, client=FakeAnthropic(research_reply(), refused))
    row = reload(db, row.id)
    assert row.status == "done" and row.generator == "rule_based" and row.model == "claude-opus-5-5"
    assert row.usage["requests"] == 2
    assert any(w.startswith("Claude could not write the itinerary") for w in row.plan["warnings"])


def test_use_ai_false_ignores_the_key(db: Session, network: respx.MockRouter) -> None:
    set_key(db)
    trip = make_trip(db)
    row = create_itinerary(db, trip.id, manual(use_ai=False))
    generate_itinerary(row.id, client=FakeAnthropic())
    assert reload(db, row.id).generator == "rule_based"


def test_unexpected_failure_marks_error_and_notifies(db: Session, network: respx.MockRouter,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    trip = make_trip(db)
    row = create_itinerary(db, trip.id, manual())

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("planner exploded")

    monkeypatch.setattr(service, "plan_itinerary", boom)
    generate_itinerary(row.id)
    row = reload(db, row.id)
    assert row.status == "error" and "planner exploded" in (row.error or "") and row.progress is None
    assert db.scalars(select(Notification)).one().kind == "itinerary"


# --------------------------------------------------------------------------- refine


def _finished(db: Session, network: respx.MockRouter, client: Any = None) -> Itinerary:
    trip = make_trip(db)
    row = create_itinerary(db, trip.id, manual())
    generate_itinerary(row.id, client=client)
    row = reload(db, row.id)
    assert row.status == "done"
    return row


def test_rule_based_refinement(db: Session, network: respx.MockRouter) -> None:
    parent = _finished(db, network)
    overpass_calls = network.routes["overpass"].call_count
    names = [i["title"] for d in parent.plan["days"] for i in d["items"] if i["kind"] == "activity"]
    assert any("Sens" in n for n in names)
    child = create_refinement(db, parent.id, "Make it more relaxed, and skip Senso-ji please.", start=False)
    assert child.parent_id == parent.id and child.status == "pending" and child.request["refine_of"] == parent.id
    generate_itinerary(child.id)
    child = reload(db, child.id)
    assert child.status == "done" and child.generator == "rule_based"
    assert child.request["inputs"]["pace"] == "relaxed"
    assert child.plan["warnings"][0].startswith("Feedback applied without AI: pace → relaxed; removed Sens")
    assert not any("Sens" in i["title"] for d in child.plan["days"] for i in d["items"] if i["kind"] == "activity")
    assert network.routes["overpass"].call_count == overpass_calls  # stored candidates reused


def test_claude_refinement(db: Session, network: respx.MockRouter) -> None:
    set_key(db)
    parent = _finished(db, network, FakeAnthropic(research_reply(), plan_reply("draft_itinerary")))
    fake = FakeAnthropic(plan_reply("current_itinerary", title="Revised"))
    child = create_refinement(db, parent.id, "Less walking on the second day", start=False)
    generate_itinerary(child.id, client=fake)
    child = reload(db, child.id)
    assert child.status == "done" and child.generator == "claude" and child.plan["title"] == "Revised"
    assert child.usage["requests"] == 1
    prompt = fake.calls[0]["messages"][0]["content"][0]["text"]
    assert "Less walking on the second day" in prompt and '"current_itinerary"' in prompt
    assert [t["name"] for t in fake.calls[0]["tools"]] == ["submit_itinerary"]


def test_claude_refinement_failure_falls_back_to_rules(db: Session, network: respx.MockRouter) -> None:
    set_key(db)
    parent = _finished(db, network, FakeAnthropic(research_reply(), plan_reply("draft_itinerary")))
    child = create_refinement(db, parent.id, "more packed", start=False)
    error = anthropic.RateLimitError("slow down", response=httpx2.Response(429, request=REQ), body=None)
    generate_itinerary(child.id, client=FakeAnthropic(error))
    child = reload(db, child.id)
    assert child.status == "done" and child.generator == "rule_based"
    assert child.request["inputs"]["pace"] == "packed"
    assert any("Claude could not revise" in w for w in child.plan["warnings"])


def test_refinement_requires_a_finished_itinerary(db: Session) -> None:
    trip = make_trip(db)
    row = create_itinerary(db, trip.id, manual())
    with pytest.raises(InvalidItineraryRequest):
        create_refinement(db, row.id, "anything", start=False)
    with pytest.raises(ItineraryNotFoundError):
        create_refinement(db, 12345, "anything", start=False)


def test_parse_feedback_rules() -> None:
    candidates = [
        Candidate(name="Senso-ji", name_local="浅草寺", category="historic", lat=35.71, lon=139.79),
        Candidate(name="Tokyo National Museum", category="museum", lat=35.71, lon=139.77),
        Candidate(name="Meiji Shrine", category="historic", lat=35.67, lon=139.69),
    ]
    changes = parse_feedback("좀 더 여유롭게. 浅草寺는 빼주세요! 초밥 더 먹고 싶어요", candidates)
    assert changes.pace == "relaxed" and changes.removed == ["Senso-ji"]
    assert changes.more_foods == ["sushi"]
    changes = parse_feedback("No temples, but add Tokyo National Museum. More museums. No ramen.", candidates)
    assert set(changes.removed) == {"Senso-ji", "Meiji Shrine"}
    assert changes.must_visit == ["Tokyo National Museum"] and changes.more_styles == ["museum"]
    assert changes.avoid_foods == ["ramen"] and changes.pace is None
    assert parse_feedback("Make it more packed", candidates).pace == "packed"
    assert parse_feedback("hello there", candidates).empty


# --------------------------------------------------------------------------- background & recovery


def test_start_generation_runs_once_in_a_daemon_thread(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    release = threading.Event()
    started: list[int] = []

    def fake_generate(itinerary_id: int, *, client: Any = None) -> None:
        started.append(itinerary_id)
        release.wait(5)

    monkeypatch.setattr(service, "generate_itinerary", fake_generate)
    assert start_generation(7) is True
    assert start_generation(7) is False and service.is_generating(7)
    release.set()
    for thread in threading.enumerate():
        if thread.name == "itinerary-7":
            assert thread.daemon
            thread.join(5)
    assert started == [7] and not service.is_generating(7)


def test_create_refinement_starts_in_background(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[int] = []
    monkeypatch.setattr(service, "start_generation", lambda itinerary_id, **_: started.append(itinerary_id) or True)
    trip = make_trip(db)
    parent = Itinerary(trip_id=trip.id, status="done", plan={"title": "x", "destination": "Tokyo",
                                                             "currency_local": "JPY", "days": []}, request={})
    db.add(parent)
    db.commit()
    child = create_refinement(db, parent.id, "  fewer museums  ")
    assert started == [child.id] and child.feedback == "fewer museums"


def test_recover_stale_itineraries(db: Session) -> None:
    trip = make_trip(db)
    rows = [Itinerary(trip_id=trip.id, status=s, request={"language": "ko"}) for s in ("pending", "running", "done")]
    db.add_all(rows)
    db.commit()
    assert recover_stale_itineraries() == 2
    db.expire_all()
    statuses = [(r.status, r.error) for r in db.scalars(select(Itinerary).order_by(Itinerary.id))]
    assert statuses[0][0] == statuses[1][0] == "error" and statuses[0][1].startswith("중단됨")
    assert statuses[2] == ("done", None)
    assert recover_stale_itineraries() == 0
