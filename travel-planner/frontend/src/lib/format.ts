/**
 * Formatting helpers, in the current UI language (./i18n).
 *
 * - Money: Korean shows KRW as "320,000원", English as "₩320,000"; other currencies use
 *   Intl currency formatting (KRW/JPY have no minor unit).
 * - API timestamps ("...Z", UTC) are shown in Asia/Seoul.
 * - Flight/itinerary times are already local ("YYYY-MM-DDTHH:MM") and are shown as-is:
 *   they are parsed field-by-field and formatted in UTC so the browser's zone never applies.
 */

import type {
  AlertKind,
  Cabin,
  Companion,
  ItineraryStatus,
  Pace,
  PriceBasis,
  RunStatus,
  StyleKey,
} from "../api/types";
import { getLang, labels, tr } from "./i18n";

export const APP_TZ = "Asia/Seoul";
const DASH = "—";

const ZERO_DECIMAL = new Set(["KRW", "JPY", "VND", "IDR", "TWD", "CLP", "ISK", "HUF"]);

/** BCP 47 locale of the current UI language. */
export function uiLocale(): string {
  return getLang() === "ko" ? "ko-KR" : "en-US";
}

const intlCache = new Map<string, Intl.NumberFormat | Intl.DateTimeFormat | Intl.RelativeTimeFormat>();

/** Intl formatter cached per language + key. */
function cached<T extends Intl.NumberFormat | Intl.DateTimeFormat | Intl.RelativeTimeFormat>(
  key: string,
  make: (locale: string) => T,
): T {
  const full = `${getLang()}|${key}`;
  let fmt = intlCache.get(full) as T | undefined;
  if (!fmt) {
    fmt = make(uiLocale());
    intlCache.set(full, fmt);
  }
  return fmt;
}

function moneyFormatter(currency: string, decimals: number): Intl.NumberFormat | null {
  try {
    return cached(`money|${currency}|${decimals}`, (locale) =>
      new Intl.NumberFormat(locale, {
        style: "currency",
        currency,
        minimumFractionDigits: 0,
        maximumFractionDigits: decimals,
      }),
    );
  } catch {
    return null;
  }
}

/** Fraction digits shown for a currency (0 for KRW/JPY and other zero-decimal currencies). */
export function currencyDecimals(currency: string): number {
  return ZERO_DECIMAL.has(currency.toUpperCase()) ? 0 : 2;
}

/** "320,000원" (Korean) / "₩320,000" (English) — or "—" for null/undefined. */
export function formatMoney(amount: number | null | undefined, currency: string): string {
  if (amount === null || amount === undefined || Number.isNaN(amount)) return DASH;
  const code = (currency || "KRW").toUpperCase();
  if (code === "KRW" && getLang() === "ko") return `${formatNumber(amount)}원`;
  const fmt = moneyFormatter(code, currencyDecimals(code));
  return fmt ? fmt.format(amount) : `${code} ${formatNumber(amount)}`;
}

/** Signed money delta: "+12,000원" / "−₩12,000" / "±₩0". */
export function formatMoneyDelta(delta: number, currency: string): string {
  if (delta === 0) return `±${formatMoney(0, currency)}`;
  const sign = delta > 0 ? "+" : "−";
  return `${sign}${formatMoney(Math.abs(delta), currency)}`;
}

/** Plain grouped number "320,000" (axis ticks, counts). */
export function formatNumber(value: number | null | undefined, decimals = 0): string {
  if (value === null || value === undefined || Number.isNaN(value)) return DASH;
  return cached(`num|${decimals}`, () => new Intl.NumberFormat("ko-KR", { maximumFractionDigits: decimals })).format(value);
}

/** Short number for chart axes: "29.5만" (Korean) / "295K" (English). */
export function formatCompactNumber(value: number): string {
  return cached("compact", (locale) =>
    new Intl.NumberFormat(locale, { notation: "compact", maximumFractionDigits: 1 }),
  ).format(value);
}

