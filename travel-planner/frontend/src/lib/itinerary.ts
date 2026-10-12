/** Pure helpers for showing an ItineraryPlan (attributions, cost basis). */

import type { ItineraryGenerator, ItineraryPlan } from "../api/types";
import { tr } from "./i18n";

// The backend's own credit lines (candidates/geocode/weather ATTRIBUTION), in English.
export const OPEN_METEO_ATTRIBUTION = "Weather data by Open-Meteo.com (CC BY 4.0)";
export const OSM_ATTRIBUTION = "Map data © OpenStreetMap contributors (ODbL)";
export const NOMINATIM_ATTRIBUTION = "Geocoding: Nominatim, © OpenStreetMap contributors (ODbL)";

/** A credit line in the current language (known backend lines are translated to Korean). */
export function attributionText(line: string): string {
  if (line === OPEN_METEO_ATTRIBUTION) return tr(line, "날씨 데이터: Open-Meteo.com (CC BY 4.0)");
  if (line === OSM_ATTRIBUTION) return tr(line, "지도 데이터 © OpenStreetMap 기여자 (ODbL)");
  if (line === NOMINATIM_ATTRIBUTION) return tr(line, "지오코딩: Nominatim, © OpenStreetMap 기여자 (ODbL)");
  return line;
}

type PlanLike = Pick<ItineraryPlan, "days"> & { attributions?: unknown; cost_basis?: unknown };

function stringList(value: unknown): string[] | null {
  if (!Array.isArray(value)) return null;
  return value.filter((v): v is string => typeof v === "string" && v.trim() !== "").map((v) => v.trim());
}

/**
 * Data credits to show under a plan (ODbL / CC BY require them), in the current language.
 * The planner's own `plan.attributions` (extra field, built from where coordinates and
 * weather came from) wins; older plans without it fall back to heuristics: OSM for
 * rule-based plans (Overpass data), Nominatim/OSM for Claude plans with coordinates
 * (geocoded via Nominatim, DESIGN §13.4). Weather from Open-Meteo is always credited when
 * any day has weather.
 */
export function planAttributions(plan: PlanLike, generator: ItineraryGenerator): string[] {
  const own = stringList(plan.attributions);
  const list = own ? [...own] : [];
  if (!own) {
    const hasCoords = plan.days.some((d) => d.items.some((i) => i.lat !== null && i.lat !== undefined));
    if (generator === "rule_based") list.push(OSM_ATTRIBUTION);
    else if (hasCoords) list.push(NOMINATIM_ATTRIBUTION);
  }
  const hasWeather = plan.days.some((d) => d.weather);
  if (hasWeather && !list.some((a) => a.includes("Open-Meteo"))) list.push(OPEN_METEO_ATTRIBUTION);
  return [...new Set(list)].map(attributionText);
}

/** Label for item/day cost estimates ("per person" / "1인"), from the plan's `cost_basis` extra field. */
export function costBasisLabel(plan: PlanLike): string | null {
  if (plan.cost_basis === "per_person") return tr("per person", "1인");
  if (plan.cost_basis === "total") return tr("total", "총액");
  return null;
}
