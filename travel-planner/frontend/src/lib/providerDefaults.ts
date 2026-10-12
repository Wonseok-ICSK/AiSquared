/** Pure helpers for the "Use for trips by default" provider switches (runtime default_providers). */

import type { ProviderInfo } from "../api/types";

/**
 * The `default_providers` list after switching `key` on or off, or null when the change would
 * leave it empty: the backend reads `[]` as "every provider enabled by default" (DESIGN §8),
 * so switching off the last default source would silently turn the built-in ones back on.
 */
export function nextDefaultProviders(
  providers: readonly Pick<ProviderInfo, "key" | "enabled">[],
  key: string,
  on: boolean,
): string[] | null {
  const current = providers.filter((p) => p.enabled).map((p) => p.key);
  const next = on ? [...new Set([...current, key])] : current.filter((k) => k !== key);
  return next.length > 0 ? next : null;
}

/** Whether `key` is the only provider still used by default (its switch must stay on). */
export function isLastDefault(providers: readonly Pick<ProviderInfo, "key" | "enabled">[], key: string): boolean {
  const enabled = providers.filter((p) => p.enabled);
  return enabled.length === 1 && enabled[0].key === key;
}
