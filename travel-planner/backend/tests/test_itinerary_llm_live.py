"""Live checks of the Claude itinerary calls (RUN_LIVE=1).

* An invalid key against the real API must map to a fatal ``auth`` error (1 request, free).
* With a real key in ``ANTHROPIC_API_KEY`` (read at import, before the test fixtures clear
  the environment): one research + compose round trip for a 2-day Tokyo stay (costs a few
  tens of US cents). Skipped without a key.
"""

from __future__ import annotations

import os
from datetime import datetime

import pytest

from app.services.itinerary.llm import ItineraryLLM, ItineraryLLMError
from app.services.itinerary.planner import plan_itinerary
from tests.test_itinerary_llm import tokyo_inputs

LIVE_KEY = os.environ.get("ANTHROPIC_API_KEY")


@pytest.mark.live
def test_live_invalid_key_is_reported_as_auth() -> None:
    llm = ItineraryLLM(api_key="sk-ant-api03-invalid-key-for-a-live-test", language="en")
    with pytest.raises(ItineraryLLMError) as info:
        llm.research_candidates(tokyo_inputs())
    assert info.value.kind == "auth" and info.value.fatal
    assert "invalid" in str(info.value).lower()


@pytest.mark.live
@pytest.mark.skipif(not LIVE_KEY, reason="needs ANTHROPIC_API_KEY")
def test_live_research_and_compose() -> None:
    inputs = tokyo_inputs(arrival_at=datetime(2026, 11, 20, 10, 0), departure_at=datetime(2026, 11, 21, 20, 0))
    llm = ItineraryLLM(api_key=LIVE_KEY, model="claude-opus-5-5", effort="medium", language="en")
    candidates, usage = llm.research_candidates(inputs)
    assert len(candidates) >= 8 and usage.web_search_requests >= 1
    assert any(c.sources_verified for c in candidates)
    draft = plan_itinerary(inputs, candidates)
    plan, compose_usage = llm.compose_itinerary(inputs, draft, candidates)
    assert plan.generator == "claude" and len(plan.days) == len(draft.days)
    print("research", usage.to_dict(), "compose", compose_usage.to_dict())