/** Budget value: null = "Not set", 0/-1 = "Unlimited", positive = money. */
export function formatBudget(value: number | null | undefined, currency: string): string {
  if (value === null || value === undefined) return tr("Not set", "미설정");
  if (value === 0 || value === -1) return tr("Unlimited", "제한 없음");
  return formatMoney(value, currency);
}

// ----------------------------------------------------------------------------- UTC timestamps

/** Parse an API UTC timestamp ("...Z"; a missing zone is treated as UTC). */
export function parseUtc(iso: string): Date {
  const hasZone = /[zZ]$|[+-]\d{2}:?\d{2}$/.test(iso);
  return new Date(hasZone ? iso : `${iso}Z`);
}

function dateTimeFormat(key: string, options: Intl.DateTimeFormatOptions): Intl.DateTimeFormat {
  return cached(`dt|${key}`, (locale) => new Intl.DateTimeFormat(locale, options));
}

const seoulDateTime = () =>
  dateTimeFormat("seoulDateTime", {
    timeZone: APP_TZ,
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  });
const seoulDateTimeYear = () =>
  dateTimeFormat("seoulDateTimeYear", {
    timeZone: APP_TZ,
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  });
const seoulShortDate = () => dateTimeFormat("seoulShortDate", { timeZone: APP_TZ, month: "short", day: "numeric" });
const seoulYear = () => dateTimeFormat("seoulYear", { timeZone: APP_TZ, year: "numeric" });
const seoulTime = () =>
  dateTimeFormat("seoulTime", { timeZone: APP_TZ, hour: "2-digit", minute: "2-digit", hourCycle: "h23" });

/** "10월 6일 17:30" / "Oct 6, 17:30" in Asia/Seoul (year added when not the current year). */
export function formatUtc(iso: string | null | undefined): string {
  if (!iso) return DASH;
  const date = parseUtc(iso);
  if (Number.isNaN(date.getTime())) return iso;
  const sameYear = seoulYear().format(date) === seoulYear().format(new Date());
  return (sameYear ? seoulDateTime() : seoulDateTimeYear()).format(date);
}

/** "10월 6일" / "Oct 6" for an epoch-ms instant, in Asia/Seoul. */
export function formatSeoulDay(ms: number): string {
  return seoulShortDate().format(new Date(ms));
}

/** "17:30" for an epoch-ms instant, in Asia/Seoul. */
export function formatSeoulTime(ms: number): string {
  return seoulTime().format(new Date(ms));
}

/** "10월 6일" / "Oct 6" in Asia/Seoul. */
export function formatUtcShortDate(iso: string): string {
  const date = parseUtc(iso);
  return Number.isNaN(date.getTime()) ? iso : seoulShortDate().format(date);
}

/** "5분 전" / "5 min. ago", "3시간 후" / "in 3 hr." relative to now. */
export function formatRelative(iso: string | null | undefined, now: number = Date.now()): string {
  if (!iso) return DASH;
  const ms = parseUtc(iso).getTime() - now;
  if (Number.isNaN(ms)) return iso;
  const abs = Math.abs(ms);
  const minute = 60_000;
  const hour = 60 * minute;
  const day = 24 * hour;
  if (abs < minute) return ms < 0 ? tr("just now", "방금 전") : tr("in <1 min", "1분 이내");
  const relative = cached("relative", (locale) => new Intl.RelativeTimeFormat(locale, { numeric: "auto", style: "short" }));
  if (abs < hour) return relative.format(Math.round(ms / minute), "minute");
  if (abs < day) return relative.format(Math.round(ms / hour), "hour");
  return relative.format(Math.round(ms / day), "day");
}

/** Elapsed seconds between two UTC timestamps (or until now). */
export function elapsedSeconds(startIso: string, endIso?: string | null): number {
  const end = endIso ? parseUtc(endIso).getTime() : Date.now();
  return Math.max(0, (end - parseUtc(startIso).getTime()) / 1000);
}

