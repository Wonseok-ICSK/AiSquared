/**
 * Plain-language text for the trip detail page: the summary note under the header and the
 * facts of a flight row (airline, flight numbers, return flight, separate tickets).
 * No React here, so it is unit-tested (tests/tripText.test.ts). Strings follow the app voice:
 * short, calm sentences (Korean 해요체), no exclamation marks.
 */

import type { FlightLegOut, FlightOfferOut, PriceBasis, TripOut } from "../../api/types";
import { airlineName, dayDiff, formatAdults, formatDate, formatLocalTime, formatMoney, formatRelative, formatUtc, parseUtc } from "../../lib/format";
import { priceTrend } from "../../lib/history";
import { tr } from "../../lib/i18n";

const DAY_MS = 86_400_000;

/** End a sentence with one period ("in 2 hr." already ends with one). */
function period(text: string): string {
  return text.endsWith(".") ? text : `${text}.`;
}

// ----------------------------------------------------------------------------- flight facts

/**
 * Marketing carrier codes of a leg in flight order (from segments, else the flight numbers'
 * two-character prefixes). Mirrors `leg_carriers` in backend/app/services/booking.py.
 */
export function legCarriers(leg: FlightLegOut | null | undefined): string[] {
  if (!leg) return [];
  let codes = leg.segments.map((s) => s.carrier_code?.toUpperCase() ?? "").filter(Boolean);
  if (codes.length === 0) codes = leg.flight_numbers.filter(Boolean).map((n) => n.slice(0, 2).toUpperCase());
  return [...new Set(codes)];
}

/**
 * True when no airline flies both directions (normally two separate bookings), null while the
 * return flight is unknown. Mirrors `separate_tickets` in backend/app/services/booking.py.
 */
export function separateTickets(offer: FlightOfferOut): boolean | null {
  if (!offer.inbound) return null;
  const out = legCarriers(offer.outbound);
  const back = new Set(legCarriers(offer.inbound));
  if (out.length === 0 || back.size === 0) return null;
  return !out.some((code) => back.has(code));
}

/**
 * Whether two legs are flown by the same airline(s): the same carrier codes, else the same
 * names. A return flight shows its own airline only when it differs from the outbound.
 */
export function sameAirline(a: FlightLegOut, b: FlightLegOut): boolean {
  const ca = legCarriers(a);
  const cb = legCarriers(b);
  if (ca.length > 0 && cb.length > 0) return ca.length === cb.length && ca.every((code) => cb.includes(code));
  return legAirline(a) === legAirline(b);
}

/** Airline name(s) of a leg in the UI language: "에어서울" / "Air Seoul" (empty when unknown). */
export function legAirline(leg: FlightLegOut): string {
  const names = leg.segments
    .filter((s) => s.carrier_code || s.carrier_name)
    .map((s) => airlineName(s.carrier_code, s.carrier_name));
  if (names.length > 0) return [...new Set(names)].join(", ");
  return leg.airlines.join(", ");
}

/** Flight number(s) of a leg: "RS260" or "KE703 / KE5741" (empty when unknown). */
export function legFlights(leg: FlightLegOut): string {
  if (leg.flight_numbers.length > 0) return leg.flight_numbers.join(" / ");
  return leg.segments
    .map((s) => s.flight_number ?? "")
    .filter(Boolean)
    .join(" / ");
}

/** Outbound airline of an offer, falling back to "Unknown airline". */
export function offerAirline(offer: FlightOfferOut): string {
  return legAirline(offer.outbound) || offer.airline || tr("Unknown airline", "항공사 정보 없음");
}

/** Outbound flight number(s) of an offer ("MM642"). */
export function offerFlights(offer: FlightOfferOut): string {
  return legFlights(offer.outbound) || offer.flight_number || "";
}

