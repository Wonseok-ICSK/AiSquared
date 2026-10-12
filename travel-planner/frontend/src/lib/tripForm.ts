/** Trip form model: state <-> API payloads, and validation mirroring schemas.TripCreate. */

import type {
  AirportOut,
  Cabin,
  FlexDays,
  MetaDefaults,
  PriceBasis,
  ScheduleMode,
  TripCreate,
  TripOut,
  TripType,
  TripUpdate,
} from "../api/types";
import { MAX_SCHEDULE_TIMES } from "../api/types";
import { josa, tr } from "./i18n";
import type { Errors } from "./validation";
import {
  checkLength,
  checkMoney,
  checkTime,
  collect,
  isCurrencyCode,
  isIataCode,
  normalizeHHMM,
  numberText,
  parseNumber,
} from "./validation";

export const TIME_WINDOW_FIELDS = [
  "outbound_depart_from",
  "outbound_depart_to",
  "outbound_arrive_from",
  "outbound_arrive_to",
  "return_depart_from",
  "return_depart_to",
  "return_arrive_from",
  "return_arrive_to",
] as const;
export type TimeWindowField = (typeof TIME_WINDOW_FIELDS)[number];

export interface TripFormState {
  name: string;
  origin: string;
  destination: string;
  trip_type: TripType;
  depart_date: string;
  return_date: string;
  adults: number;
  cabin: Cabin;
  direct_only: boolean;
  /** "" = any number of stops. */
  max_stops: string;
  times: Record<TimeWindowField, string>;
  max_price: string;
  target_price: string;
  price_basis: PriceBasis;
  currency: string;
  flex_depart_days: FlexDays;
  flex_return_days: FlexDays;
  include_nearby_airports: boolean;
  alt_destinations: string[];
  providers: string[];
  schedule_mode: ScheduleMode;
  schedule_times: string[];
  schedule_interval_hours: string;
  active: boolean;
  notes: string;
}

const EMPTY_TIMES = Object.fromEntries(TIME_WINDOW_FIELDS.map((f) => [f, ""])) as Record<
  TimeWindowField,
  string
>;

function asFlex(value: number): FlexDays {
  return value === 1 || value === 3 || value === 7 ? value : 0;
}

/** Adults allowed per trip (schemas.TripCreate: 1 <= adults <= 2). */
export const MIN_ADULTS = 1;
export const MAX_ADULTS = 2;

// ----------------------------------------------------------------------------- prefill

/** Query parameters `/trips/new` understands (the dashboard composer sends these names). */
export const PREFILL_PARAMS = ["origin", "destination", "depart", "return", "adults", "trip_type"] as const;
export type PrefillParam = (typeof PREFILL_PARAMS)[number];

/** Form values taken from `/trips/new?…`; only valid values are present. */
export interface TripPrefill {
  origin?: string;
  destination?: string;
  depart_date?: string;
  return_date?: string;
  adults?: number;
  trip_type?: TripType;
  /** Parameters that were given but ignored because their value was not valid. */
  ignored: PrefillParam[];
}

/** "YYYY-MM-DD" naming a real calendar day (2026-02-30 is rejected). */
export function isIsoDate(value: string): boolean {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  if (!match) return false;
  const [y, m, d] = [Number(match[1]), Number(match[2]), Number(match[3])];
  const date = new Date(Date.UTC(y, m - 1, d));
  return date.getUTCFullYear() === y && date.getUTCMonth() === m - 1 && date.getUTCDate() === d;
}

/**
 * Read trip fields from the new-trip URL (`?origin=ICN&destination=NRT&depart=2026-11-20&
 * return=2026-11-27&adults=2&trip_type=round_trip`, any subset). Soft validation: a value that
 * is not valid is left out (and listed in `ignored`) instead of failing the whole link:
 * - origin/destination: 3-letter airport or city code, case-insensitive (TYO, osa → OSA);
 * - depart/return: real YYYY-MM-DD dates; a return before the departure is ignored;
 * - adults: a whole number from MIN_ADULTS to MAX_ADULTS;
 * - trip_type: "round_trip" or "one_way". When absent (or ignored) it follows the dates:
 *   round trip when a return date was accepted, one way when only a departure date was.
 */
