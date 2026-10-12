/** Pure helpers for the home page: greeting by Seoul time, the composer's query string and
 * suggestion dates, the watched-trips summary sentence and the target-price line.
 * No DOM or API access, so they can be unit tested (tests/dashboard.test.ts). */

import type { PriceBasis, TripOut, TripType } from "../../api/types";
import { APP_TZ, formatMoney } from "../../lib/format";
import { tr } from "../../lib/i18n";

// ----------------------------------------------------------------------------- greeting

export type DayPart = "morning" | "afternoon" | "evening";

/** Hour of day (0–23) in Asia/Seoul for an epoch-ms instant. */
export function seoulHour(now: number): number {
  const hour = new Intl.DateTimeFormat("en-US", { timeZone: APP_TZ, hour: "numeric", hourCycle: "h23" }).format(
    new Date(now),
  );
  return Number(hour) % 24;
}

/** Morning 05–11, afternoon 12–17, evening otherwise (late night included). */
export function dayPart(hour: number): DayPart {
  if (hour >= 5 && hour < 12) return "morning";
  if (hour >= 12 && hour < 18) return "afternoon";
  return "evening";
}

/** "좋은 아침이에요" / "Good morning" and so on. */
export function greeting(part: DayPart): string {
  if (part === "morning") return tr("Good morning", "좋은 아침이에요");
  if (part === "afternoon") return tr("Good afternoon", "좋은 오후예요");
  return tr("Good evening", "좋은 저녁이에요");
}

// ----------------------------------------------------------------------------- dates

function parseDay(iso: string): Date | null {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso);
  return m ? new Date(Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3]))) : null;
}

function toDay(date: Date): string {
  return date.toISOString().slice(0, 10);
}

/** "YYYY-MM-DD" plus `days` (calendar arithmetic, no time zone involved). */
export function addDays(iso: string, days: number): string {
  const date = parseDay(iso);
  if (!date) return iso;
  date.setUTCDate(date.getUTCDate() + days);
  return toDay(date);
}

/** Day of week of a "YYYY-MM-DD" date (0 = Sunday … 6 = Saturday). */
export function weekday(iso: string): number {
  return parseDay(iso)?.getUTCDay() ?? 0;
}

/** Nights between two dates (negative when `to` is before `from`; 0 if either is invalid). */
export function nightsBetween(from: string, to: string): number {
  const a = parseDay(from);
  const b = parseDay(to);
  if (!a || !b) return 0;
  return Math.round((b.getTime() - a.getTime()) / 86_400_000);
}

/** The coming Friday (a week later when today is Friday or the weekend has started). */
export function nextFriday(today: string): string {
  const ahead = (5 - weekday(today) + 7) % 7;
  return addDays(today, ahead === 0 ? 7 : ahead);
}

// ----------------------------------------------------------------------------- composer

/** What the home composer holds; empty strings are fields left blank. */
export interface ComposerValues {
  origin: string;
  destination: string;
  depart: string;
  returnDate: string;
  adults: number | null;
}

/** Round trip when a return date is set; one way when only the departure is; else unknown. */
export function composerTripType(values: Pick<ComposerValues, "depart" | "returnDate">): TripType | null {
  if (values.returnDate) return "round_trip";
  if (values.depart) return "one_way";
  return null;
}

/**
 * Query string for /trips/new (the trip form reads exactly these names): origin, destination
 * (IATA codes), depart, return (YYYY-MM-DD), adults and trip_type. Blank fields are left out.
 */
export function composerQuery(values: ComposerValues): string {
  const params = new URLSearchParams();
  const origin = values.origin.trim().toUpperCase();
  const destination = values.destination.trim().toUpperCase();
  if (origin) params.set("origin", origin);
  if (destination) params.set("destination", destination);
  if (values.depart) params.set("depart", values.depart);
  if (values.returnDate) params.set("return", values.returnDate);
  if (values.adults !== null) params.set("adults", String(values.adults));
  const tripType = composerTripType(values);
  if (tripType) params.set("trip_type", tripType);
  return params.toString();
}

/** Travellers the app supports (1 or 2). */
export function clampAdults(n: number): 1 | 2 {
  return n >= 2 ? 2 : 1;
}

/** "왕복 · 3박 4일" / "Round trip · 3 nights" style line under the composer fields. */
export function composerHint(values: Pick<ComposerValues, "depart" | "returnDate">): string {
  const { depart, returnDate } = values;
  if (depart && returnDate) {
    const nights = nightsBetween(depart, returnDate);
    if (nights <= 0) return tr("Round trip · same day", "왕복 · 당일");
    return tr(
      `Round trip · ${nights} night${nights === 1 ? "" : "s"}`,
      `왕복 · ${nights}박 ${nights + 1}일`,
    );
  }
  if (depart) return tr("One way · add a return date for a round trip", "편도 · 오는 날을 고르면 왕복이에요");
  if (returnDate) return tr("Round trip", "왕복");
  return tr("Anything left blank can be filled in next", "비워 둔 칸은 다음 화면에서 채울 수 있어요");
}