/** "Peach Aviation MM642" — the outbound airline and flight number(s). */
export function offerLabel(offer: FlightOfferOut): string {
  return `${offerAirline(offer)} ${offerFlights(offer)}`.trim();
}

/** "11월 27일 12:50 NRT → 15:35 ICN" (with "+1" when the flight lands the next day). */
export function legRoute(leg: FlightLegOut): string {
  const plusDays = dayDiff(leg.departure_time, leg.arrival_time);
  return (
    `${formatDate(leg.departure_time, false)} ${formatLocalTime(leg.departure_time)} ${leg.departure_airport}` +
    ` → ${formatLocalTime(leg.arrival_time)}${plusDays > 0 ? `+${plusDays}` : ""} ${leg.arrival_airport}`
  );
}

// ----------------------------------------------------------------------------- summary note

export type NoteTone = "neutral" | "good" | "warn";

export interface TripSummary {
  tone: NoteTone;
  /** The main sentence (cheapest flight, or what is going on). */
  title: string;
  /** Second sentence: distance to the target, or what to do next. */
  text: string | null;
  /** When it was checked and when the next check runs. */
  timing: string | null;
}

/**
 * Relative time for instants within a day ("3시간 전" / "3 hr. ago", "2시간 후" / "in 2 hr."),
 * otherwise the Seoul date and time. Both read naturally before "에" in Korean.
 */
export function whenPhrase(iso: string, now: number): string {
  const ms = parseUtc(iso).getTime();
  if (Number.isNaN(ms)) return iso;
  return Math.abs(ms - now) < DAY_MS ? formatRelative(iso, now) : formatUtc(iso);
}

/** The next scheduled check: a phrase, or `soon` when it is overdue; null when none is scheduled. */
function nextCheck(trip: TripOut, now: number): { phrase: string } | "soon" | null {
  if (!trip.active || !trip.next_run_at) return null;
  const ms = parseUtc(trip.next_run_at).getTime();
  if (!Number.isNaN(ms) && ms <= now) return "soon";
  return { phrase: whenPhrase(trip.next_run_at, now) };
}

/** "3시간 전에 확인했고, 2시간 후에 다시 확인해요." and its variants. */
export function timingText(trip: TripOut, now: number, checking: boolean): string | null {
  if (checking) return tr("Checking prices again now.", "지금 가격을 다시 확인하고 있어요.");
  const last = trip.last_checked_at ? whenPhrase(trip.last_checked_at, now) : null;
  const next = nextCheck(trip, now);
  const tail = !trip.active
    ? tr("Scheduled checks are paused.", "자동 확인은 일시 중지돼 있어요.")
    : next === "soon"
      ? tr("The next check runs soon.", "곧 다시 확인해요.")
      : next
        ? period(tr(`Next check ${next.phrase}`, `${next.phrase}에 다시 확인해요`))
        : null;

  if (!last) return tail;
  if (trip.last_status === "error") {
    const failed = tr(`The last check (${last}) failed.`, `마지막 확인(${last})은 실패했어요.`);
    return tail ? `${failed} ${tail}` : failed;
  }
  if (next && next !== "soon") {
    return period(tr(`Checked ${last}; next check ${next.phrase}`, `${last}에 확인했고, ${next.phrase}에 다시 확인해요`));
  }
  const checked = period(tr(`Checked ${last}`, `${last}에 확인했어요`));
  return tail ? `${checked} ${tail}` : checked;
}

/** What to do before the first check. */
function firstCheckText(trip: TripOut, now: number): string {
  if (!trip.active) {
    return tr(
      "Scheduled checks are paused. Use Check now to see current fares.",
      "자동 확인은 일시 중지돼 있어요. 지금 확인을 누르면 현재 요금을 볼 수 있어요.",
    );
  }
  const next = nextCheck(trip, now);
  if (next && next !== "soon") {
    return tr(
      `${period(`The first check runs ${next.phrase}`)} Use Check now to see fares right away.`,
      `${next.phrase}에 처음 확인해요. 지금 확인을 누르면 바로 볼 수 있어요.`,
    );
  }
  return tr("Use Check now to see current fares.", "지금 확인을 누르면 현재 요금을 볼 수 있어요.");
}