export function parseTripPrefill(params: URLSearchParams | string): TripPrefill {
  const query = typeof params === "string" ? new URLSearchParams(params) : params;
  const prefill: TripPrefill = { ignored: [] };
  const read = (key: PrefillParam): string | null => {
    const raw = query.get(key);
    return raw === null || raw.trim() === "" ? null : raw.trim();
  };
  const reject = (key: PrefillParam) => prefill.ignored.push(key);

  for (const key of ["origin", "destination"] as const) {
    const raw = read(key);
    if (raw === null) continue;
    if (isIataCode(raw)) prefill[key] = raw.toUpperCase();
    else reject(key);
  }

  const depart = read("depart");
  if (depart !== null) {
    if (isIsoDate(depart)) prefill.depart_date = depart;
    else reject("depart");
  }
  const ret = read("return");
  if (ret !== null) {
    if (isIsoDate(ret) && !(prefill.depart_date && ret < prefill.depart_date)) prefill.return_date = ret;
    else reject("return");
  }

  const adults = read("adults");
  if (adults !== null) {
    const n = /^\d+$/.test(adults) ? Number(adults) : Number.NaN;
    if (n >= MIN_ADULTS && n <= MAX_ADULTS) prefill.adults = n;
    else reject("adults");
  }

  const tripType = read("trip_type");
  if (tripType === "round_trip" || tripType === "one_way") prefill.trip_type = tripType;
  else {
    if (tripType !== null) reject("trip_type");
    if (prefill.return_date) prefill.trip_type = "round_trip";
    else if (prefill.depart_date) prefill.trip_type = "one_way";
  }
  return prefill;
}

/** Whether the prefill carries any field (an empty or fully ignored query does not). */
export function hasPrefill(prefill: TripPrefill): boolean {
  return Object.keys(prefill).some((key) => key !== "ignored");
}

/** Form state with the prefilled fields applied on top (the rest stays as given). */
export function applyTripPrefill(form: TripFormState, prefill: TripPrefill): TripFormState {
  return {
    ...form,
    origin: prefill.origin ?? form.origin,
    destination: prefill.destination ?? form.destination,
    depart_date: prefill.depart_date ?? form.depart_date,
    return_date: prefill.return_date ?? form.return_date,
    adults: prefill.adults ?? form.adults,
    trip_type: prefill.trip_type ?? form.trip_type,
  };
}

/** Blank form pre-filled with /api/meta defaults. */
export function emptyTripForm(defaults: MetaDefaults): TripFormState {
  return {
    name: "",
    origin: "",
    destination: "",
    trip_type: defaults.trip_type,
    depart_date: "",
    return_date: "",
    adults: defaults.adults,
    cabin: defaults.cabin,
    direct_only: false,
    max_stops: "",
    times: { ...EMPTY_TIMES },
    max_price: "",
    target_price: "",
    price_basis: defaults.price_basis,
    currency: defaults.currency,
    flex_depart_days: 0,
    flex_return_days: 0,
    include_nearby_airports: false,
    alt_destinations: [],
    providers: [],
    schedule_mode: defaults.schedule_mode,
    schedule_times: [...defaults.schedule_times],
    schedule_interval_hours: "",
    active: true,
    notes: "",
  };
}

/** Name the backend gives a trip saved without one (mirrors schemas.auto_trip_name). */
export function autoTripName(origin: string, destination: string): string {
  return `${origin} → ${destination}`;
}

/** Whether the stored trip still carries its auto-generated name. */
function hasAutoName(trip: Pick<TripOut, "name" | "origin" | "destination">): boolean {
  return trip.name === autoTripName(trip.origin, trip.destination);
}

/**
 * Form state from a stored trip (edit mode). An auto-generated name is shown as the field's
 * placeholder (blank value), so it keeps following the route.
 */
export function tripToForm(trip: TripOut): TripFormState {
  const times = { ...EMPTY_TIMES };
  for (const f of TIME_WINDOW_FIELDS) times[f] = trip[f] ?? "";
  return {
    name: hasAutoName(trip) ? "" : trip.name,
    origin: trip.origin,
    destination: trip.destination,
    trip_type: trip.trip_type,
    depart_date: trip.depart_date,
    return_date: trip.return_date ?? "",
    adults: trip.adults,
    cabin: trip.cabin,
    direct_only: trip.direct_only,
    max_stops: trip.max_stops === null ? "" : String(trip.max_stops),
    times,
    max_price: numberText(trip.max_price),
    target_price: numberText(trip.target_price),
    price_basis: trip.price_basis,
    currency: trip.currency,
    flex_depart_days: asFlex(trip.flex_depart_days),
    flex_return_days: asFlex(trip.flex_return_days),
    include_nearby_airports: trip.include_nearby_airports,
    alt_destinations: [...trip.alt_destinations],
    providers: [...trip.providers],
    schedule_mode: trip.schedule_mode,
    schedule_times: [...trip.schedule_times],
    schedule_interval_hours: numberText(trip.schedule_interval_hours),
    active: trip.active,
    notes: trip.notes ?? "",
  };
}

