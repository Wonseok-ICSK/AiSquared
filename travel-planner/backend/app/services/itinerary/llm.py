"""Claude half of the itinerary pipeline (DESIGN §10 steps 2, 4 and 6; §13.4).

:class:`ItineraryLLM` makes three kinds of calls, all through
``client.beta.messages.stream(...)`` with the server-side refusal fallback
(``betas=["server-side-fallback-2026-07-01"]``, ``fallbacks="default"``), the model and
``output_config.effort`` from the runtime settings and ``max_tokens=64000``. Thinking is
never configured (Opus 5.5 always thinks adaptively), no temperature, no assistant prefill
and no forced ``tool_choice``: the prompt asks for the tool and ``{"type": "auto"}`` is sent.

* :meth:`ItineraryLLM.research_candidates` — server tool ``web_search_20260209`` (max 10
  uses, approximate ``user_location``; retried once without ``country`` when the API rejects
  the location) plus the strict client tool ``submit_candidates``. Returns
  :class:`~app.services.itinerary.models.Candidate` facts whose ``source_urls`` are only URLs
  that actually appeared in ``web_search_tool_result`` blocks or citations.
* :meth:`ItineraryLLM.compose_itinerary` — no web search; the optimised draft plan + candidate
  facts go in, the strict client tool ``submit_itinerary`` (a simplified LLM-facing schema)
  comes out and is converted back into an :class:`~app.schemas.ItineraryPlan`: coordinates,
  links, travel hops, day costs, budget and route metrics are rebuilt in code
  (:func:`~app.services.itinerary.planner.finalize_plan`), then
  :func:`~app.services.itinerary.planner.check_plan` attaches warnings.
* :meth:`ItineraryLLM.refine_itinerary` — the same call shape as compose, with the traveller's
  feedback and the current plan.

Every call runs one append-only conversation loop: ``pause_turn`` → the assistant content is
appended unchanged and the request re-sent (at most :data:`MAX_PAUSE_TURNS` times); a
submitted tool input is validated with Pydantic (eager input streaming disables server-side
validation) and a failure goes back as an ``is_error`` ``tool_result`` (at most
:data:`MAX_VALIDATION_RETRIES` times); an answer without the tool gets one user nudge.
``stop_reason`` is checked before any content is read; blocks are selected by type. The
system prompt (with a ``cache_control`` breakpoint) and the tools stay byte-identical across
the requests of a conversation, and automatic caching covers the growing history.

Only destination, dates, flight *times/airports* and the traveller's own preferences are sent
to Claude — never flight prices or any other provider data (Naver's terms forbid AI use).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Generic, Literal, TypeVar
from urllib.parse import urlsplit

import anthropic
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.schemas import STYLE_KEYS, ItineraryPlan, PlanDay, PlanItem, PlanItemKind
from app.services.itinerary import budget as budget_mod
from app.services.itinerary import calendar_util
from app.services.itinerary import weather as weather_mod
from app.services.itinerary.candidates import merge_candidates, name_matches
from app.services.itinerary.models import (
    BEST_TIMES,
    CATEGORIES,
    Candidate,
    OpeningPeriod,
    PlanInputs,
    canonical_cuisine,
    hhmm_to_minutes,
    minutes_to_hhmm,
)
from app.services.itinerary.planner import TEXTS as PLANNER_TEXTS
from app.services.itinerary.planner import check_plan, finalize_plan

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_EFFORT",
    "DEFAULT_MODEL",
    "FALLBACK_BETA",
    "MAX_TOKENS",
    "PRICES",
    "ItineraryLLM",
    "ItineraryLLMError",
    "LLMUsage",
    "ModelPrice",
    "estimate_cost_usd",
    "harvest_source_urls",
    "price_for",
]

# --------------------------------------------------------------------------- constants

DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_EFFORT = "xhigh"
MAX_TOKENS = 64000
FALLBACK_BETA = "server-side-fallback-2026-07-01"
WEB_SEARCH_TOOL_TYPE = "web_search_20260209"
WEB_SEARCH_MAX_USES = 10
MAX_PAUSE_TURNS = 6
MAX_VALIDATION_RETRIES = 2
MAX_JSON_RETRIES = 2
MAX_REQUESTS = 14
"""Hard cap on API requests of one conversation (pauses + retries + nudge + JSON re-issues)."""

MIN_ATTRACTIONS = 10
MIN_RESTAURANTS = 5
MAX_ATTRACTIONS = 30
MAX_RESTAURANTS = 15
MAX_SOURCE_URLS = 3
MAX_COMPOSE_CANDIDATES = 60
DESCRIPTION_CHARS = 240

_CODE_BUILT_EXTRAS = frozenset({"links", "directions_url", "coords_approximate"})
"""Item extras only code may set (rebuilt by ``finalize_plan``)."""
_WEEKDAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_LANGUAGE_NAMES = {"ko": "Korean (한국어)", "en": "English"}


# --------------------------------------------------------------------------- pricing & usage


@dataclass(frozen=True)
class ModelPrice:
    """List prices in USD per million tokens; cache multipliers relative to ``input``."""

    input: float
    output: float
    cache_read: float = 0.1
    cache_write: float = 1.25


PRICES: dict[str, ModelPrice] = {
    "claude-opus-5-5": ModelPrice(4.0, 20.0, cache_read=0.05),  # cache reads $0.20/MTok
    "claude-sonnet-5-5": ModelPrice(2.0, 10.0),
    "claude-haiku-4-5": ModelPrice(1.0, 5.0),
    # possible server-side fallback targets of Opus 5.5
    "claude-opus-5": ModelPrice(5.0, 25.0),
    "claude-opus-4-8": ModelPrice(5.0, 25.0),
}
"""Known model prices (the cache-write multiplier is the 5-minute TTL's)."""
_FAMILY_PRICES = {
    "opus": PRICES["claude-opus-5-5"],
    "sonnet": PRICES["claude-sonnet-5-5"],
    "haiku": PRICES["claude-haiku-4-5"],
}
WEB_SEARCH_USD = 10.0 / 1000
"""Web search: $10 per 1,000 searches (plus the tokens of the results)."""


def price_for(model: str | None) -> ModelPrice:
    """Price of ``model``: exact id, else the longest known id it starts with (dated ids),
    else its family (opus / sonnet / haiku), else Opus 5.5."""
    name = (model or "").strip().lower()
    if name in PRICES:
        return PRICES[name]
    prefixes = [key for key in PRICES if name.startswith(key)]
    if prefixes:
        return PRICES[max(prefixes, key=len)]
    for family, price in _FAMILY_PRICES.items():
        if family in name:
            return price
    return PRICES[DEFAULT_MODEL]


def estimate_cost_usd(
    model: str | None,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_creation_input_tokens: int = 0,
    cache_read_input_tokens: int = 0,
    web_search_requests: int = 0,
) -> float:
    """Estimated USD for one request's token counts (``input_tokens`` = the uncached rest)."""
    price = price_for(model)
    per_token = price.input / 1_000_000
    usd = (
        input_tokens * per_token
        + cache_creation_input_tokens * per_token * price.cache_write
        + cache_read_input_tokens * per_token * price.cache_read
        + output_tokens * price.output / 1_000_000
        + web_search_requests * WEB_SEARCH_USD
    )
    return usd


@dataclass
class LLMUsage:
    """Accumulated usage of one or more Claude requests (tokens, searches, estimated USD)."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    web_search_requests: int = 0
    requests: int = 0
    est_usd: float = 0.0
    models: list[str] = field(default_factory=list)

    def add_message(self, message: Any, requested_model: str) -> None:
        """Add one response's ``usage``.

        When a server-side fallback served the turn (a ``fallback_message`` entry in
        ``usage.iterations``), the per-attempt iterations are the billing truth (each priced at
        its own model); otherwise the top-level counts are used with the response's model."""
        usage = getattr(message, "usage", None)
        self.requests += 1
        if usage is None:
            return
        iterations = list(getattr(usage, "iterations", None) or [])
        if any(getattr(item, "type", None) == "fallback_message" for item in iterations):
            entries = [
                (getattr(item, "model", None) or requested_model, item.input_tokens, item.output_tokens,
                 item.cache_creation_input_tokens or 0, item.cache_read_input_tokens or 0)
                for item in iterations if getattr(item, "type", None) in ("message", "fallback_message")
            ]
        else:
            entries = [(
                getattr(message, "model", None) or requested_model, usage.input_tokens or 0,
                usage.output_tokens or 0, usage.cache_creation_input_tokens or 0,
                usage.cache_read_input_tokens or 0,
            )]
        for model, inp, out, written, read in entries:
            self.input_tokens += inp
            self.output_tokens += out
            self.cache_creation_input_tokens += written
            self.cache_read_input_tokens += read
            self.est_usd += estimate_cost_usd(
                model, input_tokens=inp, output_tokens=out,
                cache_creation_input_tokens=written, cache_read_input_tokens=read,
            )
            if model and model not in self.models:
                self.models.append(str(model))
        server = getattr(usage, "server_tool_use", None)
        searches = int(getattr(server, "web_search_requests", 0) or 0) if server is not None else 0
        self.web_search_requests += searches
        self.est_usd += searches * WEB_SEARCH_USD

    def merge(self, other: LLMUsage | None) -> LLMUsage:
        """Add ``other`` into this usage (in place) and return ``self``."""
        if other is None:
            return self
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_creation_input_tokens += other.cache_creation_input_tokens
        self.cache_read_input_tokens += other.cache_read_input_tokens
        self.web_search_requests += other.web_search_requests
        self.requests += other.requests
        self.est_usd += other.est_usd
        self.models.extend(m for m in other.models if m not in self.models)
        return self

    def to_dict(self) -> dict[str, Any]:
        """JSON form stored in ``Itinerary.usage`` (``web_searches``/``est_usd`` per DESIGN)."""
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_creation_input_tokens": self.cache_creation_input_tokens,
            "cache_read_input_tokens": self.cache_read_input_tokens,
            "web_searches": self.web_search_requests,
            "requests": self.requests,
            "est_usd": round(self.est_usd, 4),
            "models": list(self.models),
        }