/** "42초" / "42 s", "3분 05초" / "3 min 05 s". */
export function formatSeconds(seconds: number): string {
  if (seconds < 60) {
    const s = seconds.toFixed(seconds < 10 ? 1 : 0);
    return tr(`${s} s`, `${s}초`);
  }
  const m = Math.floor(seconds / 60);
  const s = String(Math.round(seconds % 60)).padStart(2, "0");
  return tr(`${m} min ${s} s`, `${m}분 ${s}초`);
}

// ----------------------------------------------------------------------------- local (naive) dates/times

interface LocalParts {
  y: number;
  m: number;
  d: number;
  hh: number;
  mm: number;
}

/** Parse "YYYY-MM-DD[THH:MM[:SS]]" without any timezone conversion. */
export function parseLocal(value: string): LocalParts | null {
  const match = /^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2}))?/.exec(value);
  if (!match) return null;
  return {
    y: Number(match[1]),
    m: Number(match[2]),
    d: Number(match[3]),
    hh: match[4] ? Number(match[4]) : 0,
    mm: match[5] ? Number(match[5]) : 0,
  };
}

function utcDateOf(parts: LocalParts): Date {
  return new Date(Date.UTC(parts.y, parts.m - 1, parts.d, parts.hh, parts.mm));
}

const localDate = () =>
  dateTimeFormat("localDate", { timeZone: "UTC", weekday: "short", month: "short", day: "numeric" });
const localDateNoWeekday = () => dateTimeFormat("localDateNoWeekday", { timeZone: "UTC", month: "short", day: "numeric" });
const localDateLong = () =>
  dateTimeFormat("localDateLong", { timeZone: "UTC", weekday: "long", year: "numeric", month: "long", day: "numeric" });

/** "11월 20일 (금)" / "Fri, Nov 20" for "2026-11-20" (or the date part of a local datetime). */
export function formatDate(value: string | null | undefined, withWeekday = true): string {
  if (!value) return DASH;
  const parts = parseLocal(value);
  if (!parts) return value;
  return (withWeekday ? localDate() : localDateNoWeekday()).format(utcDateOf(parts));
}

/** "2026년 11월 20일 금요일" / "Friday, November 20, 2026". */
export function formatDateLong(value: string): string {
  const parts = parseLocal(value);
  return parts ? localDateLong().format(utcDateOf(parts)) : value;
}

/** "11월 20일 – 11월 27일" / "Nov 20 – Nov 27" (or a single date for one-way). */
export function formatDateRange(start: string, end: string | null | undefined): string {
  const a = formatDate(start, false);
  return end ? `${a} – ${formatDate(end, false)}` : a;
}

/** "09:05" from a local ISO datetime (shown as-is). */
export function formatLocalTime(value: string | null | undefined): string {
  if (!value) return DASH;
  const parts = parseLocal(value);
  if (!parts) return value;
  return `${String(parts.hh).padStart(2, "0")}:${String(parts.mm).padStart(2, "0")}`;
}

/** "11월 20일 (금) 09:05" / "Fri, Nov 20 09:05" from a local ISO datetime. */
export function formatLocalDateTime(value: string | null | undefined): string {
  if (!value) return DASH;
  return `${formatDate(value)} ${formatLocalTime(value)}`;
}

/** Whole days between the dates of two local datetimes ("+1" next-day arrivals). */
export function dayDiff(from: string, to: string): number {
  const a = parseLocal(from);
  const b = parseLocal(to);
  if (!a || !b) return 0;
  const ms = Date.UTC(b.y, b.m - 1, b.d) - Date.UTC(a.y, a.m - 1, a.d);
  return Math.round(ms / 86_400_000);
}

/** Today's date "YYYY-MM-DD" in Asia/Seoul. */
export function todaySeoul(): string {
  return new Intl.DateTimeFormat("en-CA", { timeZone: APP_TZ }).format(new Date());
}