/** Full TripCreate body from (validated) form state. */
export function formToPayload(form: TripFormState): TripCreate {
  const oneWay = form.trip_type === "one_way";
  const payload: TripCreate = {
    name: form.name.trim(),
    origin: form.origin.trim().toUpperCase(),
    destination: form.destination.trim().toUpperCase(),
    trip_type: form.trip_type,
    depart_date: form.depart_date,
    return_date: oneWay || !form.return_date ? null : form.return_date,
    adults: form.adults,
    cabin: form.cabin,
    direct_only: form.direct_only,
    max_stops: form.direct_only || form.max_stops === "" ? null : Number(form.max_stops),
    max_price: parseNumber(form.max_price),
    target_price: parseNumber(form.target_price),
    price_basis: form.price_basis,
    currency: form.currency.trim().toUpperCase(),
    flex_depart_days: form.flex_depart_days,
    flex_return_days: oneWay ? 0 : form.flex_return_days,
    include_nearby_airports: form.include_nearby_airports,
    alt_destinations: [...form.alt_destinations],
    providers: [...form.providers],
    schedule_mode: form.schedule_mode,
    schedule_times: [...new Set(form.schedule_times.map((t) => normalizeHHMM(t) ?? t))].sort(),
    schedule_interval_hours:
      form.schedule_mode === "interval" ? parseNumber(form.schedule_interval_hours) : null,
    active: form.active,
    notes: form.notes.trim() ? form.notes : null,
  };
  for (const f of TIME_WINDOW_FIELDS) {
    const isReturn = f.startsWith("return_");
    const value = form.times[f].trim();
    payload[f] = (oneWay && isReturn) || !value ? null : (normalizeHHMM(value) ?? value);
  }
  if (payload.schedule_mode === "interval" && payload.schedule_times?.length === 0) {
    delete payload.schedule_times;
  }
  return payload;
}

function sameValue(a: unknown, b: unknown): boolean {
  return JSON.stringify(a ?? null) === JSON.stringify(b ?? null);
}

/**
 * TripUpdate body containing only fields that differ from the stored trip.
 *
 * A blank name is sent as `""` only when it clears a custom name (the backend then falls back
 * to "ORIGIN → DESTINATION"); a blank name over an auto-generated one is left out, so the
 * backend regenerates it when the route changes (TripUpdate.merged_with).
 */
export function diffPayload(original: TripOut, payload: TripCreate): TripUpdate {
  const update: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(payload)) {
    if (key === "name" && value === "") {
      if (!hasAutoName(original)) update.name = "";
      continue;
    }
    if (!sameValue((original as unknown as Record<string, unknown>)[key], value)) update[key] = value;
  }
  return update as TripUpdate;
}

/**
 * Why origin and destination may not be combined, mirroring schemas._check_trip_consistency
 * (`airports_for(origin) & airports_for(destination)` must be empty): the same code, or a city
 * code and one of its own airports (SEL/ICN). Two different airports of one city (ICN/GMP) are
 * allowed. `null` when fine or not yet known (lookups still loading).
 */
export function routeOverlapError(
  origin: string,
  destination: string,
  originAirport: AirportOut | null | undefined,
  destinationAirport: AirportOut | null | undefined,
): string | null {
  if (origin === destination) return tr("Origin and destination must differ", "출발지와 도착지는 달라야 해요");
  const contains = (metro: AirportOut | null | undefined, code: string, airport: AirportOut | null | undefined) =>
    Boolean(metro?.is_metro && airport && !airport.is_metro && airport.city_code === metro.code && airport.code === code);
  if (contains(originAirport, destination, destinationAirport)) {
    return tr(
      `${origin} already includes ${destination}; pick a different destination`,
      `${origin}에 이미 ${josa(destination, "이/가")} 포함돼 있어요. 다른 도착지를 선택하세요`,
    );
  }
  if (contains(destinationAirport, origin, originAirport)) {
    return tr(
      `${destination} already includes ${origin}; pick a different destination`,
      `${destination}에 이미 ${josa(origin, "이/가")} 포함돼 있어요. 다른 도착지를 선택하세요`,
    );
  }
  return null;
}