/** Distance between the lowest price (in the trip's basis) and the target. */
function targetText(trip: TripOut, value: number | null): { text: string | null; reached: boolean } {
  if (trip.target_price === null) {
    return { text: tr("No target price is set yet.", "목표 가격은 아직 정하지 않았어요."), reached: false };
  }
  if (value === null) return { text: null, reached: false };
  const perPerson = trip.price_basis === "per_person" && trip.adults > 1;
  const target = formatMoney(trip.target_price, trip.currency);
  const gap = trip.target_price - value;
  const amount = formatMoney(Math.abs(gap), trip.currency);
  const pp = perPerson ? tr(" per person", "") : "";
  const ppKo = perPerson ? "1인 기준 " : "";
  if (gap > 0) {
    return {
      text: tr(`That's ${amount} under your target of ${target}${pp}.`, `${ppKo}목표가 ${target}보다 ${amount} 저렴해요.`),
      reached: true,
    };
  }
  if (gap === 0) {
    return { text: tr(`That's exactly your target of ${target}${pp}.`, `${ppKo}목표가 ${target}에 닿았어요.`), reached: true };
  }
  return {
    text: tr(`${amount} to go until your target of ${target}${pp}.`, `${ppKo}목표가 ${target}까지 ${amount} 남았어요.`),
    reached: false,
  };
}

// ----------------------------------------------------------------------------- price history line

export interface PriceChange {
  dir: "down" | "up" | "flat";
  text: string;
}

export interface HistorySummary {
  tone: NoteTone;
  /** "지금 1인 최저가는 313,796원, 목표가 300,000원까지 13,796원 남았어요." */
  text: string;
  /** Latest found price vs the first check ("첫 확인보다 6.2% 올랐어요"), with two or more prices. */
  change: PriceChange | null;
}

/**
 * The one line above the price chart: the lowest price of the newest check in the price basis
 * and its distance to the target, and how it moved since the first check. When the newest
 * check found nothing it says so and names the last price found. Null without any price.
 */
export function historySummary(
  trip: TripOut,
  values: readonly (number | null)[],
  basis: PriceBasis,
  currency: string,
): HistorySummary | null {
  const found = values.filter((v): v is number => v !== null);
  if (found.length === 0) return null;
  const trend = priceTrend(values);

  let change: PriceChange | null = null;
  const first = found[0];
  const last = found[found.length - 1];
  if (found.length >= 2 && first !== 0) {
    const pct = ((last - first) / first) * 100;
    const abs = Math.abs(pct).toFixed(1);
    if (Math.abs(pct) < 0.05) change = { dir: "flat", text: tr("Same as the first check", "첫 확인과 같아요") };
    else if (pct < 0) change = { dir: "down", text: tr(`${abs}% lower than the first check`, `첫 확인보다 ${abs}% 내렸어요`) };
    else change = { dir: "up", text: tr(`${abs}% higher than the first check`, `첫 확인보다 ${abs}% 올랐어요`) };
  }

  if (trend.latest === null) {
    const money = formatMoney(trend.lastFound, currency);
    return {
      tone: "neutral",
      text: tr(
        `The latest check found no matching flights. The last lowest price was ${money}.`,
        `최근 확인에서는 조건에 맞는 항공편이 없었어요. 마지막 최저가는 ${money}이에요.`,
      ),
      change,
    };
  }

  const money = formatMoney(trend.latest, currency);
  const lowestEn = `Lowest now: ${money}${basis === "total" ? " in total" : " per person"}`;
  const lowestKo = basis === "total" ? `지금 최저가는 총액 ${money}` : `지금 1인 최저가는 ${money}`;
  const target = trip.target_price;
  if (target === null) {
    return {
      tone: "neutral",
      text: tr(`${lowestEn}. Set a target price to get alerts.`, `${lowestKo}이에요. 목표 가격을 정하면 알려드려요.`),
      change,
    };
  }
  const goal = formatMoney(target, currency);
  const gap = formatMoney(Math.abs(target - trend.latest), currency);
  if (trend.latest < target) {
    return {
      tone: "good",
      text: tr(`${lowestEn}, ${gap} under your ${goal} target.`, `${lowestKo}, 목표가 ${goal}보다 ${gap} 저렴해요.`),
      change,
    };
  }
  if (trend.latest === target) {
    return { tone: "good", text: tr(`${lowestEn}, exactly your target.`, `${lowestKo}, 목표가와 같아요.`), change };
  }
  return {
    tone: "neutral",
    text: tr(`${lowestEn}, ${gap} above your ${goal} target.`, `${lowestKo}, 목표가 ${goal}까지 ${gap} 남았어요.`),
    change,
  };
}

