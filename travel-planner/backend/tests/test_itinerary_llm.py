"""Claude itinerary calls (llm.py) against a fake Anthropic client: request shape, pause_turn
continuation, strict-tool validation retries, nudges, refusals, error mapping, source-URL
filtering, compose/refine conversion and usage math. No network."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import anthropic
import httpx
import httpx2
import pytest
import respx

from app.core import fx
from app.schemas import ItineraryPlan
from app.services.itinerary import llm as llm_mod
from app.services.itinerary.claude_source import ClaudeWebResearchSource
from app.services.itinerary.llm import ItineraryLLM, ItineraryLLMError, LLMUsage
from app.services.itinerary.models import Candidate, PlanInputs
from app.services.itinerary.planner import plan_itinerary
from tests.itinerary_llm_fakes import (
    FakeAnthropic,
    _jsonable,
    entry,
    message,
    research_payload,
    search_call,
    search_error,
    search_results,
    text,
    thinking,
    tool_use,
)

FIXTURE = Path(__file__).parent / "fixtures" / "itinerary" / "tokyo_candidates.json"
REQ = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
FLIGHT_PRICE = 987654.0
TRIP_DATES = [date(2026, 11, 20) + timedelta(days=i) for i in range(4)]


def tokyo_inputs(**overrides: Any) -> PlanInputs:
    data: dict[str, Any] = {
        "destination": "TYO", "city": "Tokyo", "country": "JP", "currency": "JPY", "timezone": "Asia/Tokyo",
        "center_lat": 35.6812, "center_lon": 139.7671,
        "arrival_at": datetime(2026, 11, 20, 14, 0), "departure_at": datetime(2026, 11, 23, 19, 0),
        "arrival_airport": "NRT", "departure_airport": "NRT", "adults": 2, "language": "ko",
        "food_wishlist": ["초밥"], "must_visit": ["Senso-ji"], "hotel_area": "Shinjuku",
        "hotel_lat": 35.6905, "hotel_lon": 139.7000,
        "budget_currency": "KRW", "budget_total": 3_000_000, "budget_flight": 1_200_000,
        "flight_price_total": FLIGHT_PRICE, "flight_currency": "KRW",
    }
    data.update(overrides)
    return PlanInputs(**data)


def make_llm(*script: Any, **kwargs: Any) -> tuple[ItineraryLLM, FakeAnthropic]:
    fake = FakeAnthropic(*script)
    return ItineraryLLM(client=fake, model=kwargs.pop("model", "claude-opus-5-5"),  # type: ignore[arg-type]
                        effort=kwargs.pop("effort", "high"), language=kwargs.pop("language", "en")), fake


def submit(payload: dict[str, Any], *, block_id: str = "toolu_1", name: str = "submit_candidates",
           **kwargs: Any) -> Any:
    return message(tool_use(block_id, name, payload), stop="tool_use", **kwargs)


def status_error(cls: type[anthropic.APIStatusError], status: int, msg: str = "boom",
                 body: object | None = None) -> anthropic.APIStatusError:
    return cls(msg, response=httpx2.Response(status, request=REQ), body=body)


def walk_objects(schema: Any) -> Iterator[dict[str, Any]]:
    if isinstance(schema, dict):
        if schema.get("type") == "object":
            yield schema
        for value in schema.values():
            yield from walk_objects(value)
    elif isinstance(schema, list):
        for value in schema:
            yield from walk_objects(value)


def _fx_handler(request: httpx.Request) -> httpx.Response:
    base, quote = request.url.params["base"], request.url.params["quotes"]
    return httpx.Response(200, json=[{"base": base, "quote": quote, "rate": fx.static_rate(base, quote)}])


@pytest.fixture(autouse=True)
def no_network() -> Iterator[respx.MockRouter]:
    """Frankfurter answers with the static table; any other HTTP call fails the test."""
    with respx.mock(assert_all_called=False) as router:
        router.get(fx.FRANKFURTER_V2_URL).mock(side_effect=_fx_handler)
        yield router


# --------------------------------------------------------------------------- research


SEEN = ["https://www.senso-ji.jp/", "https://www.tnm.jp/en/"]
CITED = "https://www.japan-guide.com/e/e3001.html"


def research_turns() -> tuple[Any, Any]:
    """Turn 1 pauses after a search (with a citation); turn 2 submits."""
    paused = message(
        thinking("sig-research"), search_call("srvtoolu_1"), search_results("srvtoolu_1", SEEN),
        search_call("srvtoolu_2"), search_error("srvtoolu_2"),
        text("Senso-ji opens at 6:00.", cite=[CITED]),
        stop="pause_turn", container="cntr_1",
        usage={"input_tokens": 2000, "output_tokens": 300, "cache_creation_input_tokens": 1500,
               "cache_read_input_tokens": 0, "server_tool_use": {"web_search_requests": 2, "web_fetch_requests": 0}},
    )
    extra = [
        entry("Senso-ji", "historic", name_local="浅草寺", is_outdoor=True, fee=0, fee_currency="JPY",
              rating=4.5, rating_source="google",
              source_urls=["http://senso-ji.jp", "https://made-up.example.com/sensoji"]),
        entry("Tokyo National Museum", "museum", lat=0, lon=0, closed_weekdays=[0], holiday_shift=True,
              source_urls=["https://www.tnm.jp/en"]),
        entry("Guide Spot", "viewpoint", source_urls=[CITED + "#top"]),
        entry("Invented Place", "attraction", source_urls=["https://not-searched.example.org/"]),
    ]
    final = submit(research_payload(extra=extra), usage={
        "input_tokens": 500, "output_tokens": 4000, "cache_creation_input_tokens": 300,
        "cache_read_input_tokens": 3500, "server_tool_use": {"web_search_requests": 0, "web_fetch_requests": 0}})
    return paused, final


def test_research_request_shape() -> None:
    llm, fake = make_llm(*research_turns())
    llm.research_candidates(tokyo_inputs())
    call = fake.calls[0]
    assert call["model"] == "claude-opus-5-5"
    assert call["max_tokens"] == 64000
    assert call["output_config"] == {"effort": "high"}
    assert call["betas"] == ["server-side-fallback-2026-07-01"]
    assert call["fallbacks"] == "default"
    assert call["tool_choice"] == {"type": "auto"}
    assert call["cache_control"] == {"type": "ephemeral"}
    for forbidden in ("thinking", "temperature", "top_p", "top_k", "output_format"):
        assert forbidden not in call
    assert call["system"] == [{"type": "text", "text": llm_mod.RESEARCH_SYSTEM, "cache_control": {"type": "ephemeral"}}]
    web, submit_tool = call["tools"]
    assert web == {
        "type": "web_search_20260209", "name": "web_search", "max_uses": 10,
        "user_location": {"type": "approximate", "city": "Tokyo", "country": "JP", "timezone": "Asia/Tokyo"},
    }
    assert submit_tool["name"] == "submit_candidates"
    assert submit_tool["strict"] is True and submit_tool["eager_input_streaming"] is True
    objects = list(walk_objects(submit_tool["input_schema"]))
    assert len(objects) == 3
    for obj in objects:  # strict schema: closed objects, every property required, no numeric limits
        assert obj["additionalProperties"] is False
        assert sorted(obj["required"]) == sorted(obj["properties"])
    assert not any(k in json.dumps(submit_tool) for k in ('"minimum"', '"maximum"', '"minLength"', '"pattern"'))
    # only the user message carries trip data; the flight price never reaches Claude
    assert call["messages"][0]["role"] == "user"
    prompt = call["messages"][0]["content"][0]["text"]
    assert "Tokyo" in prompt and "Korean" in prompt and "Senso-ji" in prompt
    assert str(int(FLIGHT_PRICE)) not in prompt and "flight_price" not in prompt


def test_research_pause_turn_continues_with_unchanged_history() -> None:
    paused, final = research_turns()
    llm, fake = make_llm(paused, final)
    candidates, usage = llm.research_candidates(tokyo_inputs())
    assert len(fake.calls) == 2
    first, second = fake.calls
    # append-only: the second request = the first + the paused assistant content, verbatim
    assert second["messages"][:1] == first["messages"]
    assert second["messages"][1] == {"role": "assistant", "content": _jsonable(paused.content)}
    assert len(second["messages"]) == 2  # no extra "continue" user message
    blocks = second["messages"][1]["content"]
    assert blocks[0] == {"type": "thinking", "thinking": "", "signature": "sig-research"}
    assert blocks[2]["content"][0]["encrypted_content"] == "ENC0=="
    # byte-identical system/tools; the container of the paused turn is passed back
    assert second["system"] == first["system"] and second["tools"] == first["tools"]
    assert "container" not in first and second["container"] == "cntr_1"
    assert len(candidates) == 4 + 12 + 6
    assert usage.requests == 2


def test_research_keeps_only_seen_source_urls() -> None:
    llm, _ = make_llm(*research_turns())
    candidates, _ = llm.research_candidates(tokyo_inputs())
    by_name = {c.name: c for c in candidates}
    senso = by_name["Senso-ji"]
    assert senso.source_urls == ["https://www.senso-ji.jp/"]  # seen form; invented URL dropped
    assert senso.sources_verified and senso.name_local == "浅草寺"
    assert senso.est_cost == 0 and senso.cost_currency == "JPY" and senso.rating == 4.5
    assert senso.coord_source == "llm" and senso.is_outdoor
    museum = by_name["Tokyo National Museum"]
    assert museum.source_urls == ["https://www.tnm.jp/en/"]
    assert museum.lat is None and museum.coord_source == "none"
    assert museum.closed_weekdays == [0] and museum.holiday_shift
    assert by_name["Guide Spot"].source_urls == [CITED]  # citation URL counts as seen
    invented = by_name["Invented Place"]
    assert invented.source_urls == [] and not invented.sources_verified
    assert by_name["Sight 0"].est_cost is None and by_name["Sight 0"].rating is None
    restaurant = by_name["Restaurant 0"]
    assert restaurant.is_food and restaurant.est_cost == 3000 and restaurant.cuisines == ["sushi"]


def test_research_usage_math() -> None:
    llm, _ = make_llm(*research_turns())
    _, usage = llm.research_candidates(tokyo_inputs())
    assert usage.input_tokens == 2500 and usage.output_tokens == 4300
    assert usage.cache_creation_input_tokens == 1800 and usage.cache_read_input_tokens == 3500
    assert usage.web_search_requests == 2 and usage.requests == 2
    expected = (2500 * 4 + 1800 * 4 * 1.25 + 3500 * 4 * 0.05 + 4300 * 20) / 1_000_000 + 2 * 0.01
    assert usage.est_usd == pytest.approx(expected)
    assert usage.to_dict() == {
        "input_tokens": 2500, "output_tokens": 4300, "cache_creation_input_tokens": 1800,
        "cache_read_input_tokens": 3500, "web_searches": 2, "requests": 2,
        "est_usd": round(expected, 4), "models": ["claude-opus-5-5"],
    }


def test_strict_tool_validation_retry() -> None:
    bad = research_payload()
    bad["candidates"][0]["opening_hours"] = [{"weekday": 9, "open": "25:00", "close": "17:00"}]
    llm, fake = make_llm(submit(bad, block_id="toolu_bad"), submit(research_payload(), block_id="toolu_ok"))
    candidates, usage = llm.research_candidates(tokyo_inputs())
    assert len(candidates) == 18 and usage.requests == 2
    history = fake.calls[1]["messages"]
    assert history[1]["role"] == "assistant" and history[1]["content"][0]["id"] == "toolu_bad"
    result = history[2]["content"][0]
    assert history[2]["role"] == "user" and result["type"] == "tool_result"
    assert result["tool_use_id"] == "toolu_bad" and result["is_error"] is True
    assert "candidates.0.opening_hours.0.weekday" in result["content"]
    assert "candidates.0.opening_hours.0.open" in result["content"]


def test_too_few_candidates_are_requested_again_then_accepted() -> None:
    few = research_payload(n_sights=3, n_food=1)
    llm, fake = make_llm(submit(few, block_id="a"), submit(few, block_id="b"), submit(few, block_id="c"))
    candidates, _ = llm.research_candidates(tokyo_inputs())
    assert len(candidates) == 4 and len(fake.calls) == 3
    assert "Only 3 sights" in fake.calls[1]["messages"][-1]["content"][0]["content"]


def test_validation_gives_up_after_two_retries() -> None:
    bad = {"candidates": [{"name": ""}], "notes": ""}
    llm, fake = make_llm(submit(bad), submit(bad), submit(bad))
    with pytest.raises(ItineraryLLMError) as info:
        llm.research_candidates(tokyo_inputs())
    assert info.value.kind == "invalid_output" and len(fake.calls) == 3
    assert info.value.usage.requests == 3


def test_end_turn_without_tool_gets_one_nudge() -> None:
    llm, fake = make_llm(message(text("Here are some places: ...")), submit(research_payload()))
    candidates, _ = llm.research_candidates(tokyo_inputs())
    assert candidates
    nudge = fake.calls[1]["messages"][-1]
    assert nudge["role"] == "user" and "submit_candidates" in nudge["content"][0]["text"]
    llm, fake = make_llm(message(text("no")), message(text("still no")))
    with pytest.raises(ItineraryLLMError) as info:
        llm.research_candidates(tokyo_inputs())
    assert info.value.kind == "invalid_output" and len(fake.calls) == 2


def test_refusal_is_reported_with_category() -> None:
    refused = message(stop="refusal", stop_details={"type": "refusal", "category": "cyber", "explanation": "x"})
    llm, fake = make_llm(refused, language="ko")
    with pytest.raises(ItineraryLLMError) as info:
        llm.research_candidates(tokyo_inputs())
    assert info.value.kind == "refusal" and not info.value.fatal
    assert "cyber" in str(info.value) and "거절" in str(info.value)
    assert info.value.usage.requests == 1 and len(fake.calls) == 1


def test_max_tokens_with_tool_use_is_not_executed() -> None:
    truncated = message(tool_use("toolu_1", "submit_candidates", {"candidates": []}), stop="max_tokens")
    llm, _ = make_llm(truncated)
    with pytest.raises(ItineraryLLMError) as info:
        llm.research_candidates(tokyo_inputs())
    assert info.value.kind == "max_tokens"


def test_pause_turn_cap() -> None:
    pauses = [message(search_call(f"s{i}"), search_results(f"s{i}", [f"https://x{i}.jp/"]), stop="pause_turn")
              for i in range(llm_mod.MAX_PAUSE_TURNS + 1)]
    llm, fake = make_llm(*pauses)
    with pytest.raises(ItineraryLLMError) as info:
        llm.research_candidates(tokyo_inputs())
    assert info.value.kind == "incomplete" and len(fake.calls) == llm_mod.MAX_PAUSE_TURNS + 1


def test_user_location_rejection_retries_without_country() -> None:
    rejected = status_error(anthropic.BadRequestError, 400, "tools.0.user_location.country: unsupported country")
    llm, fake = make_llm(rejected, submit(research_payload()))
    candidates, usage = llm.research_candidates(tokyo_inputs(country="KP"))
    assert candidates and usage.requests == 1
    assert fake.calls[0]["tools"][0]["user_location"]["country"] == "KP"
    assert "country" not in fake.calls[1]["tools"][0]["user_location"]


def test_other_bad_requests_are_not_retried() -> None:
    llm, fake = make_llm(status_error(anthropic.BadRequestError, 400, "messages: invalid"))
    with pytest.raises(ItineraryLLMError) as info:
        llm.research_candidates(tokyo_inputs())
    assert info.value.kind == "bad_request" and len(fake.calls) == 1


@pytest.mark.parametrize(
    ("error", "kind", "fatal"),
    [
        (status_error(anthropic.AuthenticationError, 401, "invalid x-api-key"), "auth", True),
        (status_error(anthropic.PermissionDeniedError, 403), "permission", True),
        (status_error(anthropic.NotFoundError, 404, "model: claude-x"), "model", True),
        (status_error(anthropic.RateLimitError, 429), "rate_limit", False),
        (status_error(anthropic.BadRequestError, 400, "Your credit balance is too low"), "billing", True),
        (status_error(anthropic.InternalServerError, 500), "overloaded", False),
        (status_error(anthropic.OverloadedError, 529), "overloaded", False),
        (status_error(anthropic.APIStatusError, 200, "stream error",
                      body={"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}),
         "overloaded", False),
        (anthropic.APITimeoutError(request=REQ), "timeout", False),
        (anthropic.APIConnectionError(request=REQ), "network", False),
    ],
)
def test_sdk_errors_are_mapped(error: Exception, kind: str, fatal: bool) -> None:
    llm, _ = make_llm(error)
    with pytest.raises(ItineraryLLMError) as info:
        llm.research_candidates(tokyo_inputs())
    assert info.value.kind == kind and info.value.fatal is fatal
    assert str(info.value) and "sk-" not in str(info.value)


def test_model_error_names_the_model() -> None:
    llm, _ = make_llm(status_error(anthropic.NotFoundError, 404), model="claude-nope")
    with pytest.raises(ItineraryLLMError, match="claude-nope"):
        llm.research_candidates(tokyo_inputs())


def test_unparseable_streamed_tool_input_is_reissued() -> None:
    llm, fake = make_llm(ValueError("Unable to parse tool parameter JSON"), submit(research_payload()))
    candidates, usage = llm.research_candidates(tokyo_inputs())
    assert candidates and usage.requests == 1 and len(fake.calls) == 2
    assert fake.calls[0]["messages"] == fake.calls[1]["messages"]


def test_haiku_disables_dynamic_filtering() -> None:
    llm, _ = make_llm(model="claude-haiku-4-5")
    web = llm.research_tools(tokyo_inputs())[0]
    assert web["allowed_callers"] == ["direct"]
    assert "allowed_callers" not in make_llm()[0].research_tools(tokyo_inputs())[0]


def test_claude_source_never_raises() -> None:
    llm, _ = make_llm(status_error(anthropic.AuthenticationError, 401))
    source = ClaudeWebResearchSource(llm)
    assert source.fetch(tokyo_inputs()) == []
    assert source.last_exception is not None and source.last_exception.fatal
    assert source.last_error and source.last_usage.requests == 0
    llm, _ = make_llm(submit(research_payload()))
    source = ClaudeWebResearchSource(llm)
    assert len(source.fetch(tokyo_inputs())) == 18 and source.last_error is None
    assert source.last_usage.requests == 1


# --------------------------------------------------------------------------- history & usage units


def test_history_content_after_server_side_fallback() -> None:
    msg = message(
        thinking("declined-sig"), search_call("s1"), search_results("s1", ["https://a.jp/"]),
        search_call("s2"), tool_use("t0", "submit_candidates", {}), text("partial"),
        {"type": "fallback", "from": {"model": "claude-opus-5-5"}, "to": {"model": "claude-opus-4-8"},
         "trigger": {"type": "refusal"}},
        thinking("served-sig"), text("continued"),
    )
    kept = [(b.type, getattr(b, "id", None) or getattr(b, "signature", None)) for b in llm_mod._history_content(msg.content)]
    assert kept == [
        ("server_tool_use", "s1"), ("web_search_tool_result", None), ("text", None), ("fallback", None),
        ("thinking", "served-sig"), ("text", None),
    ]
    plain = message(thinking("x"), text("y"))
    assert llm_mod._history_content(plain.content) == list(plain.content)


def test_usage_prices_fallback_iterations_per_model() -> None:
    msg = message(text("ok"), model="claude-opus-4-8", usage={
        "input_tokens": 1000, "output_tokens": 500, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
        "iterations": [
            {"type": "message", "input_tokens": 100, "output_tokens": 0, "cache_creation_input_tokens": 0,
             "cache_read_input_tokens": 0},
            {"type": "fallback_message", "model": "claude-opus-4-8", "input_tokens": 1000, "output_tokens": 500,
             "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
        ],
    })
    usage = LLMUsage()
    usage.add_message(msg, "claude-opus-5-5")
    assert usage.input_tokens == 1100 and usage.output_tokens == 500
    assert usage.est_usd == pytest.approx(100 * 4e-6 + 1000 * 5e-6 + 500 * 25e-6)
    assert usage.models == ["claude-opus-5-5", "claude-opus-4-8"]


def test_price_lookup() -> None:
    assert llm_mod.price_for("claude-opus-5-5").cache_read == 0.05
    assert llm_mod.price_for("claude-sonnet-5-5").input == 2.0
    assert llm_mod.price_for("claude-haiku-4-5-20251001").output == 5.0
    assert llm_mod.price_for("claude-opus-5").input == 5.0
    assert llm_mod.price_for("some-new-sonnet-model").input == 2.0
    assert llm_mod.estimate_cost_usd("claude-sonnet-5-5", input_tokens=1_000_000, cache_read_input_tokens=1_000_000,
                                     output_tokens=1_000_000, web_search_requests=1000) == pytest.approx(2 + 0.2 + 10 + 10)
    merged = LLMUsage(input_tokens=1, est_usd=0.5, models=["a"]).merge(LLMUsage(input_tokens=2, est_usd=0.25,
                                                                               models=["a", "b"]))
    assert merged.input_tokens == 3 and merged.est_usd == 0.75 and merged.models == ["a", "b"]


# --------------------------------------------------------------------------- compose & refine


def load_candidates() -> list[Candidate]:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return [Candidate(**row) for row in data["pois"] + data["restaurants"]]


WEATHER = {d: {"summary": "Clear", "temp_min_c": 8, "temp_max_c": 16, "precip_prob": 10, "precip_mm": 0,
               "source": "forecast"} for d in TRIP_DATES}


@pytest.fixture
def draft() -> tuple[PlanInputs, list[Candidate], ItineraryPlan]:
    inputs = tokyo_inputs()
    candidates = load_candidates()
    return inputs, candidates, plan_itinerary(inputs, candidates, weather=WEATHER)


def llm_itinerary(plan: ItineraryPlan, **changes: Any) -> dict[str, Any]:
    """A submit_itinerary payload mirroring ``plan`` (titles/descriptions rewritten)."""
    days = []
    for day in plan.days:
        items = [{
            "start": i.start, "end": i.end, "kind": i.kind, "title": f"LLM {i.title}",
            "candidate_id": i.candidate_id or "", "area": i.area or "", "description": f"About {i.title}",
            "tips": "Go early." if i.kind == "activity" else "", "est_cost": -1,
        } for i in day.items]
        days.append({"date": day.date.isoformat(), "theme": f"Day {day.date.day}", "items": items})
    payload = {"title": "도쿄 4일 여행", "summary": "LLM summary", "days": days,
               "warnings": ["스시 다이는 일찍 줄 서세요."]}
    payload.update(changes)
    return payload


def test_compose_converts_and_rebuilds_facts_in_code(draft: tuple[PlanInputs, list[Candidate], ItineraryPlan]) -> None:
    inputs, candidates, plan = draft
    payload = llm_itinerary(plan)
    departure_day = next(d for d in payload["days"] if any(i["kind"] == "departure" for i in d["items"]))
    departure = next(i for i in departure_day["items"] if i["kind"] == "departure")
    departure["start"] = "10:00"  # flight times are code-owned
    activity = next(i for d in payload["days"] for i in d["items"] if i["candidate_id"])
    activity.update(lat=1.0, lng=2.0, map_url="https://evil.example/", source_urls=["https://evil.example/"])
    llm, fake = make_llm(submit(payload, name="submit_itinerary"))
    composed, usage = llm.compose_itinerary(inputs, plan, candidates, weather=WEATHER)

    assert composed.generator == "claude" and composed.title == "도쿄 4일 여행"
    assert usage.requests == 1
    by_id = {c.id: c for c in candidates}
    items = [i for d in composed.days for i in d.items]
    assert all(i.title.startswith("LLM ") for i in items)
    for item in items:
        if item.candidate_id:
            candidate = by_id[item.candidate_id]
            assert (item.lat, item.lng) == (candidate.lat, candidate.lon)
            assert item.source_urls == candidate.source_urls
            assert item.map_url.startswith("https://www.google.com/maps/search/?api=1&query=")
            assert getattr(item, "links")["google_maps"] == item.map_url
            assert item.description == f"About {item.title.removeprefix('LLM ')}"
    draft_departure = next(i for d in plan.days for i in d.items if i.kind == "departure")
    new_departure = next(i for i in items if i.kind == "departure")
    assert (new_departure.start, new_departure.end) == (draft_departure.start, draft_departure.end)
    assert sum(1 for i in items if i.travel_from_prev is not None) >= 5
    assert composed.route_metrics.total_travel_minutes > 0
    assert composed.budget is not None and composed.budget.currency == "KRW"
    assert "스시 다이는 일찍 줄 서세요." in composed.warnings
    assert composed.days[0].weather == plan.days[0].weather
    assert getattr(composed, "hotel") == getattr(plan, "hotel")
    # costs: code-computed draft costs kept for the same places
    draft_costs = {i.candidate_id: (i.est_cost, i.cost_currency) for d in plan.days for i in d.items if i.candidate_id}
    assert all((i.est_cost, i.cost_currency) == draft_costs[i.candidate_id] for i in items if i.candidate_id)

    call = fake.calls[0]
    assert [t["name"] for t in call["tools"]] == ["submit_itinerary"]  # no web search when composing
    assert call["tools"][0]["strict"] is True and call["tools"][0]["eager_input_streaming"] is True
    assert call["system"][0]["text"] == llm_mod.COMPOSE_SYSTEM
    prompt = call["messages"][0]["content"][0]["text"]
    body = json.loads(prompt[prompt.index("{"):])
    assert {c["id"] for c in body["candidates"]} >= {i.candidate_id for d in plan.days for i in d.items if i.candidate_id}
    assert body["draft_itinerary"][0]["date"] == "2026-11-20"
    assert str(int(FLIGHT_PRICE)) not in prompt and "http" not in prompt


def test_compose_rejects_unknown_ids_and_missing_days(draft: tuple[PlanInputs, list[Candidate], ItineraryPlan]) -> None:
    inputs, candidates, plan = draft
    wrong_id = llm_itinerary(plan)
    wrong_id["days"][1]["items"].append({"start": "21:00", "end": "22:00", "kind": "activity", "title": "Bar",
                                         "candidate_id": "made-up-123", "area": "", "description": "",
                                         "tips": "", "est_cost": -1})
    missing_day = llm_itinerary(plan)
    missing_day["days"] = missing_day["days"][:-1]
    llm, fake = make_llm(submit(wrong_id, name="submit_itinerary", block_id="t1"),
                         submit(missing_day, name="submit_itinerary", block_id="t2"),
                         submit(llm_itinerary(plan), name="submit_itinerary", block_id="t3"))
    composed, usage = llm.compose_itinerary(inputs, plan, candidates)
    assert composed.generator == "claude" and usage.requests == 3
    first_error = fake.calls[1]["messages"][-1]["content"][0]
    assert first_error["is_error"] and "made-up-123" in first_error["content"]
    second_error = fake.calls[2]["messages"][-1]["content"][0]
    assert second_error["is_error"] and "missing 2026-11-23" in second_error["content"]


def test_compose_requires_arrival_and_valid_times(draft: tuple[PlanInputs, list[Candidate], ItineraryPlan]) -> None:
    inputs, candidates, plan = draft
    no_arrival = llm_itinerary(plan)
    no_arrival["days"][0]["items"] = [i for i in no_arrival["days"][0]["items"] if i["kind"] != "arrival"]
    bad_time = llm_itinerary(plan)
    bad_time["days"][1]["items"][0]["start"] = "noon"
    llm, fake = make_llm(submit(no_arrival, name="submit_itinerary", block_id="a"),
                         submit(bad_time, name="submit_itinerary", block_id="b"),
                         submit(bad_time, name="submit_itinerary", block_id="c"))
    with pytest.raises(ItineraryLLMError) as info:
        llm.compose_itinerary(inputs, plan, candidates)
    assert info.value.kind == "invalid_output"
    assert "arrival item is missing" in fake.calls[1]["messages"][-1]["content"][0]["content"]
    assert "days.1.items.0.start" in fake.calls[2]["messages"][-1]["content"][0]["content"]


def test_refine_shares_the_cached_prefix_and_sends_feedback(
    draft: tuple[PlanInputs, list[Candidate], ItineraryPlan],
) -> None:
    inputs, candidates, plan = draft
    llm, fake = make_llm(submit(llm_itinerary(plan), name="submit_itinerary"),
                         submit(llm_itinerary(plan, title="Revised"), name="submit_itinerary"))
    composed, _ = llm.compose_itinerary(inputs, plan, candidates, weather=WEATHER)
    revised, usage = llm.refine_itinerary(inputs, composed, "Less walking on day 2, please.", candidates)
    assert revised.title == "Revised" and revised.generator == "claude" and usage.requests == 1
    compose_call, refine_call = fake.calls
    assert refine_call["system"] == compose_call["system"] and refine_call["tools"] == compose_call["tools"]
    prompt = refine_call["messages"][0]["content"][0]["text"]
    assert "<feedback>\nLess walking on day 2, please.\n</feedback>" in prompt
    assert '"current_itinerary"' in prompt
    # the earlier composer's warning is not carried into the revision unless repeated
    assert revised.warnings.count("스시 다이는 일찍 줄 서세요.") == 1


def test_refine_keeps_base_places_without_stored_facts(
    draft: tuple[PlanInputs, list[Candidate], ItineraryPlan],
) -> None:
    inputs, _, plan = draft
    llm, _ = make_llm(submit(llm_itinerary(plan), name="submit_itinerary"))
    revised, _ = llm.refine_itinerary(inputs, plan, "keep it", [])
    kept = [i for d in revised.days for i in d.items if i.candidate_id]
    assert kept and all(i.lat is not None for i in kept)
    assert all(getattr(i, "coords_approximate", False) for i in kept)
