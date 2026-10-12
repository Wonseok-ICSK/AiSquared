import { test } from "node:test";
import assert from "node:assert/strict";
import type { PlanDay, PlanWeather } from "../src/api/types";
import {
  NOMINATIM_ATTRIBUTION,
  OPEN_METEO_ATTRIBUTION,
  OSM_ATTRIBUTION,
  costBasisLabel,
  planAttributions,
} from "../src/lib/itinerary";
import { inLang } from "./fixtures";

function day(withCoords: boolean, weather: PlanWeather | null = null): PlanDay {
  return {
    date: "2026-11-21",
    theme: "",
    areas: [],
    est_day_cost: null,
    weather,
    items: [
      {
        start: "10:00",
        end: "11:00",
        kind: "activity",
        title: "Senso-ji",
        area: "Asakusa",
        lat: withCoords ? 35.71 : null,
        lng: withCoords ? 139.79 : null,
        description: null,
        est_cost: 0,
        cost_currency: "JPY",
        travel_from_prev: null,
        tips: null,
        map_url: null,
        source_urls: [],
        candidate_id: null,
      },
    ],
  };
}

test("the planner's own attributions are shown (Claude plans too)", () => {
  const plan = { days: [day(true, { summary: "Sunny" })], attributions: [OSM_ATTRIBUTION, NOMINATIM_ATTRIBUTION] };
  assert.deepEqual(inLang("en", () => planAttributions(plan, "claude")), [OSM_ATTRIBUTION, NOMINATIM_ATTRIBUTION, OPEN_METEO_ATTRIBUTION]);
});

test("known credit lines are shown in Korean in the Korean UI; unknown ones as sent", () => {
  const plan = { days: [day(false)], attributions: [OSM_ATTRIBUTION, "Restaurant data: Example Guide"] };
  assert.deepEqual(planAttributions(plan, "claude"), ["지도 데이터 © OpenStreetMap 기여자 (ODbL)", "Restaurant data: Example Guide"]);
});

test("weather credit is not duplicated when the planner already lists it", () => {
  const plan = { days: [day(false, { summary: "Rain" })], attributions: ["Weather data by Open-Meteo.com (CC BY 4.0)"] };
  assert.deepEqual(inLang("en", () => planAttributions(plan, "rule_based")), [OPEN_METEO_ATTRIBUTION]);
});

test("fallbacks without plan.attributions", () => {
  inLang("en", () => {
    assert.deepEqual(planAttributions({ days: [day(true)] }, "claude"), [NOMINATIM_ATTRIBUTION]);
    assert.deepEqual(planAttributions({ days: [day(false)] }, "claude"), []);
    assert.deepEqual(planAttributions({ days: [day(false)] }, "rule_based"), [OSM_ATTRIBUTION]);
  });
});

test("cost basis label", () => {
  assert.equal(costBasisLabel({ days: [], cost_basis: "per_person" }), "1인");
  assert.equal(costBasisLabel({ days: [], cost_basis: "total" }), "총액");
  assert.equal(costBasisLabel({ days: [] }), null);
  assert.equal(inLang("en", () => costBasisLabel({ days: [], cost_basis: "per_person" })), "per person");
  assert.equal(inLang("en", () => costBasisLabel({ days: [], cost_basis: "total" })), "total");
});
