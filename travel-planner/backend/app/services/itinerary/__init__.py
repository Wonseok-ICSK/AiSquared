"""Itinerary pipeline (DESIGN §10, §13.4).

Deterministic half (this package's core modules):

* :mod:`.models` — :class:`Candidate`, :class:`PlanInputs`, :func:`build_plan_inputs`,
  :func:`city_center_for` (the plan schema itself is :class:`app.schemas.ItineraryPlan`).
  ``PlanInputs.center_source`` says where the centre came from; ``"airport"`` means only the
  airport's location was known.
* :mod:`.candidates` — :class:`CandidateSource` ABC, :class:`OverpassSource` (OSM fallback),
  :func:`merge_candidates`.
* :mod:`.geocode` — Nominatim + permanent ``GeocodeCache``: :func:`geocode_candidates`,
  :func:`geocode_city`, :func:`resolve_plan_locations` (airport-only centre -> geocoded city;
  ``hotel_area`` -> approximate hotel point). The single-name ``geocode.geocode()`` is not
  re-exported so the submodule name stays usable.
* :mod:`.weather` — Open-Meteo forecast / climatology: :func:`get_weather`.
* :mod:`.calendar_util` — holidays and closures: :func:`holidays_for`, :func:`is_closed`.
* :mod:`.routing` — travel-time model, balanced clustering, stop ordering, route metrics.
* :mod:`.budget` — budget semantics, cost defaults, :func:`build_budget_summary`.
* :mod:`.links` — Google Maps / Tabelog / Naver Map links.
* :mod:`.planner` — :func:`plan_itinerary` (rule-based plan), :func:`finalize_plan` and
  :func:`check_plan` (also for Claude-composed plans).

Typical keyless flow::

    # db= also geocodes (cached) an airport-only city centre and the hotel area
    inputs = build_plan_inputs(trip, prefs, arrival_at=..., departure_at=..., ..., db=db)
    cands = merge_candidates(OverpassSource().fetch(inputs, db))
    cands = geocode_candidates(db, cands, inputs.city, inputs.country, inputs.center)
    wx = get_weather(inputs.center_lat, inputs.center_lon, inputs.dates, inputs.timezone)
    plan = plan_itinerary(inputs, cands, weather=wx)

Claude-composed plans (the draft from :func:`plan_itinerary` revised by the LLM)::

    plan = finalize_plan(llm_plan, inputs, cands, allowed_urls=web_search_urls)
    plan.warnings += check_plan(plan, inputs, cands, weather=wx)

:func:`finalize_plan` keeps only text, times and the choice/order of items from the LLM:
coordinates, source URLs and every link are rebuilt in code (keep the draft's ``hotel``
extra); :func:`check_plan` with the candidates also reports closures, opening-hours
violations and outdoor stops on wet days.
"""

from app.services.itinerary.budget import build_budget_summary
from app.services.itinerary.calendar_util import holidays_for, holidays_for_dates, is_closed
from app.services.itinerary.candidates import (
    CandidateSource,
    OverpassSource,
    StaticSource,
    merge_candidates,
)
from app.services.itinerary.geocode import geocode_candidates, geocode_city, resolve_plan_locations
from app.services.itinerary.models import (
    Candidate,
    OpeningPeriod,
    PlanInputs,
    build_plan_inputs,
    city_center_for,
)
from app.services.itinerary.planner import check_plan, finalize_plan, plan_itinerary
from app.services.itinerary.routing import route_metrics, travel_estimate
from app.services.itinerary.weather import get_weather

__all__ = [
    "Candidate",
    "CandidateSource",
    "OpeningPeriod",
    "OverpassSource",
    "PlanInputs",
    "StaticSource",
    "build_budget_summary",
    "build_plan_inputs",
    "check_plan",
    "city_center_for",
    "finalize_plan",
    "geocode_candidates",
    "geocode_city",
    "get_weather",
    "holidays_for",
    "holidays_for_dates",
    "is_closed",
    "merge_candidates",
    "plan_itinerary",
    "resolve_plan_locations",
    "route_metrics",
    "travel_estimate",
]