// ----------------------------------------------------------------------------- misc

/** "2시간 25분" / "2h 25m", "45분" / "45m". */
export function formatDuration(minutes: number | null | undefined): string {
  if (minutes === null || minutes === undefined) return DASH;
  const h = Math.floor(minutes / 60);
  const m = Math.round(minutes % 60);
  if (h === 0) return tr(`${m}m`, `${m}분`);
  if (m === 0) return tr(`${h}h`, `${h}시간`);
  const mm = String(m).padStart(2, "0");
  return tr(`${h}h ${mm}m`, `${h}시간 ${mm}분`);
}

/** "직항" / "Direct", "경유 1회" / "1 stop", "경유 2회" / "2 stops". */
export function formatStops(stops: number): string {
  if (stops <= 0) return tr("Direct", "직항");
  return tr(stops === 1 ? "1 stop" : `${stops} stops`, `경유 ${stops}회`);
}

/** "18분" / "18 min" for itinerary hops. */
export function formatMinutes(minutes: number): string {
  if (minutes < 60) {
    const m = Math.round(minutes);
    return tr(`${m} min`, `${m}분`);
  }
  return formatDuration(minutes);
}

export const CABIN_LABEL = labels<Cabin>({
  economy: ["Economy", "일반석"],
  premium_economy: ["Premium economy", "프리미엄 일반석"],
  business: ["Business", "비즈니스석"],
  first: ["First", "일등석"],
});

export const BASIS_LABEL = labels<PriceBasis>({
  per_person: ["per person", "1인"],
  total: ["total", "총액"],
});

export const ALERT_KIND_LABEL = labels<AlertKind>({
  below_target: ["Below target", "목표가 이하"],
  price_drop: ["Price drop", "가격 하락"],
  new_low: ["New lowest price", "신규 최저가"],
});

export const RUN_STATUS_LABEL = labels<RunStatus>({
  running: ["Running", "검색 중"],
  ok: ["OK", "성공"],
  partial: ["Partial", "일부 성공"],
  error: ["Error", "오류"],
});

export const ITINERARY_STATUS_LABEL = labels<ItineraryStatus>({
  pending: ["Queued", "대기 중"],
  running: ["Generating", "생성 중"],
  done: ["Ready", "완료"],
  error: ["Failed", "실패"],
});

export const COMPANION_LABEL = labels<Companion>({
  solo: ["Solo", "혼자"],
  couple: ["Couple", "커플"],
  family: ["Family", "가족"],
});

export const PACE_LABEL = labels<Pace>({
  relaxed: ["Relaxed", "여유롭게"],
  moderate: ["Moderate", "보통"],
  packed: ["Packed", "빡빡하게"],
});

export const STYLE_LABEL = labels<StyleKey>({
  sightseeing: ["Sightseeing", "관광"],
  food: ["Food", "맛집"],
  relaxation: ["Relaxation", "휴식"],
  hotel_resort: ["Hotel / resort", "호텔·리조트"],
  shopping: ["Shopping", "쇼핑"],
  nightlife: ["Nightlife", "나이트라이프"],
  nature: ["Nature", "자연"],
  photography: ["Photography", "사진"],
  culture: ["Culture", "문화"],
  local_experience: ["Local experiences", "현지 체험"],
});

/** "성인 2명" / "2 adults", "성인 1명" / "1 adult". */
export function formatAdults(n: number): string {
  return tr(n === 1 ? "1 adult" : `${n} adults`, `성인 ${n}명`);
}

/** Humanize a snake_case key: "google_flights" -> "Google flights". */
export function humanize(key: string): string {
  const text = key.replace(/_/g, " ");
  return text.charAt(0).toUpperCase() + text.slice(1);
}

/** Price in a basis from a per-person value. */
export function inBasis(perPerson: number, adults: number, basis: PriceBasis): number {
  return basis === "total" ? perPerson * adults : perPerson;
}