export type SuggestionIcon = "weekend" | "food" | "sea" | "palm";

/** A suggestion chip that fills the composer. */
export interface Suggestion {
  key: string;
  label: string;
  icon: SuggestionIcon;
  origin: string;
  destination: string;
  depart: string;
  returnDate: string;
}

/** Suggestion chips with dates relative to `today` (YYYY-MM-DD, Seoul). */
export function suggestions(today: string): Suggestion[] {
  const friday = nextFriday(today);
  const osaka = addDays(today, 21);
  const fukuoka = addDays(today, 14);
  const danang = addDays(today, 42);
  return [
    {
      key: "tokyo-weekend",
      label: tr("Tokyo weekend", "도쿄 주말 여행"),
      icon: "weekend",
      origin: "ICN",
      destination: "TYO",
      depart: friday,
      returnDate: addDays(friday, 2),
    },
    {
      key: "osaka-3n",
      label: tr("Osaka, 3 nights", "오사카 3박 4일"),
      icon: "food",
      origin: "ICN",
      destination: "OSA",
      depart: osaka,
      returnDate: addDays(osaka, 3),
    },
    {
      key: "fukuoka-2n",
      label: tr("Fukuoka, 2 nights", "후쿠오카 2박 3일"),
      icon: "sea",
      origin: "ICN",
      destination: "FUK",
      depart: fukuoka,
      returnDate: addDays(fukuoka, 2),
    },
    {
      key: "danang-4n",
      label: tr("Da Nang, 4 nights", "다낭 4박 5일"),
      icon: "palm",
      origin: "ICN",
      destination: "DAD",
      depart: danang,
      returnDate: addDays(danang, 4),
    },
  ];
}

/** Most frequent origin among the watched trips (ties: the first seen), or null. */
export function mostCommonOrigin(trips: readonly Pick<TripOut, "origin">[]): string | null {
  const counts = new Map<string, number>();
  let best: string | null = null;
  let bestCount = 0;
  for (const { origin } of trips) {
    const n = (counts.get(origin) ?? 0) + 1;
    counts.set(origin, n);
    if (n > bestCount) {
      best = origin;
      bestCount = n;
    }
  }
  return best;
}

// ----------------------------------------------------------------------------- target

export interface TargetStatus {
  /** The fare is at or below the target. */
  reached: boolean;
  /** target − current (positive = below target by that much). */
  diff: number;
}

/** Where the current price stands against the target (null without both values). */
export function targetStatus(current: number | null, target: number | null): TargetStatus | null {
  if (current === null || target === null || target <= 0 || current <= 0) return null;
  return { reached: current <= target, diff: target - current };
}

/**
 * The target price as one plain line: "목표가 300,000원 · 13,796원 남음", "목표가 300,000원보다
 * 22,000원 낮아요", "목표가 300,000원에 닿았어요", or just "목표가 300,000원" without a current
 * fare. Null when no target is set.
 */
export function targetLine(
  current: number | null,
  target: number | null,
  currency: string,
): { text: string; reached: boolean } | null {
  if (target === null || target <= 0) return null;
  const goal = formatMoney(target, currency);
  const status = targetStatus(current, target);
  if (!status) return { text: tr(`Target ${goal}`, `목표가 ${goal}`), reached: false };
  const gap = formatMoney(Math.abs(status.diff), currency);
  if (!status.reached) return { text: tr(`Target ${goal} · ${gap} to go`, `목표가 ${goal} · ${gap} 남음`), reached: false };
  if (status.diff === 0) return { text: tr(`At your ${goal} target`, `목표가 ${goal}에 닿았어요`), reached: true };
  return { text: tr(`${gap} below your ${goal} target`, `목표가 ${goal}보다 ${gap} 낮아요`), reached: true };
}

// ----------------------------------------------------------------------------- summary

export type SummaryTone = "neutral" | "good" | "warn";

export interface WatchSummary {
  text: string;
  tone: SummaryTone;
}

function basisPhrase(basis: PriceBasis): string {
  return basis === "per_person" ? tr("per person", "1인 기준") : tr("in total", "총액 기준");
}

function current(t: TripOut): number | null {
  return t.latest_min_price_basis_value;
}