# --------------------------------------------------------------------------- errors

ErrorKind = Literal[
    "auth", "permission", "model", "billing", "rate_limit", "overloaded", "timeout", "network",
    "too_large", "bad_request", "api", "refusal", "max_tokens", "context", "invalid_output",
    "incomplete",
]
FATAL_KINDS: frozenset[str] = frozenset({"auth", "permission", "model", "billing"})
"""Errors that make further Claude calls pointless (skip the remaining steps)."""

_ERROR_TEXT: dict[str, dict[str, str]] = {
    "auth": {
        "en": "The Anthropic API key is invalid. Check it in Settings.",
        "ko": "Anthropic API 키가 올바르지 않습니다. 설정에서 확인하세요.",
    },
    "permission": {
        "en": "The Anthropic API key is not allowed to use this model or feature.",
        "ko": "Anthropic API 키에 이 모델/기능을 사용할 권한이 없습니다.",
    },
    "model": {
        "en": "The Claude model “{model}” is not available. Choose another model in Settings.",
        "ko": "Claude 모델 “{model}”을(를) 사용할 수 없습니다. 설정에서 다른 모델을 선택하세요.",
    },
    "billing": {
        "en": "The Anthropic account has no remaining credit.",
        "ko": "Anthropic 계정의 크레딧이 부족합니다.",
    },
    "rate_limit": {
        "en": "Claude is rate-limited right now. Try again in a few minutes.",
        "ko": "Claude 요청 한도를 초과했습니다. 몇 분 후 다시 시도하세요.",
    },
    "overloaded": {
        "en": "Claude is temporarily unavailable (overloaded). Try again later.",
        "ko": "Claude가 일시적으로 혼잡합니다. 잠시 후 다시 시도하세요.",
    },
    "timeout": {
        "en": "The request to Claude timed out.",
        "ko": "Claude 요청 시간이 초과되었습니다.",
    },
    "network": {
        "en": "Could not reach the Claude API (network error).",
        "ko": "Claude API에 연결할 수 없습니다 (네트워크 오류).",
    },
    "too_large": {
        "en": "The request to Claude was too large.",
        "ko": "Claude 요청이 너무 큽니다.",
    },
    "bad_request": {
        "en": "Claude rejected the request.",
        "ko": "Claude가 요청을 거부했습니다.",
    },
    "api": {
        "en": "The Claude API returned an error.",
        "ko": "Claude API 오류가 발생했습니다.",
    },
    "refusal": {
        "en": "Claude declined this request{category}.",
        "ko": "Claude가 이 요청을 거절했습니다{category}.",
    },
    "max_tokens": {
        "en": "Claude's answer was cut off (output limit reached).",
        "ko": "Claude의 응답이 출력 한도에 도달해 잘렸습니다.",
    },
    "context": {
        "en": "The conversation with Claude grew too long.",
        "ko": "Claude와의 대화가 너무 길어졌습니다.",
    },
    "invalid_output": {
        "en": "Claude did not return a usable result.",
        "ko": "Claude가 사용할 수 있는 결과를 반환하지 않았습니다.",
    },
    "incomplete": {
        "en": "Claude did not finish in time.",
        "ko": "Claude가 작업을 끝내지 못했습니다.",
    },
}


class ItineraryLLMError(Exception):
    """A Claude call failed; ``str(exc)`` is a user-facing message.

    ``kind`` classifies the failure (see :data:`ErrorKind`), ``fatal`` is True when further
    calls are pointless (invalid key, no permission/credit, unknown model), ``detail`` holds
    the technical reason (never secrets) and ``usage`` what was spent before failing.
    """

    def __init__(
        self,
        kind: ErrorKind,
        message: str,
        *,
        detail: str | None = None,
        usage: LLMUsage | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.detail = detail
        self.usage = usage or LLMUsage()
        self.fatal = kind in FATAL_KINDS


class _Rejected(Exception):
    """A submitted tool input failed validation; the message goes back to Claude."""


class _UnparseableToolInput(Exception):
    """The SDK could not parse a streamed tool input at all (eager input streaming)."""


# --------------------------------------------------------------------------- tool schemas

_WEEKDAY_SCHEMA: dict[str, Any] = {"type": "integer", "enum": [0, 1, 2, 3, 4, 5, 6]}

CANDIDATES_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["candidates", "notes"],
    "properties": {
        "candidates": {
            "type": "array",
            "description": "All researched places: sights/activities first, then restaurants and cafés.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "name", "name_local", "category", "styles", "area", "lat", "lon", "visit_minutes",
                    "opening_hours", "opening_hours_text", "closed_weekdays", "closed_on_holidays",
                    "holiday_shift", "best_time", "is_outdoor", "fee", "fee_currency", "rating",
                    "rating_source", "cuisine", "price_level", "description", "booking_required",
                    "source_urls",
                ],
                "properties": {
                    "name": {"type": "string", "description": "Name in the requested output language."},
                    "name_local": {"type": "string",
                                   "description": "Name in the local language/script; \"\" if identical."},
                    "category": {"type": "string", "enum": list(CATEGORIES)},
                    "styles": {"type": "array", "items": {"type": "string", "enum": list(STYLE_KEYS)},
                               "description": "Travel styles this place serves."},
                    "area": {"type": "string", "description": "Neighbourhood or district."},
                    "lat": {"type": "number", "description": "Approximate latitude; 0 if unknown."},
                    "lon": {"type": "number", "description": "Approximate longitude; 0 if unknown."},
                    "visit_minutes": {"type": "integer", "description": "Typical time on site in minutes."},
                    "opening_hours": {
                        "type": "array",
                        "description": "One entry per open weekday and period; [] if unknown.",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["weekday", "open", "close"],
                            "properties": {
                                "weekday": _WEEKDAY_SCHEMA,
                                "open": {"type": "string", "description": "HH:MM, 24-hour local time."},
                                "close": {"type": "string", "description": "HH:MM, 24-hour local time."},
                            },
                        },
                    },
                    "opening_hours_text": {"type": "string",
                                           "description": "Hours as written by the source; \"\" if unknown."},
                    "closed_weekdays": {"type": "array", "items": _WEEKDAY_SCHEMA},
                    "closed_on_holidays": {"type": "boolean"},
                    "holiday_shift": {"type": "boolean"},
                    "best_time": {"type": "string", "enum": list(BEST_TIMES)},
                    "is_outdoor": {"type": "boolean"},
                    "fee": {"type": "number",
                            "description": "Per person in fee_currency: admission, or typical meal spend "
                                           "for restaurants; 0 = free, -1 = unknown."},
                    "fee_currency": {"type": "string", "description": "ISO 4217 code."},
                    "rating": {"type": "number", "description": "As shown by rating_source; -1 if unknown."},
                    "rating_source": {"type": "string"},
                    "cuisine": {"type": "string", "description": "Restaurants/cafés only, e.g. \"sushi\"."},
                    "price_level": {"type": "integer", "enum": [-1, 0, 1, 2, 3, 4]},
                    "description": {"type": "string"},
                    "booking_required": {"type": "boolean"},
                    "source_urls": {"type": "array", "items": {"type": "string", "format": "uri"}},
                },
            },
        },
        "notes": {"type": "string", "description": "Important notes about the trip period; \"\" if none."},
    },
}

SUBMIT_CANDIDATES_TOOL: dict[str, Any] = {
    "name": "submit_candidates",
    "description": (
        "Submit the researched places for the trip. Call this exactly once, at the end of your "
        "research, with every place and the verified facts about it."
    ),
    "strict": True,
    "eager_input_streaming": True,
    "input_schema": CANDIDATES_SCHEMA,
}

