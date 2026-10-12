"""llm.py through the *real* anthropic SDK (1.11, httpx2) pointed at a mock transport that
streams SSE: verifies the exact HTTP requests (headers, JSON body, append-only history with
encrypted search results and thinking signatures), streamed tool-input parsing and error
mapping. No network; no Anthropic key exists on this machine, so live calls are not tested."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import anthropic
import httpx2
import pytest

from app.services.itinerary.llm import ItineraryLLM, ItineraryLLMError
from tests.itinerary_llm_fakes import (
    message_dict,
    research_payload,
    search_call,
    search_results,
    sse_body,
    sse_response,
    text,
    thinking,
    tool_use,
)
from tests.test_itinerary_llm import tokyo_inputs

API_KEY = "sk-ant-test-not-a-real-key"


class Recorder:
    """Mock transport handler: records requests, replies from a script."""

    def __init__(self, *replies: httpx2.Response | Callable[[httpx2.Request], httpx2.Response]) -> None:
        self.replies = list(replies)
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        reply = self.replies.pop(0)
        return reply(request) if callable(reply) else reply

    def body(self, index: int) -> dict[str, Any]:
        return json.loads(self.requests[index].content)


def sdk_llm(recorder: Recorder, **kwargs: Any) -> ItineraryLLM:
    client = anthropic.Anthropic(
        api_key=API_KEY, max_retries=0,
        http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(recorder)),
    )
    return ItineraryLLM(client=client, model="claude-opus-5-5", effort=kwargs.get("effort", "medium"),
                        language="en")


PAUSED = message_dict(
    thinking("sig-abc"), search_call("srvtoolu_1", "Senso-ji opening hours"),
    search_results("srvtoolu_1", ["https://www.senso-ji.jp/"]),
    text("Senso-ji is open early.", cite=["https://www.japan-guide.com/e/e3001.html"]),
    stop="pause_turn", container="cntr_42",
    usage={"input_tokens": 1200, "output_tokens": 300, "cache_creation_input_tokens": 900,
           "cache_read_input_tokens": 0, "server_tool_use": {"web_search_requests": 1, "web_fetch_requests": 0}},
)


def test_research_requests_through_the_real_sdk() -> None:
    payload = research_payload()
    payload["candidates"][0]["source_urls"] = ["https://senso-ji.jp", "https://fake.example/"]
    final = message_dict(tool_use("toolu_1", "submit_candidates", payload), stop="tool_use")
    recorder = Recorder(sse_response(PAUSED), sse_response(final))
    candidates, usage = sdk_llm(recorder).research_candidates(tokyo_inputs())

    assert len(recorder.requests) == 2
    first = recorder.requests[0]
    assert first.url.path == "/v1/messages"
    assert first.headers["anthropic-beta"] == "server-side-fallback-2026-07-01"
    assert first.headers["x-api-key"] == API_KEY
    body = recorder.body(0)
    assert sorted(body) == ["cache_control", "fallbacks", "max_tokens", "messages", "model", "output_config",
                            "stream", "system", "tool_choice", "tools"]
    assert body["stream"] is True and body["fallbacks"] == "default" and body["max_tokens"] == 64000
    assert body["output_config"] == {"effort": "medium"} and body["tool_choice"] == {"type": "auto"}
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert body["tools"][0]["type"] == "web_search_20260209"
    assert body["tools"][1]["strict"] is True and body["tools"][1]["eager_input_streaming"] is True

    second = recorder.body(1)
    assert second["container"] == "cntr_42"
    assert second["tools"] == body["tools"] and second["system"] == body["system"]
    assert second["messages"][0] == body["messages"][0]
    assistant = second["messages"][1]
    assert assistant["role"] == "assistant"
    sent = [{k: v for k, v in b.items() if v is not None} for b in assistant["content"]]
    expected = [{k: v for k, v in b.items() if v is not None} for b in PAUSED["content"]]
    for block in expected[2]["content"]:
        block.pop("page_age", None)
    for block in sent[2]["content"]:
        block.pop("page_age", None)
    assert sent == expected  # thinking signature, encrypted search results and citations unchanged
    assert len(second["messages"]) == 2

    # streamed (chunked) tool input parsed; URLs verified against the search results/citations
    assert len(candidates) == 18
    first_candidate = candidates[0]
    assert first_candidate.source_urls == ["https://www.senso-ji.jp/"] and first_candidate.sources_verified
    assert usage.requests == 2 and usage.web_search_requests == 1
    assert usage.input_tokens == 1200 + 1000 and usage.cache_creation_input_tokens == 900


def test_authentication_error_through_the_sdk() -> None:
    error = httpx2.Response(401, json={"type": "error", "error": {"type": "authentication_error",
                                                                   "message": "invalid x-api-key"}})
    with pytest.raises(ItineraryLLMError) as info:
        sdk_llm(Recorder(error)).research_candidates(tokyo_inputs())
    assert info.value.kind == "auth" and info.value.fatal
    assert API_KEY not in str(info.value) and API_KEY not in (info.value.detail or "")


def test_mid_stream_error_event_through_the_sdk() -> None:
    events = [
        {"type": "message_start", "message": {**message_dict(stop="end_turn"), "content": [], "stop_reason": None}},
        {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}},
    ]
    response = httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=sse_body(events))
    with pytest.raises(ItineraryLLMError) as info:
        sdk_llm(Recorder(response)).research_candidates(tokyo_inputs())
    assert info.value.kind == "overloaded"


def test_refusal_through_the_sdk() -> None:
    refused = message_dict(stop="refusal")
    events_response = sse_response(refused)
    with pytest.raises(ItineraryLLMError) as info:
        sdk_llm(Recorder(events_response)).research_candidates(tokyo_inputs())
    assert info.value.kind == "refusal"
