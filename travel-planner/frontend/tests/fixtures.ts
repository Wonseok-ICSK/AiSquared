/** Shared test fixtures shaped exactly like the API types (schemas.py). */

import type { AirportOut, TripOut } from "../src/api/types";
import { setLang } from "../src/lib/i18n";
import type { Lang } from "../src/lib/i18n";

/** A stored trip; override any field. */
export function makeTrip(overrides: Partial<TripOut> = {}): TripOut {
  return {
    id: 1,
    name: "Tokyo food trip",
    origin: "ICN",
    destination: "TYO",
    trip_type: "round_trip",
    depart_date: "2026-11-20",
    return_date: "2026-11-27",
    adults: 2,
    cabin: "economy",
    direct_only: false,
    max_stops: null,
    outbound_depart_from: null,
    outbound_depart_to: null,
    outbound_arrive_from: null,
    outbound_arrive_to: null,
    return_depart_from: null,
    return_depart_to: null,
    return_arrive_from: null,
    return_arrive_to: null,
    max_price: null,
    target_price: 300000,
    price_basis: "per_person",
    currency: "KRW",
    flex_depart_days: 0,
    flex_return_days: 0,
    include_nearby_airports: false,
    alt_destinations: [],
    providers: [],
    schedule_mode: "times",
    schedule_times: ["08:30", "17:30"],
    schedule_interval_hours: null,
    active: true,
    notes: null,
    last_checked_at: "2026-10-06T08:30:00Z",
    last_status: "ok",
    last_error: null,
    created_at: "2026-10-01T00:00:00Z",
    updated_at: "2026-10-06T08:30:00Z",
    latest_min_price: 556000,
    latest_min_price_basis_value: 278000,
    lowest_ever: 278000,
    next_run_at: "2026-10-06T17:30:00Z",
    unread_alerts: 0,
    ...overrides,
  };
}

/** An airport (or metro when `is_metro`) as GET /api/airports returns it. */
export function makeAirport(code: string, city: string, cityCode: string | null, isMetro = false, name?: string): AirportOut {
  return {
    code,
    name: name ?? (isMetro ? city : `${city} ${code} Airport`),
    city,
    city_code: cityCode,
    country: "KR",
    lat: 0,
    lon: 0,
    is_metro: isMetro,
  };
}

export const SEL = makeAirport("SEL", "Seoul", "SEL", true);
export const ICN = makeAirport("ICN", "Seoul", "SEL", false, "Incheon International Airport");
export const GMP = makeAirport("GMP", "Seoul", "SEL", false, "Gimpo International Airport");
export const TYO = makeAirport("TYO", "Tokyo", "TYO", true);
export const NRT = makeAirport("NRT", "Tokyo", "TYO", false, "Narita International Airport");
export const HND = makeAirport("HND", "Tokyo", "TYO", false, "Haneda Airport");
export const OSA = makeAirport("OSA", "Osaka", "OSA", true);
export const KIX = makeAirport("KIX", "Osaka", "OSA", false, "Kansai International Airport");
export const ITM = makeAirport("ITM", "Osaka", "OSA", false, "Osaka Itami Airport");

/** Run `fn` with the UI language set to `lang`, restoring Korean (the default) afterwards. */
export function inLang<T>(lang: Lang, fn: () => T): T {
  setLang(lang);
  try {
    return fn();
  } finally {
    setLang("ko");
  }
}
