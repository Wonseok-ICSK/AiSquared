/** Client-side validation primitives mirroring backend/app/schemas.py rules. */

import { tr } from "./i18n";

export type Errors = Record<string, string>;

const HHMM_RE = /^(\d{1,2}):(\d{2})$/;
const CODE_RE = /^[A-Z]{3}$/;

/** "8:05" -> "08:05"; null when not a valid 24h time. */
export function normalizeHHMM(value: string): string | null {
  const match = HHMM_RE.exec(value.trim());
  if (!match) return null;
  const h = Number(match[1]);
  const m = Number(match[2]);
  if (h > 23 || m > 59) return null;
  return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}`;
}

/** 3-letter IATA airport/metro code (after upper-casing). */
export function isIataCode(value: string): boolean {
  return CODE_RE.test(value.trim().toUpperCase());
}

/** ISO-4217 code (after upper-casing). */
export function isCurrencyCode(value: string): boolean {
  return CODE_RE.test(value.trim().toUpperCase());
}

/**
 * Parse a numeric text input: "" -> null, "1,500,000" -> 1500000, garbage -> NaN.
 * Thousands separators and spaces are ignored.
 */
export function parseNumber(text: string): number | null {
  const cleaned = text.replace(/[,\s_]/g, "");
  if (cleaned === "") return null;
  const value = Number(cleaned);
  return Number.isFinite(value) ? value : Number.NaN;
}

/** Text for a numeric input from a stored number (null -> ""). */
export function numberText(value: number | null | undefined): string {
  return value === null || value === undefined ? "" : String(value);
}

/** Validate an optional non-negative amount; returns an error message or null. */
export function checkMoney(text: string, required = false): string | null {
  const value = parseNumber(text);
  if (value === null) return required ? tr("Required", "필수 항목이에요") : null;
  if (Number.isNaN(value)) return tr("Enter a number", "숫자를 입력하세요");
  if (value < 0) return tr("Must be 0 or more", "0 이상이어야 해요");
  return null;
}

/** Budget: positive = cap, 0 or -1 = unlimited; other negatives rejected (DESIGN §3). */
export function checkBudget(text: string, required = false): string | null {
  const value = parseNumber(text);
  if (value === null) return required ? tr("Required (0 or -1 = unlimited)", "필수 항목이에요 (0 또는 -1 = 제한 없음)") : null;
  if (Number.isNaN(value)) return tr("Enter a number", "숫자를 입력하세요");
  if (value < 0 && value !== -1)
    return tr("Use a positive amount, or 0 / -1 for unlimited", "양수 금액을 입력하거나, 제한 없음은 0 / -1을 입력하세요");
  return null;
}

/** Optional HH:MM time; returns an error message or null. */
export function checkTime(text: string): string | null {
  if (!text.trim()) return null;
  return normalizeHHMM(text) ? null : tr("Use HH:MM (24h)", "HH:MM (24시간) 형식으로 입력하세요");
}

/** Max-length check for free text. */
export function checkLength(text: string, max: number): string | null {
  return text.length > max ? tr(`At most ${max} characters`, `최대 ${max}자까지 입력할 수 있어요`) : null;
}

/** Collect non-null messages into an Errors map. */
export function collect(entries: [string, string | null][]): Errors {
  const errors: Errors = {};
  for (const [key, msg] of entries) if (msg) errors[key] = msg;
  return errors;
}

/** True when there are no errors. */
export function isValid(errors: Errors): boolean {
  return Object.keys(errors).length === 0;
}
