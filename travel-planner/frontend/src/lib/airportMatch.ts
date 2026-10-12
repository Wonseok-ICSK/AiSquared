/** Pure helpers deciding which airport a typed autocomplete text stands for. */

import type { AirportOut } from "../api/types";

/** Whether `text` looks like an IATA airport/city code ("icn", "SEL"). */
export function looksLikeCode(text: string): boolean {
  return /^[A-Za-z]{3}$/.test(text.trim());
}

/**
 * The airport a typed text should commit to when the user leaves the box without picking.
 *
 * Order: an option whose code equals the text ("kix"); the highlighted option when `options`
 * were loaded for exactly this text (standard combobox Tab behaviour); a metro, then a single
 * airport, whose city or name equals the text ("Osaka" -> OSA); the only option. Returns null
 * when nothing fits, so the caller can fall back (plain 3-letter code, or revert).
 */
export function airportForText(
  text: string,
  options: readonly AirportOut[],
  activeIndex: number,
  optionsAreForText: boolean,
): AirportOut | null {
  const typed = text.trim();
  if (!typed || options.length === 0) return null;
  const upper = typed.toUpperCase();
  const byCode = options.find((a) => a.code === upper);
  if (byCode) return byCode;
  if (!optionsAreForText) return null;
  const highlighted = activeIndex >= 0 ? options[activeIndex] : undefined;
  if (highlighted) return highlighted;
  const lower = typed.toLowerCase();
  const named = options.filter(
    (a) => a.city.toLowerCase() === lower || a.city_ko === typed.trim() || a.name.toLowerCase() === lower,
  );
  const metro = named.find((a) => a.is_metro);
  if (metro) return metro;
  if (named.length === 1) return named[0];
  return options.length === 1 ? options[0] : null;
}
