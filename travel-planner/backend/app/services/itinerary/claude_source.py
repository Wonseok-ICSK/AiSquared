"""Claude web research as a :class:`~app.services.itinerary.candidates.CandidateSource`
(DESIGN §10 step 2, §13.4).

Wraps :meth:`ItineraryLLM.research_candidates`: Claude searches the web (server tool) and
submits place facts whose ``source_urls`` were verified against the search results. Like
every source it never raises for ordinary failures: :attr:`last_error` holds the
user-facing message, :attr:`last_exception` the :class:`ItineraryLLMError` (``fatal`` tells
the caller whether further Claude calls make sense) and :attr:`last_usage` the spend.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.services.itinerary.candidates import CandidateSource
from app.services.itinerary.llm import ItineraryLLM, ItineraryLLMError, LLMUsage
from app.services.itinerary.models import Candidate, PlanInputs

logger = logging.getLogger(__name__)


class ClaudeWebResearchSource(CandidateSource):
    """Candidates researched by Claude with web search (facts + verified source URLs)."""

    key = "claude"
    display_name = "Claude web research"
    attribution = None

    def __init__(self, llm: ItineraryLLM) -> None:
        super().__init__()
        self.llm = llm
        self.last_usage = LLMUsage()
        self.last_exception: ItineraryLLMError | None = None

    def fetch(self, inputs: PlanInputs, db: Session | None = None) -> list[Candidate]:
        """Researched candidates (``coord_source="llm"`` — geocode them afterwards); ``[]``
        when the research failed (see :attr:`last_error` / :attr:`last_exception`)."""
        self.last_error = None
        self.last_exception = None
        self.last_usage = LLMUsage()
        try:
            candidates, usage = self.llm.research_candidates(inputs)
        except ItineraryLLMError as exc:
            logger.warning("Claude research for %s failed (%s): %s", inputs.city, exc.kind, exc.detail or exc)
            self.last_error = str(exc)
            self.last_exception = exc
            self.last_usage = exc.usage
            return []
        self.last_usage = usage
        return candidates