/**
 * The page summary: the cheapest matching flight now (airline + flight number, total for all
 * travellers), the distance to the target, and when it was / will be checked — or that no
 * check has run yet, or that the last one failed.
 */
export function tripSummary(
  trip: TripOut,
  best: FlightOfferOut | null,
  now: number,
  checking = false,
): TripSummary {
  const total = best?.price_total ?? trip.latest_min_price;
  const timing = timingText(trip, now, checking);

  if (total === null) {
    if (!trip.last_checked_at) {
      return checking
        ? {
            tone: "neutral",
            title: tr("Checking prices for the first time.", "처음으로 가격을 확인하고 있어요."),
            text: tr("This can take a minute.", "1분 정도 걸릴 수 있어요."),
            timing: null,
          }
        : {
            tone: "neutral",
            title: tr("No prices checked yet.", "아직 가격을 확인하지 않았어요."),
            text: firstCheckText(trip, now),
            timing: null,
          };
    }
    if (trip.last_status === "error" && !checking) {
      return {
        tone: "warn",
        title: tr("The last price check failed.", "마지막 가격 확인에 실패했어요."),
        text: tr("The error is shown below. Try again in a moment.", "아래에 오류 내용이 있어요. 잠시 후 다시 확인해 보세요."),
        timing: timingText({ ...trip, last_checked_at: null }, now, false),
      };
    }
    return {
      tone: "neutral",
      title: tr("No flights match your conditions yet.", "아직 조건에 맞는 항공편이 없어요."),
      text: tr(
        "Widening the time windows, stops or maximum price in the trip settings may help.",
        "여행 설정에서 시간대, 경유 조건, 최대 가격을 넓혀 보세요.",
      ),
      timing,
    };
  }

  const roundTrip = trip.trip_type === "round_trip";
  const money = formatMoney(total, best?.currency ?? trip.currency);
  const pax = formatAdults(trip.adults);
  const rtEn = roundTrip ? " round trip" : "";
  const rtKo = roundTrip ? "왕복 " : "";
  const title = best
    ? tr(
        `The cheapest matching flight is ${offerLabel(best)}: ${money}${rtEn} for ${pax}.`,
        `지금 가장 싼 항공편은 ${offerLabel(best)}, ${pax} ${rtKo}${money}이에요.`,
      )
    : tr(`The cheapest matching fare is ${money}${rtEn} for ${pax}.`, `지금 가장 싼 요금은 ${pax} ${rtKo}${money}이에요.`);

  const value = best
    ? trip.price_basis === "total"
      ? best.price_total
      : best.price_per_person
    : trip.latest_min_price_basis_value;
  const target = targetText(trip, value);
  const failed = trip.last_status === "error" && !checking;
  return {
    tone: failed ? "warn" : target.reached ? "good" : "neutral",
    title,
    text: target.text,
    timing,
  };
}