ITINERARY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["title", "summary", "days", "warnings"],
    "properties": {
        "title": {"type": "string"},
        "summary": {"type": "string"},
        "days": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["date", "theme", "items"],
                "properties": {
                    "date": {"type": "string", "format": "date"},
                    "theme": {"type": "string"},
                    "items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["start", "end", "kind", "title", "candidate_id", "area",
                                         "description", "tips", "est_cost"],
                            "properties": {
                                "start": {"type": "string", "description": "HH:MM local time."},
                                "end": {"type": "string", "description": "HH:MM local time."},
                                "kind": {"type": "string", "enum": [
                                    "arrival", "transit", "hotel", "activity", "meal", "free", "departure",
                                ]},
                                "title": {"type": "string"},
                                "candidate_id": {"type": "string",
                                                 "description": "Id from the candidate list, or \"\"."},
                                "area": {"type": "string"},
                                "description": {"type": "string"},
                                "tips": {"type": "string"},
                                "est_cost": {"type": "number",
                                             "description": "Per person in the local currency; -1 if unknown."},
                            },
                        },
                    },
                },
            },
        },
        "warnings": {"type": "array", "items": {"type": "string"}},
    },
}

SUBMIT_ITINERARY_TOOL: dict[str, Any] = {
    "name": "submit_itinerary",
    "description": (
        "Submit the final day-by-day itinerary. Call this exactly once with the complete plan "
        "(every day of the draft, in order)."
    ),
    "strict": True,
    "eager_input_streaming": True,
    "input_schema": ITINERARY_SCHEMA,
}

# --------------------------------------------------------------------------- prompts (static)

RESEARCH_SYSTEM = """You are the research assistant of a personal trip planner. You find real, currently operating places for one trip and report verified facts about them.

How to work:
1. Use the web_search tool to research the destination for this traveller: well-known, well-reviewed sights, activities and neighbourhoods that match their travel style, and restaurants or cafés that match their food wishes and restaurant budget. Prefer official sites and reputable guides for opening hours, closing days and admission fees, and well-known review platforms for ratings. Be efficient: combine topics in one query and stop searching once you have enough.
2. Finish by calling the submit_candidates tool exactly once with everything you found. Do not answer in plain text; the tool call is your answer.

Rules for submit_candidates:
- 15-30 sights or activities (any category except restaurant and cafe) and 8-15 restaurants or cafés. Include every must-visit place the traveller names (when it exists), never include places they want to avoid, and skip foods they avoid.
- Spread the places over the city's main areas so a day can be spent in one area, and keep them within about 30 km of the city centre.
- Report only facts you saw in search results and use the sentinel for anything unknown: -1 for numbers, "" for text, [] for lists. Never guess opening hours.
- name: the name in the requested output language; name_local: the name in the local language and script ("" if identical).
- area: the neighbourhood or district (for example "Asakusa"), spelled the same way for every place in it.
- Weekdays are numbers: 0 = Monday ... 6 = Sunday. opening_hours: one entry per open weekday and period, "HH:MM" 24-hour local time; a period that ends after midnight closes at, for example, "02:00". closed_weekdays: the regular closing days. holiday_shift: true when a regular closing day that falls on a public holiday moves to the next weekday (typical for Japanese museums). closed_on_holidays: closed on every public holiday.
- fee: the admission fee per person in fee_currency (the local currency unless the source states another), 0 for free entry; for restaurants and cafés the typical spend per person for one meal; -1 if unknown.
- rating: the rating as shown by its source and rating_source naming that source (for example "google", "tabelog", "tripadvisor"); -1 and "" when unknown.
- price_level: 0 free ... 4 very expensive, -1 if unknown.
- lat and lon: approximate coordinates when you know them, otherwise 0 and 0. visit_minutes: the typical time on site.
- best_time: when the place is best visited; is_outdoor: true when the visit is mostly outdoors and depends on the weather; booking_required: true when tickets or reservations are needed in advance.
- cuisine: for restaurants and cafés the main cuisine in English (for example "sushi", "ramen", "korean_bbq"), otherwise "".
- source_urls: up to 3 pages from your search results that support the facts. Never invent URLs.
- description: one or two sentences in the output language on why the place suits this traveller.
- notes: anything important about the trip period (closures, renovations, seasonal events) in the output language, or ""."""

COMPOSE_SYSTEM = """You are the itinerary writer of a personal trip planner. You receive a trip brief, facts about candidate places and an itinerary that route-optimisation code built from those facts. You return the final day-by-day itinerary by calling the submit_itinerary tool exactly once. Do not answer in plain text; the tool call is your answer.

Rules:
- Start from the given itinerary. Keep each day's geographic grouping: a day stays in its areas. Move a stop to another day only when that day covers the same area, and never add a stop far from the rest of its day.
- Keep the arrival, the airport transfers, the hotel items and the departure exactly as given (same kind and times). Schedule nothing before the arrival or after the departure day's hotel pick-up.
- Use only places from the candidate list and reference each by its candidate_id. Items that are not a specific candidate (arrival, transit, hotel, free time, a generic meal suggestion) use candidate_id "".
- Keep times realistic: allow at least the travel minutes shown between places (more when you reorder), respect opening hours, closing days and public holidays, give each visit its typical duration, have lunch around 11:30-13:30 and dinner around 18:00-20:00, and follow the traveller's pace. Prefer indoor places on days with likely rain.
- Respect the budget: restaurant spend per person within the restaurant budget, and no paid extras the category budgets cannot cover.
- Write the title, summary, day themes, descriptions and tips in the requested output language. description: one or two sentences on what to do there. tips: practical advice (tickets, reservations, what to order, when it is less crowded) or "".
- est_cost: per person in the local currency, -1 if unknown. area: the neighbourhood of the item, "" if none.
- Never write URLs, links or coordinates; the application adds maps and links itself.
- warnings: only important caveats the traveller must know (for example a must-visit place that could not be fitted), in the output language; [] if none.
- Times are "HH:MM" 24-hour local time. Return one entry per day of the given itinerary, with the same dates, in order."""

NUDGE_TEXT = (
    "Please call the {tool} tool now with your complete result. Use what you already have; "
    "do not answer in plain text."
)


# --------------------------------------------------------------------------- LLM-facing input models


def _valid_hhmm(value: Any) -> str:
    """``"9:05"`` -> ``"09:05"``; raises ``ValueError`` for anything that is not HH:MM."""
    if not isinstance(value, str):
        raise ValueError("time must be a string HH:MM")
    minutes = hhmm_to_minutes(value)
    return "24:00" if minutes == 24 * 60 else minutes_to_hhmm(minutes)


class _HoursIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    weekday: int = Field(ge=0, le=6)
    open: str
    close: str

    @field_validator("open", "close", mode="before")
    @classmethod
    def _times(cls, value: Any) -> str:
        return _valid_hhmm(value)


