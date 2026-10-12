/** Provider display names and stable chart colors (color follows the provider, never its rank). */

import { useCallback, useMemo } from "react";
import { useApp } from "./appContext";
import { humanize } from "./format";
import { providerName } from "./providerText";

const KNOWN_ORDER = ["google_flights", "naver", "serpapi", "mock"];
const SERIES_SLOTS = ["var(--series-2)", "var(--series-3)", "var(--series-4)", "var(--series-5)"];

/** Map provider key -> display name (Korean when known, else /api/meta), falling back to a humanized key. */
export function useProviderNames(): (key: string) => string {
  const { meta } = useApp();
  const names = useMemo(
    () => new Map((meta?.providers ?? []).map((p) => [p.key, providerName(p)])),
    [meta],
  );
  return useCallback((key: string) => names.get(key) ?? humanize(key), [names]);
}

/** Fixed provider order: registration order from /api/meta, then known keys, then others. */
export function useProviderOrder(): string[] {
  const { meta } = useApp();
  return useMemo(() => {
    const order = (meta?.providers ?? []).map((p) => p.key);
    for (const key of KNOWN_ORDER) if (!order.includes(key)) order.push(key);
    return order;
  }, [meta]);
}

/** CSS color for a provider series (slot 1 is reserved for "lowest overall"). */
export function providerColor(key: string, order: string[]): string {
  const index = order.indexOf(key);
  return index >= 0 && index < SERIES_SLOTS.length ? SERIES_SLOTS[index] : "var(--series-muted)";
}