/** Field errors for the trip form (keys = TripCreate field names, "times.<field>" for windows). */
export function validateTripForm(
  form: TripFormState,
  airports: { origin?: AirportOut | null; destination?: AirportOut | null },
  naverAcknowledged: boolean,
): Errors {
  const origin = form.origin.trim().toUpperCase();
  const destination = form.destination.trim().toUpperCase();
  const roundTrip = form.trip_type === "round_trip";

  const routeError =
    isIataCode(origin) && isIataCode(destination)
      ? routeOverlapError(origin, destination, airports.origin, airports.destination)
      : null;

  let returnError: string | null = null;
  if (roundTrip) {
    if (!form.return_date) returnError = tr("Return date is required for a round trip", "왕복 여행은 오는 날을 입력해야 해요");
    else if (form.depart_date && form.return_date < form.depart_date) {
      returnError = tr("Return date must be on or after the departure date", "오는 날은 가는 날과 같거나 그 이후여야 해요");
    }
  }

  let scheduleError: string | null = null;
  let intervalError: string | null = null;
  if (form.schedule_mode === "times") {
    if (form.schedule_times.length === 0) scheduleError = tr("Add at least one time (HH:MM)", "시간을 하나 이상 추가하세요 (HH:MM)");
    else if (form.schedule_times.length > MAX_SCHEDULE_TIMES) {
      scheduleError = tr(`At most ${MAX_SCHEDULE_TIMES} times`, `최대 ${MAX_SCHEDULE_TIMES}개까지 추가할 수 있어요`);
    } else if (form.schedule_times.some((t) => !normalizeHHMM(t))) {
      scheduleError = tr("Times must be HH:MM (24h)", "시간은 HH:MM (24시간) 형식이어야 해요");
    }
  } else {
    const hours = parseNumber(form.schedule_interval_hours);
    if (hours === null) intervalError = tr("Required for an interval schedule", "간격 주기를 쓰려면 입력해야 해요");
    else if (Number.isNaN(hours) || hours < 1 || hours > 168) intervalError = tr("Between 1 and 168 hours", "1~168시간 사이로 입력하세요");
  }

  const timeErrors: [string, string | null][] = TIME_WINDOW_FIELDS.filter(
    (f) => roundTrip || !f.startsWith("return_"),
  ).map((f) => [`times.${f}`, checkTime(form.times[f])]);

  return collect([
    ["name", checkLength(form.name, 200)],
    [
      "origin",
      !origin
        ? tr("Required", "필수 항목이에요")
        : !isIataCode(origin)
          ? tr("Use a 3-letter airport or city code", "3자리 공항 또는 도시 코드를 입력하세요")
          : null,
    ],
    [
      "destination",
      !destination
        ? tr("Required", "필수 항목이에요")
        : !isIataCode(destination)
          ? tr("Use a 3-letter airport or city code", "3자리 공항 또는 도시 코드를 입력하세요")
          : routeError,
    ],
    ["depart_date", form.depart_date ? null : tr("Required", "필수 항목이에요")],
    ["return_date", returnError],
    [
      "adults",
      Number.isInteger(form.adults) && form.adults >= MIN_ADULTS && form.adults <= MAX_ADULTS
        ? null
        : tr("1 or 2 adults", "성인 1명 또는 2명"),
    ],
    ["max_price", checkMoney(form.max_price)],
    ["target_price", checkMoney(form.target_price)],
    [
      "currency",
      isCurrencyCode(form.currency) ? null : tr("Use a 3-letter currency code (e.g. KRW)", "3자리 통화 코드를 입력하세요 (예: KRW)"),
    ],
    ["alt_destinations", form.alt_destinations.length > 10 ? tr("At most 10 airports", "공항은 최대 10개까지예요") : null],
    ["schedule_times", scheduleError],
    ["schedule_interval_hours", intervalError],
    ["notes", checkLength(form.notes, 4000)],
    ["providers", form.providers.includes("naver") && !naverAcknowledged
      ? tr("Tick the box to confirm you accept the Naver terms risk", "Naver 약관 위반 위험을 이해했다면 체크박스를 선택하세요")
      : null],
    ...timeErrors,
  ]);
}

/** Non-blocking warnings (shown but do not prevent saving). */
export function tripFormWarnings(form: TripFormState, today: string): string[] {
  const warnings: string[] = [];
  if (form.depart_date && form.depart_date < today) warnings.push(tr("The departure date is in the past.", "가는 날이 이미 지났어요."));
  const max = parseNumber(form.max_price);
  const target = parseNumber(form.target_price);
  if (max !== null && target !== null && !Number.isNaN(max) && !Number.isNaN(target) && target > max) {
    warnings.push(
      tr(
        "The target price is above the maximum price, so offers at the target are excluded.",
        "목표 가격이 최대 가격보다 높아서, 목표 가격대의 항공권은 제외돼요.",
      ),
    );
  }
  return warnings;
}
