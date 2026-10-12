/** App-wide state: /api/meta (enums + defaults) and the unread notification count. */

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { api } from "../api/endpoints";
import type { MetaDefaults, MetaOut } from "../api/types";
import { DEFAULT_SCHEDULE_TIMES, STYLE_KEYS } from "../api/types";
import { useInterval } from "./hooks";

const UNREAD_POLL_MS = 30_000;

/** Defaults used until /api/meta answers (and if it fails). Mirrors schemas.MetaDefaults. */
export const FALLBACK_DEFAULTS: MetaDefaults = {
  currency: "KRW",
  timezone: "Asia/Seoul",
  adults: 1,
  cabin: "economy",
  trip_type: "round_trip",
  price_basis: "per_person",
  schedule_mode: "times",
  schedule_times: [...DEFAULT_SCHEDULE_TIMES],
  style_mode: "percent",
  style_weights: Object.fromEntries(STYLE_KEYS.map((k) => [k, 10])),
  score_style_weights: Object.fromEntries(STYLE_KEYS.map((k) => [k, 3])),
  budget_basis: "total",
  max_schedule_times: 6,
};

interface AppContextValue {
  meta: MetaOut | null;
  defaults: MetaDefaults;
  unread: number;
  refreshUnread: () => void;
}

const AppContext = createContext<AppContextValue>({
  meta: null,
  defaults: FALLBACK_DEFAULTS,
  unread: 0,
  refreshUnread: () => undefined,
});

/** Provides meta + unread count (polled every 30 s) to the app. */
export function AppProvider({ children }: { children: ReactNode }) {
  const [meta, setMeta] = useState<MetaOut | null>(null);
  const [unread, setUnread] = useState(0);

  useEffect(() => {
    api
      .meta()
      .then(setMeta)
      .catch(() => setMeta(null));
  }, []);

  const refreshUnread = useCallback(() => {
    api
      .unreadCount()
      .then((r) => setUnread(r.count))
      .catch(() => undefined);
  }, []);

  useEffect(() => refreshUnread(), [refreshUnread]);
  useInterval(refreshUnread, UNREAD_POLL_MS);

  const value = useMemo<AppContextValue>(
    () => ({ meta, defaults: meta?.defaults ?? FALLBACK_DEFAULTS, unread, refreshUnread }),
    [meta, unread, refreshUnread],
  );
  return <AppContext.Provider value={value}>{children}</AppContext.Provider>;
}

/** Access meta/defaults/unread state. */
export function useApp(): AppContextValue {
  return useContext(AppContext);
}
