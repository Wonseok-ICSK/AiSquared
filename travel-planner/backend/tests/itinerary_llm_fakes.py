"""Test doubles for the itinerary Claude calls (no network).

* :class:`FakeAnthropic` — mimics exactly the SDK surface ``llm.py`` uses:
  ``client.beta.messages.stream(**kwargs)`` returning a context manager whose
  ``get_final_message()`` returns a scripted :class:`BetaMessage`. Every call's kwargs are
  snapshotted as JSON (the history list is mutated afterwards).
* Builders for response blocks / messages and ``submit_candidates`` entries.
* :func:`sse_body` / :func:`sse_response` — Server-Sent-Events bodies for the *real* SDK
  pointed at an ``httpx2.MockTransport`` (request-shape tests).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx2
from anthropic.types.beta import BetaMessage
from pydantic import BaseModel

DEFAULT_USAGE = {"input_tokens": 1000, "output_tokens": 200, "cache_creation_input_tokens": 0,
                 "cache_read_input_tokens": 0}


# --------------------------------------------------------------------------- fake client


def _jsonable(value: Any) -> Any:
    def default(obj: Any) -> Any:
        if isinstance(obj, BaseModel):
            return obj.model_dump(mode="json", by_alias=True, exclude_none=True)
        raise TypeError(f"not serializable: {type(obj)!r}")

    return json.loads(json.dumps(value, default=default))


class FakeStream:
    """The object ``with client.beta.messages.stream(...) as stream`` yields."""

    def __init__(self, message: BetaMessage) -> None:
        self._message = message
        self.request_id = "req_fake"

    def __enter__(self) -> FakeStream:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def get_final_message(self) -> BetaMessage:
        return self._message


Scripted = BetaMessage | BaseException | Callable[[dict[str, Any]], BetaMessage]


class FakeMessages:
    """``client.beta.messages`` with a script of responses (or exceptions to raise)."""

    def __init__(self, script: list[Scripted]) -> None:
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []

    def stream(self, **kwargs: Any) -> FakeStream:
        self.calls.append(_jsonable(kwargs))
        if not self.script:
            raise AssertionError("unexpected extra Claude request")
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        if callable(item) and not isinstance(item, BetaMessage):
            item = item(self.calls[-1])
        return FakeStream(item)


class _Beta:
    def __init__(self, messages: FakeMessages) -> None:
        self.messages = messages


class FakeAnthropic:
    """Scripted stand-in for ``anthropic.Anthropic``."""

    def __init__(self, *script: Scripted) -> None:
        self.beta = _Beta(FakeMessages(list(script)))

    @property
    def calls(self) -> list[dict[str, Any]]:
        return self.beta.messages.calls

    @property
    def remaining(self) -> int:
        return len(self.beta.messages.script)


# --------------------------------------------------------------------------- blocks & messages


def text(value: str, *, cite: list[str] | None = None) -> dict[str, Any]:
    block: dict[str, Any] = {"type": "text", "text": value}
    if cite:
        block["citations"] = [
            {"type": "web_search_result_location", "url": url, "title": "Cited", "cited_text": "…",
             "encrypted_index": "EIDX"}
            for url in cite
        ]
    return block


def thinking(signature: str = "sig-1") -> dict[str, Any]:
    return {"type": "thinking", "thinking": "", "signature": signature}


def search_call(block_id: str, query: str = "tokyo sights") -> dict[str, Any]:
    return {"type": "server_tool_use", "id": block_id, "name": "web_search", "input": {"query": query}}


def search_results(block_id: str, urls: list[str]) -> dict[str, Any]:
    return {
        "type": "web_search_tool_result", "tool_use_id": block_id,
        "content": [{"type": "web_search_result", "url": url, "title": f"Page {i}",
                     "encrypted_content": f"ENC{i}==", "page_age": None} for i, url in enumerate(urls)],
    }


def search_error(block_id: str, code: str = "unavailable") -> dict[str, Any]:
    return {"type": "web_search_tool_result", "tool_use_id": block_id,
            "content": {"type": "web_search_tool_result_error", "error_code": code}}


def tool_use(block_id: str, name: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {"type": "tool_use", "id": block_id, "name": name, "input": payload}


def message(
    *blocks: dict[str, Any],
    stop: str = "end_turn",
    usage: dict[str, Any] | None = None,
    model: str = "claude-opus-5-5",
    container: str | None = None,
    stop_details: dict[str, Any] | None = None,
) -> BetaMessage:
    data: dict[str, Any] = {
        "id": "msg_fake", "type": "message", "role": "assistant", "model": model,
        "content": list(blocks), "stop_reason": stop, "stop_sequence": None,
        "usage": usage or dict(DEFAULT_USAGE),
    }
    if container:
        data["container"] = {"id": container, "expires_at": "2026-10-06T12:00:00Z"}
    if stop_details:
        data["stop_details"] = stop_details
    return BetaMessage.model_validate(data)


# --------------------------------------------------------------------------- research payloads


def entry(name: str, category: str = "attraction", **overrides: Any) -> dict[str, Any]:
    """One complete ``submit_candidates`` entry (every strict-schema field present)."""
    data: dict[str, Any] = {
        "name": name, "name_local": "", "category": category, "styles": [], "area": "Asakusa",
        "lat": 35.7148, "lon": 139.7967, "visit_minutes": 60,
        "opening_hours": [{"weekday": d, "open": "09:00", "close": "17:00"} for d in range(7)],
        "opening_hours_text": "", "closed_weekdays": [], "closed_on_holidays": False, "holiday_shift": False,
        "best_time": "any", "is_outdoor": False, "fee": -1, "fee_currency": "", "rating": -1,
        "rating_source": "", "cuisine": "", "price_level": -1, "description": "", "booking_required": False,
        "source_urls": [],
    }
    data.update(overrides)
    return data


def research_payload(n_sights: int = 12, n_food: int = 6, extra: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """A valid submission with ``n_sights`` sights and ``n_food`` restaurants spread over Tokyo."""
    sights = [entry(f"Sight {i}", "museum", lat=35.68 + i * 0.004, lon=139.70 + i * 0.006, area=f"Area {i % 3}")
              for i in range(n_sights)]
    food = [entry(f"Restaurant {i}", "restaurant", cuisine="sushi", lat=35.69 + i * 0.003,
                  lon=139.71 + i * 0.004, area=f"Area {i % 3}", fee=3000, fee_currency="JPY",
                  opening_hours=[{"weekday": d, "open": "11:00", "close": "22:00"} for d in range(7)])
            for i in range(n_food)]
    return {"candidates": [*(extra or []), *sights, *food], "notes": ""}


# --------------------------------------------------------------------------- SSE for the real SDK


def sse_events(msg: dict[str, Any], *, json_chunk: int = 40) -> list[dict[str, Any]]:
    """Stream events reproducing ``msg`` (a BetaMessage-like dict); tool inputs are split into
    ``input_json_delta`` chunks of ``json_chunk`` characters."""
    usage = dict(msg.get("usage") or DEFAULT_USAGE)
    start = {k: v for k, v in msg.items() if k not in ("content", "stop_reason", "container")}
    start.update(content=[], stop_reason=None, stop_sequence=None,
                 usage={**usage, "output_tokens": 1})
    events: list[dict[str, Any]] = [{"type": "message_start", "message": start}]
    for index, block in enumerate(msg["content"]):
        kind = block["type"]
        if kind == "text":
            events.append({"type": "content_block_start", "index": index, "content_block": {"type": "text", "text": ""}})
            events.append({"type": "content_block_delta", "index": index,
                           "delta": {"type": "text_delta", "text": block["text"]}})
            for citation in block.get("citations") or []:
                events.append({"type": "content_block_delta", "index": index,
                               "delta": {"type": "citations_delta", "citation": citation}})
        elif kind == "thinking":
            events.append({"type": "content_block_start", "index": index,
                           "content_block": {"type": "thinking", "thinking": "", "signature": ""}})
            events.append({"type": "content_block_delta", "index": index,
                           "delta": {"type": "signature_delta", "signature": block["signature"]}})
        elif kind in ("tool_use", "server_tool_use"):
            events.append({"type": "content_block_start", "index": index, "content_block": {**block, "input": {}}})
            raw = json.dumps(block["input"], ensure_ascii=False)
            for pos in range(0, len(raw), json_chunk):
                events.append({"type": "content_block_delta", "index": index,
                               "delta": {"type": "input_json_delta", "partial_json": raw[pos:pos + json_chunk]}})
        else:
            events.append({"type": "content_block_start", "index": index, "content_block": block})
        events.append({"type": "content_block_stop", "index": index})
    delta: dict[str, Any] = {"stop_reason": msg["stop_reason"], "stop_sequence": None}
    if msg.get("container"):
        delta["container"] = msg["container"]
    final_usage: dict[str, Any] = {"output_tokens": usage.get("output_tokens", 0)}
    if usage.get("server_tool_use"):
        final_usage["server_tool_use"] = usage["server_tool_use"]
    events.append({"type": "message_delta", "delta": delta, "usage": final_usage})
    events.append({"type": "message_stop"})
    return events


def sse_body(events: list[dict[str, Any]]) -> bytes:
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e, ensure_ascii=False)}\n\n" for e in events).encode()


def sse_response(msg: dict[str, Any]) -> httpx2.Response:
    """A 200 SSE response streaming ``msg``."""
    return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=sse_body(sse_events(msg)))


def message_dict(*blocks: dict[str, Any], stop: str, usage: dict[str, Any] | None = None,
                 container: str | None = None) -> dict[str, Any]:
    data: dict[str, Any] = {"id": "msg_sse", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
                            "content": list(blocks), "stop_reason": stop, "usage": usage or dict(DEFAULT_USAGE)}
    if container:
        data["container"] = {"id": container, "expires_at": "2026-10-06T12:00:00Z"}
    return data
