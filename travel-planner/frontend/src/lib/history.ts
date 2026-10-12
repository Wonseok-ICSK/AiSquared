/** Pure helpers over GET /api/trips/{id}/price-history (one point per finished run). */

import type { PriceBasis, PriceHistoryOut } from "../api/types";

/** Run minima in a price basis, oldest first (null where the run had no matching offer). */
export function historyValues(history: PriceHistoryOut | undefined, basis: PriceBasis): (number | null)[] {
  if (!history) return [];
  return history.points.map((p) => (basis === "total" ? p.min_total : p.min_per_person));
}

export interface PriceTrend {
  /** Lowest matching price of the newest run (null: that run found no matching offer). */
  latest: number | null;
  /** Nearest earlier non-null run minimum, to compare `latest` with. */
  previous: number | null;
  /** Whether `previous` comes from the run right before the newest one. */
  previousIsAdjacent: boolean;
  /** Newest non-null run minimum, when `latest` is null ("last found"). */
  lastFound: number | null;
  /** How many runs ago `lastFound` was seen (1 = the run before the newest). */
  lastFoundRunsAgo: number | null;
}

/**
 * Trend of run minima for "Lowest now" cards. The newest run decides the current price, even
 * when it found nothing; an older price is only reported as `lastFound`, never as current.
 */
export function priceTrend(values: readonly (number | null)[]): PriceTrend {
  const n = values.length;
  const latest = n > 0 ? values[n - 1] : null;
  let prevIndex = -1;
  for (let i = n - 2; i >= 0; i--) {
    if (values[i] !== null) {
      prevIndex = i;
      break;
    }
  }
  const earlier = prevIndex >= 0 ? values[prevIndex] : null;
  return {
    latest,
    previous: latest !== null ? earlier : null,
    previousIsAdjacent: latest !== null && prevIndex === n - 2,
    lastFound: latest === null ? earlier : null,
    lastFoundRunsAgo: latest === null && prevIndex >= 0 ? n - 1 - prevIndex : null,
  };
}