/** Sentence about how many trips are watched (and how many of their last checks failed). */
function watchingSentence(trips: readonly TripOut[], active: readonly TripOut[], failed: number): string {
  const n = trips.length;
  const m = active.length;
  if (n === 1) {
    const name = trips[0].name;
    if (m === 0) return tr(`Checks for “${name}” are paused.`, `‘${name}’ 가격 확인을 잠시 멈춰 두었어요.`);
    return failed > 0
      ? tr(`I'm watching “${name}”, but its last check failed.`, `‘${name}’ 가격을 지켜보고 있는데, 마지막 확인에 실패했어요.`)
      : tr(`I'm watching fares for “${name}”.`, `‘${name}’ 가격을 지켜보고 있어요.`);
  }
  if (m === 0) return tr(`All ${n} trips are paused.`, `여행 ${n}개 모두 가격 확인을 멈춰 두었어요.`);
  const failedEn = failed > 0 ? `; the last check failed for ${failed}` : "";
  const failedKo = failed > 0 ? `있고, ${failed}개는 마지막 확인에 실패했어요` : "있어요";
  if (m === n) return tr(`I'm watching ${n} trips${failedEn}.`, `여행 ${n}개를 지켜보고 ${failedKo}.`);
  return tr(`I'm watching ${m} of your ${n} trips${failedEn}.`, `여행 ${n}개 중 ${m}개를 지켜보고 ${failedKo}.`);
}

/** Sentence about the target prices of the active trips (null when there is nothing to say). */
function targetSentence(single: boolean, active: readonly TripOut[]): { text: string; good: boolean } | null {
  const priced = active.filter((t) => current(t) !== null);
  const targeted = priced.filter((t) => t.target_price !== null && t.target_price > 0);
  const relGap = (t: TripOut) => ((current(t) as number) - (t.target_price as number)) / (t.target_price as number);

  const good = targeted.filter((t) => (current(t) as number) <= (t.target_price as number)).sort((a, b) => relGap(a) - relGap(b));
  if (good.length > 0) {
    const t = good[0];
    const diff = (t.target_price as number) - (current(t) as number);
    const k = good.length;
    const moreEn = k > 1 ? ` (${k} trips are at or below target)` : "";
    const moreKo = k > 1 ? ` (목표가 이하 ${k}개)` : "";
    const money = formatMoney(diff, t.currency);
    const text =
      diff > 0
        ? tr(
            `“${t.name}” is ${money} ${basisPhrase(t.price_basis)} below its target${moreEn}.`,
            `‘${t.name}’ 가격이 목표가보다 ${basisPhrase(t.price_basis)} ${money} 낮아요${moreKo}.`,
          )
        : tr(`“${t.name}” has reached its target${moreEn}.`, `‘${t.name}’ 가격이 목표가에 닿았어요${moreKo}.`);
    return { text, good: true };
  }

  if (targeted.length > 0) {
    const t = [...targeted].sort((a, b) => relGap(a) - relGap(b))[0];
    const money = formatMoney((current(t) as number) - (t.target_price as number), t.currency);
    const basis = basisPhrase(t.price_basis);
    const text = single
      ? tr(`The fare is ${money} ${basis} above your target.`, `목표가까지 ${basis} ${money} 남았어요.`)
      : tr(
          `Closest to its target is “${t.name}”, ${money} ${basis} away.`,
          `목표가에 가장 가까운 여행은 ‘${t.name}’, ${basis} ${money} 남았어요.`,
        );
    return { text, good: false };
  }

  if (priced.length > 0) {
    return {
      text: tr(
        "Set a target price and I'll let you know when a fare drops below it.",
        "목표가를 정해 두면 그 아래로 내려갈 때 알려 드릴게요.",
      ),
      good: false,
    };
  }
  if (active.some((t) => t.last_checked_at && t.last_status !== "error")) {
    return {
      text: tr("The latest checks found no matching flights.", "최근 확인에서는 조건에 맞는 항공권을 찾지 못했어요."),
      good: false,
    };
  }
  if (active.every((t) => !t.last_checked_at)) {
    return {
      text: tr("I'll sum things up here after the first price check.", "첫 가격 확인이 끝나면 여기서 정리해 드릴게요."),
      good: false,
    };
  }
  return null;
}

/**
 * One or two plain sentences about all watched trips: how many are watched (and failed), then
 * the trip at or below its target — or the one closest to it — and by how much.
 */
export function watchSummary(trips: readonly TripOut[]): WatchSummary | null {
  if (trips.length === 0) return null;
  const active = trips.filter((t) => t.active);
  const failed = active.filter((t) => t.last_status === "error").length;
  const first = watchingSentence(trips, active, failed);
  if (active.length === 0) {
    const resume = tr(
      trips.length === 1 ? "Turn it back on to resume checks." : "Turn one back on to resume checks.",
      "다시 켜면 가격 확인을 이어 갈게요.",
    );
    return { text: `${first} ${resume}`, tone: "neutral" };
  }
  const second = targetSentence(trips.length === 1, active);
  const tone: SummaryTone = second?.good ? "good" : failed > 0 ? "warn" : "neutral";
  return { text: second ? `${first} ${second.text}` : first, tone };
}