class _CandidateIn(BaseModel):
    """One ``submit_candidates`` entry (sentinels: -1 / "" / [] = unknown)."""

    model_config = ConfigDict(extra="ignore")

    name: str = Field(min_length=1, max_length=300)
    name_local: str = ""
    category: str = "attraction"
    styles: list[str] = Field(default_factory=list)
    area: str = ""
    lat: float = Field(0.0, ge=-90, le=90)
    lon: float = Field(0.0, ge=-180, le=180)
    visit_minutes: int = 0
    opening_hours: list[_HoursIn] = Field(default_factory=list)
    opening_hours_text: str = ""
    closed_weekdays: list[int] = Field(default_factory=list)
    closed_on_holidays: bool = False
    holiday_shift: bool = False
    best_time: str = "any"
    is_outdoor: bool = False
    fee: float = -1
    fee_currency: str = ""
    rating: float = -1
    rating_source: str = ""
    cuisine: str = ""
    price_level: int = -1
    description: str = ""
    booking_required: bool = False
    source_urls: list[str] = Field(default_factory=list)

    @field_validator("name", mode="before")
    @classmethod
    def _strip(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value

    @field_validator("closed_weekdays")
    @classmethod
    def _weekdays(cls, value: list[int]) -> list[int]:
        bad = [v for v in value if not 0 <= v <= 6]
        if bad:
            raise ValueError(f"weekdays must be 0 (Monday) to 6 (Sunday), got {bad}")
        return value


class _CandidatesIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    candidates: list[_CandidateIn]
    notes: str = ""


class _ItemIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    start: str
    end: str
    kind: PlanItemKind
    title: str = ""
    candidate_id: str = ""
    area: str = ""
    description: str = ""
    tips: str = ""
    est_cost: float = -1

    @field_validator("start", "end", mode="before")
    @classmethod
    def _times(cls, value: Any) -> str:
        return _valid_hhmm(value)

    @field_validator("kind", mode="before")
    @classmethod
    def _lower(cls, value: Any) -> Any:
        return value.strip().lower() if isinstance(value, str) else value


class _DayIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    date: date
    theme: str = ""
    items: list[_ItemIn]


class _ItineraryIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    title: str = Field(min_length=1)
    summary: str = ""
    days: list[_DayIn] = Field(min_length=1)
    warnings: list[str] = Field(default_factory=list)


def _describe_validation_error(exc: ValidationError, limit: int = 10) -> str:
    """Compact, model-readable list of Pydantic errors."""
    lines = []
    for error in exc.errors()[:limit]:
        where = ".".join(str(part) for part in error.get("loc", ())) or "input"
        lines.append(f"- {where}: {error.get('msg', 'invalid value')}")
    more = len(exc.errors()) - limit
    if more > 0:
        lines.append(f"- … and {more} more problem(s)")
    return "\n".join(lines)


# --------------------------------------------------------------------------- source URLs


def _url_key(url: str) -> str:
    """Comparison key: scheme-less, lower-case host without ``www.``, no fragment, no
    trailing slash (``https://www.a.jp/x/`` == ``http://a.jp/x``)."""
    parts = urlsplit(url.strip())
    host = parts.netloc.lower().removeprefix("www.")
    path = parts.path.rstrip("/")
    return f"{host}{path}" + (f"?{parts.query}" if parts.query else "")


def harvest_source_urls(content: Iterable[Any]) -> list[str]:
    """URLs that appeared in ``web_search_tool_result`` blocks (result lists, not errors)
    and in ``web_search_result_location`` citations of text blocks."""
    urls: list[str] = []
    for block in content:
        kind = getattr(block, "type", None)
        if kind == "web_search_tool_result":
            results = getattr(block, "content", None)
            if isinstance(results, list):  # a dict-like object means a search error
                urls.extend(u for r in results if isinstance(u := getattr(r, "url", None), str))
        elif kind == "text":
            for citation in getattr(block, "citations", None) or []:
                url = getattr(citation, "url", None)
                if isinstance(url, str):
                    urls.append(url)
    return urls


class _SeenUrls:
    """URLs seen in a conversation, matched by :func:`_url_key`."""

    def __init__(self) -> None:
        self._by_key: dict[str, str] = {}

    def add(self, urls: Iterable[str]) -> None:
        for url in urls:
            if url.startswith(("http://", "https://")):
                self._by_key.setdefault(_url_key(url), url)

    def verified(self, urls: Iterable[str], limit: int = MAX_SOURCE_URLS) -> list[str]:
        """The seen form of each URL in ``urls`` that was seen (order kept, no duplicates)."""
        kept: list[str] = []
        for url in urls:
            seen = self._by_key.get(_url_key(url)) if isinstance(url, str) else None
            if seen and seen not in kept:
                kept.append(seen)
        return kept[:limit]

    def __len__(self) -> int:
        return len(self._by_key)


# --------------------------------------------------------------------------- history helpers

_PAIRED_RESULT_SUFFIX = "_tool_result"
_ECHO_BEFORE_FALLBACK = frozenset({"text", "server_tool_use", "fallback"})


def _history_content(content: Sequence[Any]) -> list[Any]:
    """Assistant content to append to the history: unchanged, except that after a
    mid-output server-side fallback the blocks before the last ``fallback`` block lose
    ``thinking`` / ``redacted_thinking`` / ``tool_use`` blocks, ``server_tool_use`` blocks
    without their result and unknown internal blocks (the documented echo rule)."""
    blocks = list(content)
    boundary = max((i for i, b in enumerate(blocks) if getattr(b, "type", None) == "fallback"), default=None)
    if boundary is None:
        return blocks
    answered = {getattr(b, "tool_use_id", None) for b in blocks
                if str(getattr(b, "type", "")).endswith(_PAIRED_RESULT_SUFFIX)}
    kept: list[Any] = []
    for index, block in enumerate(blocks):
        kind = str(getattr(block, "type", ""))
        if index < boundary:
            if kind == "server_tool_use" and getattr(block, "id", None) not in answered:
                continue
            if kind not in _ECHO_BEFORE_FALLBACK and not kind.endswith(_PAIRED_RESULT_SUFFIX):
                continue
        kept.append(block)
    return kept


def _tool_result(tool_use_id: str, text: str, *, is_error: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {"type": "tool_result", "tool_use_id": tool_use_id, "content": text}
    if is_error:
        result["is_error"] = True
    return result


def _dumps(data: Any) -> str:
    """Deterministic compact JSON for prompts (stable bytes help prompt caching)."""
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


# --------------------------------------------------------------------------- prompt payloads


def _language_name(language: str) -> str:
    return _LANGUAGE_NAMES.get(language, language)


def _money(value: float | None) -> float | None:
    return None if value is None else round(float(value), 2)


def _budget_text(value: float | None) -> str | float:
    if value is None:
        return "not specified"
    amount, unlimited = budget_mod.budget_value(value)
    return "unlimited" if unlimited else (amount if amount is not None else "not specified")


def _trip_brief(inputs: PlanInputs, holidays: Mapping[date, str]) -> dict[str, Any]:
    """Trip facts for prompts: destination, stay, travellers and their own preferences.

    Deliberately excludes the flight price and any other flight-provider data (only the
    arrival/departure times and airports of the chosen flight)."""
    weights = sorted(
        ((k, float(v)) for k, v in inputs.style_weights.items() if k in STYLE_KEYS),
        key=lambda kv: (-kv[1], kv[0]),
    )
    scale = "percent (0-100)" if inputs.style_mode == "percent" else "score (0-5)"
    restaurant_ccy = inputs.restaurant_budget_currency or inputs.currency
    stay = set(inputs.dates)
    return {
        "destination": {"city": inputs.city, "country": inputs.country, "airport_or_metro": inputs.destination,
                        "local_currency": inputs.currency, "timezone": inputs.timezone},
        "arrival": {"local_time": inputs.arrival_at.strftime("%Y-%m-%d %H:%M (%a)"),
                    "airport": inputs.arrival_airport or inputs.destination},
        "departure": {"local_time": inputs.departure_at.strftime("%Y-%m-%d %H:%M (%a)"),
                      "airport": inputs.departure_airport or inputs.destination},
        "dates": [f"{d.isoformat()} ({_WEEKDAY_NAMES[d.weekday()]})" for d in inputs.dates],
        "public_holidays": {d.isoformat(): name for d, name in sorted(holidays.items()) if d in stay},
        "travellers": {"adults": inputs.adults, "companion": inputs.companion, "pace": inputs.pace},
        "travel_style": {"scale": scale, "weights": {k: v for k, v in weights}},
        "food": {
            "wishlist": list(inputs.food_wishlist),
            "preferred_cuisines": list(inputs.preferred_cuisines),
            "avoid": list(inputs.avoid_foods),
            "restaurant_budget_per_person_per_meal": {
                "min": _money(inputs.restaurant_budget_min), "max": _money(inputs.restaurant_budget_max),
                "currency": restaurant_ccy,
            },
        },
        "must_visit": list(inputs.must_visit),
        "avoid_places": list(inputs.avoid),
        "hotel_area": inputs.hotel_area,
        "budget": {
            "basis": inputs.budget_basis, "currency": inputs.budget_currency,
            "food": _budget_text(inputs.budget_food), "activities": _budget_text(inputs.budget_activities),
            "transport": _budget_text(inputs.budget_transport), "shopping": _budget_text(inputs.budget_shopping),
            "other": _budget_text(inputs.budget_other),
        },
        "traveller_notes": inputs.notes,
        "output_language": _language_name(inputs.language),
    }


def _hours_summary(candidate: Candidate) -> str:
    """``"Mon-Fri 09:00-17:00; Sat,Sun 10:00-18:00"`` (or the source text / ``"unknown"``)."""
    if not candidate.opening_hours:
        return candidate.opening_hours_text or "unknown"
    per_day: dict[int, str] = {}
    for weekday in range(7):
        spans = sorted(f"{p.open}-{p.close}" for p in candidate.opening_hours if p.weekday == weekday)
        if spans:
            per_day[weekday] = ",".join(spans)
    groups: list[tuple[list[int], str]] = []
    for weekday, text in per_day.items():
        if groups and groups[-1][1] == text and groups[-1][0][-1] == weekday - 1:
            groups[-1][0].append(weekday)
        else:
            groups.append(([weekday], text))
    parts = []
    for days, text in groups:
        label = (_WEEKDAY_NAMES[days[0]] if len(days) == 1
                 else f"{_WEEKDAY_NAMES[days[0]]}-{_WEEKDAY_NAMES[days[-1]]}")
        parts.append(f"{label} {text}")
    return "; ".join(parts)


def _candidate_fact(candidate: Candidate) -> dict[str, Any]:
    """Facts of one candidate for the compose prompt (no URLs or coordinates)."""
    fact: dict[str, Any] = {
        "id": candidate.id,
        "name": candidate.name,
        "category": candidate.category,
        "area": candidate.area or "",
        "hours": _hours_summary(candidate),
        "visit_minutes": candidate.visit_minutes,
        "best_time": candidate.best_time,
        "outdoor": candidate.is_outdoor,
    }
    if candidate.name_local:
        fact["name_local"] = candidate.name_local
    if candidate.closed_weekdays:
        fact["closed"] = [_WEEKDAY_NAMES[d] for d in candidate.closed_weekdays]
    if candidate.closed_on_holidays:
        fact["closed_on_holidays"] = True
    if candidate.holiday_shift:
        fact["holiday_shift"] = True
    if candidate.est_cost is not None:
        fact["cost"] = f"{candidate.est_cost:g} {candidate.cost_currency or ''}".strip()
    if candidate.rating is not None:
        fact["rating"] = f"{candidate.rating:g} ({candidate.rating_source or '?'})"
    if candidate.cuisine:
        fact["cuisine"] = candidate.cuisine
    if candidate.booking_required:
        fact["booking_required"] = True
    if candidate.description:
        fact["description"] = candidate.description[:DESCRIPTION_CHARS]
    return fact


def _plan_payload(plan: ItineraryPlan, holidays: Mapping[date, str]) -> list[dict[str, Any]]:
    """The plan's days in the LLM-facing item format (with the computed travel minutes)."""
    days = []
    for day in plan.days:
        weather = day.weather or {}
        info: dict[str, Any] = {
            "date": day.date.isoformat(),
            "weekday": _WEEKDAY_NAMES[day.date.weekday()],
            "theme": day.theme,
        }
        if day.date in holidays:
            info["public_holiday"] = holidays[day.date]
        if weather:
            info["weather"] = {
                "summary": weather.get("summary"),
                "temp_c": [weather.get("temp_min_c"), weather.get("temp_max_c")],
                "rain_likely": weather_mod.is_wet(weather),
            }
        items = []
        for item in day.items:
            entry: dict[str, Any] = {
                "start": item.start, "end": item.end, "kind": item.kind, "title": item.title,
                "candidate_id": item.candidate_id or "", "area": item.area or "",
            }
            if item.travel_from_prev is not None:
                entry["travel_from_previous"] = (f"{item.travel_from_prev.mode} "
                                                 f"{int(item.travel_from_prev.minutes)} min")
            if item.est_cost is not None:
                entry["est_cost"] = f"{item.est_cost:g} {item.cost_currency or plan.currency_local}"
            if item.description:
                entry["description"] = item.description[:DESCRIPTION_CHARS]
            items.append(entry)
        info["items"] = items
        days.append(info)
    return days


def _excluded(candidate: Candidate, inputs: PlanInputs) -> bool:
    """Places/foods the traveller wants to avoid (never offered to the composer)."""
    if any(name_matches(term, candidate) for term in inputs.avoid):
        return True
    if candidate.is_food and inputs.avoid_foods:
        avoid = {k for term in inputs.avoid_foods if (k := canonical_cuisine(term))}
        return bool(avoid & set(candidate.cuisines))
    return False


def _compose_candidates(
    plan: ItineraryPlan, candidates: Sequence[Candidate], inputs: PlanInputs
) -> list[Candidate]:
    """Candidates offered to the composer: the plan's, its unscheduled alternates, then the
    rest (sights before food), avoided ones excluded, at most :data:`MAX_COMPOSE_CANDIDATES`."""
    by_id = {c.id: c for c in candidates}
    order: list[str] = [i.candidate_id for d in plan.days for i in d.items if i.candidate_id]
    unscheduled = getattr(plan, "unscheduled", None) or []
    order += [str(u.get("candidate_id")) for u in unscheduled if isinstance(u, dict) and u.get("candidate_id")]
    order += [c.id for c in candidates if not c.is_food] + [c.id for c in candidates if c.is_food]
    chosen: list[Candidate] = []
    for cid in dict.fromkeys(order):
        candidate = by_id.get(cid)
        if candidate is not None and not _excluded(candidate, inputs):
            chosen.append(candidate)
        if len(chosen) >= MAX_COMPOSE_CANDIDATES:
            break
    return chosen


# --------------------------------------------------------------------------- plan conversion


def _meal_key(item: PlanItem) -> str:
    """``breakfast`` / ``lunch`` / ``dinner`` from the item's ``meal`` extra or its start."""
    meal = getattr(item, "meal", None)
    if meal in ("breakfast", "lunch", "dinner"):
        return str(meal)
    try:
        start = hhmm_to_minutes(item.start)
    except ValueError:
        return "lunch"
    return "breakfast" if start < 630 else ("lunch" if start < 960 else "dinner")


def _candidate_extras(candidate: Candidate, day: date, holidays: Mapping[date, str]) -> dict[str, Any]:
    """Display facts of a candidate-backed item (same keys as the rule-based planner's)."""
    extras: dict[str, Any] = {
        "category": candidate.category,
        "name_local": candidate.name_local,
        "hours": calendar_util.hours_label(candidate, day, holidays) or candidate.opening_hours_text,
        "hours_verified": candidate.hours_known or candidate.sources_verified,
        "rating": candidate.rating,
        "rating_source": candidate.rating_source,
        "booking_required": candidate.booking_required,
        "is_outdoor": candidate.is_outdoor,
    }
    if candidate.is_food:
        cuisines = candidate.cuisines
        extras["cuisine"] = cuisines[0] if cuisines else None
    return extras


@dataclass
class _BaseIndex:
    """Items of the base (draft / current) plan for matching LLM items back to them."""

    by_day: dict[date, list[PlanItem]]
    used: set[int] = field(default_factory=set)

    @classmethod
    def of(cls, plan: ItineraryPlan) -> _BaseIndex:
        return cls({d.date: list(d.items) for d in plan.days})

    def match(self, day: date, item: _ItemIn) -> PlanItem | None:
        """Same candidate (same day first, then any day), else the same kind on that day
        with the closest start time; each base item matches once."""
        pools = [self.by_day.get(day, [])] + [items for d, items in self.by_day.items() if d != day]
        if item.candidate_id:
            for pool in pools:
                for base in pool:
                    if base.candidate_id == item.candidate_id and id(base) not in self.used:
                        self.used.add(id(base))
                        return base
        start = hhmm_to_minutes(item.start)
        same_kind = [b for b in self.by_day.get(day, []) if b.kind == item.kind and id(b) not in self.used]
        if not same_kind:
            return None

        def gap(base: PlanItem) -> int:
            try:
                return abs(hhmm_to_minutes(base.start) - start)
            except ValueError:
                return 24 * 60

        best = min(same_kind, key=gap)
        self.used.add(id(best))
        return best


def _item_cost(
    item: _ItemIn, base: PlanItem | None, candidate: Candidate | None, same_place: bool, inputs: PlanInputs
) -> tuple[float | None, str | None, bool | None]:
    """``(est_cost, cost_currency, cost_estimated)``: the base plan's code-computed cost for
    the same place, else the candidate's fact, else Claude's estimate (local currency)."""
    if base is not None and same_place and base.est_cost is not None:
        estimated = getattr(base, "cost_estimated", None)
        return base.est_cost, base.cost_currency, estimated if isinstance(estimated, bool) else None
    if candidate is not None and candidate.est_cost is not None:
        return candidate.est_cost, candidate.cost_currency or inputs.currency, False
    if item.est_cost >= 0:
        return round(item.est_cost, 2), inputs.currency, True
    return None, None, None


def _convert_item(
    item: _ItemIn, day: date, base: PlanItem | None, candidate: Candidate | None,
    inputs: PlanInputs, holidays: Mapping[date, str],
) -> PlanItem:
    """One LLM item -> :class:`PlanItem`. Flight times are code-owned: arrival/departure
    items keep the base plan's times. Coordinates/links are added later by finalize; a base
    place whose facts are unavailable keeps its id and (approximate) base coordinates."""
    cid = candidate.id if candidate is not None else None
    if cid is None and item.candidate_id and base is not None and base.candidate_id == item.candidate_id:
        cid = item.candidate_id
    same_place = base is not None and (base.candidate_id or None) == cid
    extras: dict[str, Any] = {}
    if base is not None:
        source = base.model_extra or {}
        keep = source.keys() if same_place else {"meal"} & source.keys()
        extras.update({k: source[k] for k in keep if k not in _CODE_BUILT_EXTRAS})
    if candidate is not None:
        extras.update(_candidate_extras(candidate, day, holidays))
    start, end = item.start, item.end
    if base is not None and item.kind in ("arrival", "departure") and base.kind == item.kind:
        start, end = base.start, base.end
    cost, currency, estimated = _item_cost(item, base, candidate, same_place, inputs)
    if estimated is not None:
        extras["cost_estimated"] = estimated
    title = item.title.strip() or (candidate.name if candidate else (base.title if base else item.kind))
    keep_point = candidate is None and cid is not None and base is not None
    plan_item = PlanItem(
        start=start, end=end, kind=item.kind, title=title,
        area=item.area.strip() or (candidate.area if candidate else (base.area if base else None)),
        lat=base.lat if keep_point else None, lng=base.lng if keep_point else None,  # type: ignore[union-attr]
        description=item.description.strip() or None, tips=item.tips.strip() or None,
        est_cost=cost, cost_currency=currency, candidate_id=cid,
        **extras,
    )
    if plan_item.kind == "meal" and getattr(plan_item, "meal", None) is None:
        plan_item.meal = _meal_key(plan_item)  # type: ignore[attr-defined]
    return plan_item


def _check_days(submitted: _ItineraryIn, base: ItineraryPlan) -> None:
    """Dates must be exactly the base plan's (each once)."""
    wanted = [d.date for d in base.days]
    got = [d.date for d in submitted.days]
    unknown = sorted({d.isoformat() for d in got if d not in wanted})
    if unknown:
        raise _Rejected(f"days: {', '.join(unknown)} not part of the trip; use exactly these dates: "
                        f"{', '.join(d.isoformat() for d in wanted)}.")
    duplicates = sorted({d.isoformat() for d in got if got.count(d) > 1})
    if duplicates:
        raise _Rejected(f"days: each date must appear once ({', '.join(duplicates)} repeated).")
    missing = [d.isoformat() for d in wanted if d not in got]
    if missing:
        raise _Rejected(f"days: missing {', '.join(missing)}; return every day of the itinerary.")


def _check_items(submitted: _ItineraryIn, base: ItineraryPlan, known: Mapping[str, Candidate]) -> None:
    """Candidate ids must be known (offered, or already in the base plan); the
    arrival/departure must stay; no empty planned day."""
    in_base = {i.candidate_id for d in base.days for i in d.items if i.candidate_id}
    unknown = sorted({i.candidate_id for d in submitted.days for i in d.items
                      if i.candidate_id and i.candidate_id not in known and i.candidate_id not in in_base})
    if unknown:
        raise _Rejected(f"unknown candidate_id(s): {', '.join(unknown[:10])}. Use ids from the candidate "
                        "list exactly, or \"\" for items that are not a listed place.")
    base_kinds = {i.kind for d in base.days for i in d.items}
    got_kinds = {i.kind for d in submitted.days for i in d.items}
    for kind in ("arrival", "departure"):
        if kind in base_kinds and kind not in got_kinds:
            raise _Rejected(f"the {kind} item is missing; keep it exactly as in the given itinerary.")
    planned = {d.date for d in base.days if d.items}
    empty = [d.date.isoformat() for d in submitted.days if d.date in planned and not d.items]
    if empty:
        raise _Rejected(f"days without items: {', '.join(empty)}.")


def _unscheduled(plan: ItineraryPlan, offered: Sequence[Candidate]) -> list[dict[str, Any]]:
    used = {i.candidate_id for d in plan.days for i in d.items if i.candidate_id}
    return [{"candidate_id": c.id, "name": c.name, "category": c.category}
            for c in offered if c.id not in used and not c.is_food][:15]


def _convert_plan(
    submitted: _ItineraryIn,
    base: ItineraryPlan,
    inputs: PlanInputs,
    known: Mapping[str, Candidate],
    offered: Sequence[Candidate],
    holidays: Mapping[date, str],
) -> ItineraryPlan:
    """The submitted itinerary as an (unfinalized) :class:`ItineraryPlan` built on ``base``:
    day weather/holiday and plan extras (hotel, layover, attributions, ...) come from the
    base plan; raises :class:`_Rejected` for problems Claude must fix."""
    _check_days(submitted, base)
    _check_items(submitted, base, known)
    index = _BaseIndex.of(base)
    base_days = {d.date: d for d in base.days}
    days: list[PlanDay] = []
    for day_in in sorted(submitted.days, key=lambda d: d.date):
        base_day = base_days[day_in.date]
        items = [
            _convert_item(item, day_in.date, index.match(day_in.date, item),
                          known.get(item.candidate_id) if item.candidate_id else None, inputs, holidays)
            for item in day_in.items
        ]
        areas = list(dict.fromkeys(i.area for i in items if i.kind in ("activity", "meal") and i.area))
        plan_day = PlanDay(date=day_in.date, theme=day_in.theme.strip() or base_day.theme, areas=areas,
                           items=items, weather=base_day.weather)
        for key, value in (base_day.model_extra or {}).items():
            setattr(plan_day, key, value)
        days.append(plan_day)
    extras = {k: v for k, v in (base.model_extra or {}).items() if k not in ("unscheduled", "llm_warnings")}
    plan = ItineraryPlan(
        title=submitted.title.strip(), summary=submitted.summary.strip(), destination=base.destination,
        currency_local=base.currency_local, days=days, generator="claude", **extras,
    )
    plan.unscheduled = _unscheduled(plan, offered)  # type: ignore[attr-defined]
    plan.llm_warnings = [w.strip() for w in submitted.warnings if w.strip()]  # type: ignore[attr-defined]
    return plan


def _over_budget_warning(plan: ItineraryPlan, language: str) -> str | None:
    if plan.budget is None:
        return None
    over = budget_mod.over_budget_lines(plan.budget)
    if not over:
        return None
    texts = PLANNER_TEXTS.get(language, PLANNER_TEXTS["en"])
    return texts["w_over_budget"].format(lines=", ".join(over))


def _carried_warnings(
    base: ItineraryPlan, inputs: PlanInputs, candidates: Sequence[Candidate],
    holidays: Mapping[date, str], weather: Mapping[date, dict[str, Any]],
) -> list[str]:
    """Base-plan warnings that are not about its own timing/route/budget (those are
    recomputed for the new plan) nor an earlier composer's notes: data warnings such as
    unverified hours, assumed hotel area or service notices."""
    recomputed = set(check_plan(base, inputs, candidates, holidays=holidays, weather=weather))
    over = _over_budget_warning(base, inputs.language)
    if over:
        recomputed.add(over)
    previous_llm = set(getattr(base, "llm_warnings", None) or [])
    return [w for w in base.warnings if w not in recomputed and w not in previous_llm]


# --------------------------------------------------------------------------- the client wrapper

T = TypeVar("T")


@dataclass
class _LoopOutcome(Generic[T]):
    value: T
    usage: LLMUsage
    seen: _SeenUrls


class ItineraryLLM:
    """Claude calls of the itinerary pipeline (see module docstring).

    ``client`` defaults to ``anthropic.Anthropic(api_key=api_key)`` (``api_key=None`` lets
    the SDK read ``ANTHROPIC_API_KEY``). ``model``/``effort`` come from the runtime settings;
    ``language`` (``"en"``/``"ko"``) is used for error messages (plan text follows
    ``PlanInputs.language``).
    """

    def __init__(
        self,
        client: anthropic.Anthropic | None = None,
        model: str = DEFAULT_MODEL,
        effort: str = DEFAULT_EFFORT,
        language: str = "en",
        *,
        api_key: str | None = None,
    ) -> None:
        self.client = client if client is not None else anthropic.Anthropic(api_key=api_key)
        self.model = (model or DEFAULT_MODEL).strip()
        self.effort = effort or DEFAULT_EFFORT
        self.language = language if language in ("en", "ko") else "en"

    # ------------------------------------------------------------------ public API

    def research_candidates(self, inputs: PlanInputs) -> tuple[list[Candidate], LLMUsage]:
        """Research places with web search and return ``(candidates, usage)``.

        Candidates carry ``coord_source="llm"`` coordinates (geocode them afterwards) and only
        source URLs seen in this conversation's search results/citations
        (``sources_verified`` = at least one kept). Raises :class:`ItineraryLLMError`."""
        holidays = calendar_util.holidays_for_dates(inputs.country, inputs.dates, inputs.language)
        prompt = self._research_prompt(inputs, holidays)
        try:
            outcome = self._research(inputs, prompt, include_country=True)
        except ItineraryLLMError as exc:
            if not (exc.kind == "bad_request" and exc.usage.requests == 0
                    and "user_location" in (exc.detail or "")):
                raise
            logger.warning("Web search rejected user_location country %r; retrying without it", inputs.country)
            outcome = self._research(inputs, prompt, include_country=False)
        submitted, usage, seen = outcome.value, outcome.usage, outcome.seen
        candidates = self._to_candidates(submitted, inputs, seen)
        logger.info("Claude research: %d candidates (%d verified), %d searches, %d URLs seen, ~$%.3f",
                    len(candidates), sum(c.sources_verified for c in candidates),
                    usage.web_search_requests, len(seen), usage.est_usd)
        return candidates, usage

    def compose_itinerary(
        self,
        inputs: PlanInputs,
        draft_plan: ItineraryPlan,
        candidates: Sequence[Candidate],
        *,
        weather: Mapping[date, dict[str, Any]] | None = None,
        holidays: Mapping[date, str] | None = None,
    ) -> tuple[ItineraryPlan, LLMUsage]:
        """Turn the optimised draft into the final plan (no web search).

        Returns a finalized plan (``generator="claude"``) with warnings from the route checker
        and ``usage``. Raises :class:`ItineraryLLMError`."""
        holidays = self._holidays(inputs, holidays)
        offered = _compose_candidates(draft_plan, candidates, inputs)
        payload = {
            "trip": _trip_brief(inputs, holidays),
            "candidates": [_candidate_fact(c) for c in offered],
            "draft_itinerary": _plan_payload(draft_plan, holidays),
        }
        text = (
            "Here are the trip brief, the candidate places and the draft itinerary built by the "
            "route optimiser (JSON). Write the final itinerary in "
            f"{_language_name(inputs.language)} and submit it with submit_itinerary.\n\n{_dumps(payload)}"
        )
        return self._write_plan(inputs, draft_plan, candidates, offered, text, weather, holidays, "compose")

    def refine_itinerary(
        self,
        inputs: PlanInputs,
        plan: ItineraryPlan,
        feedback: str,
        candidates: Sequence[Candidate],
        *,
        weather: Mapping[date, dict[str, Any]] | None = None,
        holidays: Mapping[date, str] | None = None,
    ) -> tuple[ItineraryPlan, LLMUsage]:
        """Revise ``plan`` according to the traveller's ``feedback`` (same rules as compose).

        Returns ``(plan, usage)``; raises :class:`ItineraryLLMError`."""
        holidays = self._holidays(inputs, holidays)
        offered = _compose_candidates(plan, candidates, inputs)
        payload = {
            "trip": _trip_brief(inputs, holidays),
            "candidates": [_candidate_fact(c) for c in offered],
            "current_itinerary": _plan_payload(plan, holidays),
        }
        text = (
            "The traveller wants changes to the current itinerary below. Apply their feedback while "
            "following all the rules (keep what they did not ask to change), write in "
            f"{_language_name(inputs.language)} and submit the complete revised itinerary with "
            "submit_itinerary.\n\n"
            f"Traveller feedback:\n<feedback>\n{feedback.strip()}\n</feedback>\n\n{_dumps(payload)}"
        )
        weather = weather if weather is not None else {d.date: d.weather for d in plan.days if d.weather}
        return self._write_plan(inputs, plan, candidates, offered, text, weather, holidays, "refine")

    # ------------------------------------------------------------------ research helpers

    def research_tools(self, inputs: PlanInputs, *, include_country: bool = True) -> list[dict[str, Any]]:
        """The research call's tools: web search (approximate user location) + submit_candidates."""
        location: dict[str, Any] = {"type": "approximate", "city": inputs.city}
        if include_country and inputs.country:
            location["country"] = inputs.country
        if inputs.timezone:
            location["timezone"] = inputs.timezone
        web_search: dict[str, Any] = {
            "type": WEB_SEARCH_TOOL_TYPE, "name": "web_search", "max_uses": WEB_SEARCH_MAX_USES,
            "user_location": location,
        }
        if "haiku" in self.model.lower():  # no dynamic filtering on Haiku
            web_search["allowed_callers"] = ["direct"]
        return [web_search, SUBMIT_CANDIDATES_TOOL]

    def _research_prompt(self, inputs: PlanInputs, holidays: Mapping[date, str]) -> str:
        return (
            "Research places for this trip (JSON brief below). Write names, descriptions and notes "
            f"in {_language_name(inputs.language)}, then call submit_candidates.\n\n"
            f"{_dumps(_trip_brief(inputs, holidays))}"
        )

    def _research(self, inputs: PlanInputs, prompt: str, *, include_country: bool) -> _LoopOutcome[_CandidatesIn]:
        messages: list[dict[str, Any]] = [{"role": "user", "content": [{"type": "text", "text": prompt}]}]
        return self._run_tool_loop(
            system=RESEARCH_SYSTEM,
            tools=self.research_tools(inputs, include_country=include_country),
            messages=messages,
            tool_name="submit_candidates",
            accept=self._accept_candidates,
            label="research",
        )

    @staticmethod
    def _accept_candidates(raw: Any, lenient: bool) -> _CandidatesIn:
        try:
            submitted = _CandidatesIn.model_validate(raw)
        except ValidationError as exc:
            problems = _describe_validation_error(exc)
            raise _Rejected(f"The submit_candidates input is invalid:\n{problems}") from exc
        sights = sum(1 for c in submitted.candidates if c.category.strip().lower() not in ("restaurant", "cafe"))
        food = len(submitted.candidates) - sights
        if not lenient and (sights < MIN_ATTRACTIONS or food < MIN_RESTAURANTS):
            raise _Rejected(
                f"Only {sights} sights/activities and {food} restaurants/cafés were submitted; research a "
                "few more (aim for 15-30 sights and 8-15 restaurants) and call submit_candidates again "
                "with the complete list."
            )
        if not submitted.candidates:
            raise _Rejected("No candidates were submitted.")
        return submitted

    @staticmethod
    def _to_candidates(submitted: _CandidatesIn, inputs: PlanInputs, seen: _SeenUrls) -> list[Candidate]:
        """LLM entries -> :class:`Candidate` (sentinels to ``None``, URLs verified, capped)."""
        sights: list[Candidate] = []
        food: list[Candidate] = []
        for entry in submitted.candidates:
            urls = seen.verified(entry.source_urls)
            has_point = not (entry.lat == 0 and entry.lon == 0)
            try:
                candidate = Candidate(
                    name=entry.name,
                    name_local=entry.name_local if entry.name_local != entry.name else None,
                    category=entry.category,
                    styles=entry.styles,
                    area=entry.area,
                    lat=entry.lat if has_point else None,
                    lon=entry.lon if has_point else None,
                    coord_source="llm" if has_point else "none",
                    visit_minutes=max(0, entry.visit_minutes),
                    opening_hours=[OpeningPeriod(weekday=h.weekday, open=h.open, close=h.close)
                                   for h in entry.opening_hours] or None,
                    opening_hours_text=entry.opening_hours_text,
                    closed_weekdays=entry.closed_weekdays,
                    closed_on_holidays=entry.closed_on_holidays,
                    holiday_shift=entry.holiday_shift,
                    best_time=entry.best_time,
                    is_outdoor=entry.is_outdoor,
                    est_cost=entry.fee if entry.fee >= 0 else None,
                    cost_currency=(entry.fee_currency or inputs.currency) if entry.fee >= 0 else None,
                    rating=entry.rating if entry.rating > 0 else None,
                    rating_source=entry.rating_source if entry.rating > 0 else None,
                    cuisine=entry.cuisine,
                    price_level=entry.price_level if 0 <= entry.price_level <= 4 else None,
                    description=entry.description,
                    booking_required=entry.booking_required,
                    source_urls=urls,
                    sources_verified=bool(urls),
                )
            except ValidationError as exc:
                logger.info("Dropping research entry %r: %s", entry.name, exc.errors()[:2])
                continue
            (food if candidate.is_food else sights).append(candidate)
        return merge_candidates(sights[:MAX_ATTRACTIONS], food[:MAX_RESTAURANTS])

    # ------------------------------------------------------------------ compose helpers

    @staticmethod
    def _holidays(inputs: PlanInputs, holidays: Mapping[date, str] | None) -> Mapping[date, str]:
        if holidays is not None:
            return holidays
        return calendar_util.holidays_for_dates(inputs.country, inputs.dates, inputs.language)

    def _write_plan(
        self,
        inputs: PlanInputs,
        base: ItineraryPlan,
        candidates: Sequence[Candidate],
        offered: Sequence[Candidate],
        text: str,
        weather: Mapping[date, dict[str, Any]] | None,
        holidays: Mapping[date, str],
        label: str,
    ) -> tuple[ItineraryPlan, LLMUsage]:
        """Run the compose/refine conversation and finalize the converted plan."""
        by_id = {c.id: c for c in candidates}
        known = {c.id: c for c in offered}
        for day in base.days:  # places already in the base plan stay referable
            for item in day.items:
                if item.candidate_id and item.candidate_id in by_id:
                    known.setdefault(item.candidate_id, by_id[item.candidate_id])
        weather = weather if weather is not None else {d.date: d.weather for d in base.days if d.weather}

        def accept(raw: Any, lenient: bool) -> ItineraryPlan:
            try:
                submitted = _ItineraryIn.model_validate(raw)
            except ValidationError as exc:
                problems = _describe_validation_error(exc)
                raise _Rejected(f"The submit_itinerary input is invalid:\n{problems}") from exc
            return _convert_plan(submitted, base, inputs, known, offered, holidays)

        messages: list[dict[str, Any]] = [{"role": "user", "content": [{"type": "text", "text": text}]}]
        outcome = self._run_tool_loop(
            system=COMPOSE_SYSTEM, tools=[SUBMIT_ITINERARY_TOOL], messages=messages,
            tool_name="submit_itinerary", accept=accept, label=label,
        )
        pool = list({c.id: c for c in [*candidates, *known.values()]}.values())
        plan = finalize_plan(outcome.value, inputs, pool)
        warnings = _carried_warnings(base, inputs, pool, holidays, weather)
        warnings += list(getattr(plan, "llm_warnings", None) or [])
        over = _over_budget_warning(plan, inputs.language)
        if over:
            warnings.append(over)
        warnings += check_plan(plan, inputs, pool, holidays=holidays, weather=weather)
        plan.warnings = list(dict.fromkeys(warnings))
        plan.generator = "claude"
        logger.info("Claude %s: %d days, %d items, %d warnings, ~$%.3f", label, len(plan.days),
                    sum(len(d.items) for d in plan.days), len(plan.warnings), outcome.usage.est_usd)
        return plan, outcome.usage

    # ------------------------------------------------------------------ conversation loop

    def _run_tool_loop(
        self,
        *,
        system: str,
        tools: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        tool_name: str,
        accept: Callable[[Any, bool], T],
        label: str,
    ) -> _LoopOutcome[T]:
        """One append-only conversation until ``tool_name`` is submitted with valid input.

        ``accept(raw_input, lenient)`` validates/converts a submission or raises
        :class:`_Rejected` (``lenient`` is True on the last allowed attempt, for soft checks).
        """
        system_blocks = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
        usage = LLMUsage()
        seen = _SeenUrls()
        pauses = rejections = json_retries = 0
        nudged = False
        container: str | None = None
        for _ in range(MAX_REQUESTS):
            try:
                message = self._request(system_blocks, tools, messages, container, label)
            except _UnparseableToolInput as exc:
                json_retries += 1
                logger.warning("Claude %s: unparseable tool input (%s); re-issuing", label, exc)
                if json_retries > MAX_JSON_RETRIES:
                    raise self._error("invalid_output", detail=str(exc), usage=usage) from exc
                continue
            except ItineraryLLMError as exc:
                exc.usage = usage
                raise
            usage.add_message(message, self.model)
            seen.add(harvest_source_urls(message.content))
            container = self._container_id(message) or container
            stop = message.stop_reason
            self._raise_for_stop(stop, message, usage, label)
            messages.append({"role": "assistant", "content": _history_content(message.content)})
            if stop == "pause_turn":
                pauses += 1
                if pauses > MAX_PAUSE_TURNS:
                    raise self._error("incomplete", detail=f"{label}: still paused after {MAX_PAUSE_TURNS} "
                                      "continuations", usage=usage)
                continue
            calls = [b for b in message.content if getattr(b, "type", None) == "tool_use"]
            if calls:
                lenient = rejections >= MAX_VALIDATION_RETRIES
                value, results = self._handle_calls(calls, tool_name, accept, lenient)
                if value is not None:
                    return _LoopOutcome(value[0], usage, seen)
                rejections += 1
                if rejections > MAX_VALIDATION_RETRIES:
                    raise self._error("invalid_output", detail=f"{label}: input rejected "
                                      f"{rejections} times: {results[-1]['content'][:300]}", usage=usage)
                messages.append({"role": "user", "content": results})
                continue
            if nudged:
                raise self._error("invalid_output", detail=f"{label}: no {tool_name} call after a reminder",
                                  usage=usage)
            nudged = True
            nudge = NUDGE_TEXT.format(tool=tool_name)
            messages.append({"role": "user", "content": [{"type": "text", "text": nudge}]})
        raise self._error("incomplete", detail=f"{label}: more than {MAX_REQUESTS} requests", usage=usage)

    @staticmethod
    def _handle_calls(
        calls: Sequence[Any], tool_name: str, accept: Callable[[Any, bool], T], lenient: bool
    ) -> tuple[tuple[T] | None, list[dict[str, Any]]]:
        """Validate the turn's tool calls: ``((value,), results)`` on success, else
        ``(None, tool_result blocks)`` with ``is_error`` results to send back."""
        results: list[dict[str, Any]] = []
        for call in calls:
            name = getattr(call, "name", "")
            if name != tool_name:
                results.append(_tool_result(call.id, f"Unknown tool {name!r}; call {tool_name}.", is_error=True))
                continue
            try:
                value = accept(getattr(call, "input", None), lenient)
            except _Rejected as exc:
                results.append(_tool_result(call.id, f"{exc}\nFix these problems and call {tool_name} again "
                                            "with the complete corrected input.", is_error=True))
                continue
            return (value,), results
        return None, results

    @staticmethod
    def _container_id(message: Any) -> str | None:
        container = getattr(message, "container", None)
        cid = getattr(container, "id", None) if container is not None else None
        return cid if isinstance(cid, str) and cid else None

    def _raise_for_stop(self, stop: str | None, message: Any, usage: LLMUsage, label: str) -> None:
        """Refusal / truncation / context overflow end the call before content is read."""
        if stop == "refusal":
            details = getattr(message, "stop_details", None)
            category = getattr(details, "category", None)
            explanation = getattr(details, "explanation", None)
            logger.warning("Claude %s refused (category=%s): %s", label, category, explanation)
            suffix = f" ({category})" if category else ""
            raise self._error("refusal", detail=f"category={category}: {explanation}", usage=usage,
                              category=suffix)
        if stop == "max_tokens":
            raise self._error("max_tokens", detail=f"{label}: max_tokens={MAX_TOKENS} reached", usage=usage)
        if stop == "model_context_window_exceeded":
            raise self._error("context", detail=f"{label}: context window exceeded", usage=usage)

    def _request(
        self,
        system: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        container: str | None,
        label: str,
    ) -> Any:
        """One streamed request; SDK errors are mapped most-specific-first."""
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": MAX_TOKENS,
            "system": system,
            "tools": tools,
            "tool_choice": {"type": "auto"},
            "messages": messages,
            "output_config": {"effort": self.effort},
            "cache_control": {"type": "ephemeral"},
            "betas": [FALLBACK_BETA],
            "fallbacks": "default",
        }
        if container:
            kwargs["container"] = container
        try:
            try:
                with self.client.beta.messages.stream(**kwargs) as stream:
                    message = stream.get_final_message()
                    request_id = getattr(stream, "request_id", None)
            except ValueError as exc:  # the SDK could not parse a streamed tool input at all
                raise _UnparseableToolInput(str(exc)[:300]) from exc
        except anthropic.AuthenticationError as exc:
            raise self._error("auth", detail=self._status_detail(exc)) from exc
        except anthropic.PermissionDeniedError as exc:
            raise self._error("permission", detail=self._status_detail(exc)) from exc
        except anthropic.NotFoundError as exc:
            raise self._error("model", detail=self._status_detail(exc), model=self.model) from exc
        except anthropic.RateLimitError as exc:
            raise self._error("rate_limit", detail=self._status_detail(exc)) from exc
        except anthropic.RequestTooLargeError as exc:
            raise self._error("too_large", detail=self._status_detail(exc)) from exc
        except anthropic.BadRequestError as exc:
            detail = self._status_detail(exc)
            kind: ErrorKind = "billing" if "credit balance" in detail.lower() else "bad_request"
            raise self._error(kind, detail=detail) from exc
        except (anthropic.OverloadedError, anthropic.ServiceUnavailableError,
                anthropic.InternalServerError) as exc:
            raise self._error("overloaded", detail=self._status_detail(exc)) from exc
        except anthropic.APIStatusError as exc:  # incl. an `error` event in a 200 stream
            raise self._error(self._kind_from_body(exc), detail=self._status_detail(exc)) from exc
        except anthropic.APITimeoutError as exc:
            raise self._error("timeout", detail=type(exc).__name__) from exc
        except anthropic.APIConnectionError as exc:
            raise self._error("network", detail=f"{type(exc).__name__}: {exc}") from exc
        except anthropic.APIError as exc:
            raise self._error("api", detail=f"{type(exc).__name__}: {exc}") from exc
        logger.info(
            "Claude %s: stop=%s model=%s request=%s in=%s out=%s cache_read=%s",
            label, message.stop_reason, getattr(message, "model", None), request_id,
            getattr(message.usage, "input_tokens", None), getattr(message.usage, "output_tokens", None),
            getattr(message.usage, "cache_read_input_tokens", None),
        )
        return message

    @staticmethod
    def _kind_from_body(exc: anthropic.APIStatusError) -> ErrorKind:
        """Kind of an error without a telling status code (a mid-stream ``error`` event
        arrives on an HTTP 200 response): from the body's ``error.type``."""
        body = exc.body if isinstance(exc.body, dict) else {}
        error = body.get("error") if isinstance(body.get("error"), dict) else body
        kind = str(error.get("type", "")) if isinstance(error, dict) else ""
        return {
            "overloaded_error": "overloaded",
            "api_error": "overloaded",
            "rate_limit_error": "rate_limit",
            "authentication_error": "auth",
            "permission_error": "permission",
            "not_found_error": "model",
            "request_too_large": "too_large",
            "invalid_request_error": "bad_request",
        }.get(kind, "api")  # type: ignore[return-value]

    @staticmethod
    def _status_detail(exc: anthropic.APIStatusError) -> str:
        request_id = getattr(exc, "request_id", None)
        return f"HTTP {exc.status_code}: {exc.message}" + (f" (request {request_id})" if request_id else "")

    def _error(
        self, kind: ErrorKind, *, detail: str | None = None, usage: LLMUsage | None = None, **fmt: str
    ) -> ItineraryLLMError:
        texts = _ERROR_TEXT[kind]
        template = texts.get(self.language, texts["en"])
        fmt.setdefault("model", self.model)
        fmt.setdefault("category", "")
        message = template.format(**fmt)
        logger.warning("Claude call failed (%s): %s", kind, detail or message)
        return ItineraryLLMError(kind, message, detail=detail, usage=usage)
