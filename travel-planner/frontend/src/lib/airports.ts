/** Airport/city name lookup with a shared in-memory cache (uses GET /api/airports?q=CODE). */

import { useEffect, useState } from "react";
import { api } from "../api/endpoints";
import type { AirportOut } from "../api/types";
import { getLang } from "./i18n";

const cache = new Map<string, AirportOut | null>();
const inflight = new Map<string, Promise<AirportOut | null>>();

/** Remember airports returned by an autocomplete query. */
export function rememberAirports(list: AirportOut[]): void {
  for (const airport of list) cache.set(airport.code, airport);
}

/** Resolve an IATA airport or metro code to its AirportOut (null when unknown). */
export function lookupAirport(code: string): Promise<AirportOut | null> {
  const key = code.trim().toUpperCase();
  if (!/^[A-Z]{3}$/.test(key)) return Promise.resolve(null);
  if (cache.has(key)) return Promise.resolve(cache.get(key) ?? null);
  const pending = inflight.get(key);
  if (pending) return pending;
  const promise = api
    .airports(key)
    .then((list) => {
      const exact = list.find((a) => a.code === key) ?? null;
      cache.set(key, exact);
      return exact;
    })
    .catch(() => null)
    .finally(() => inflight.delete(key));
  inflight.set(key, promise);
  return promise;
}

/** Cached AirportOut for `code` (undefined while loading, null when unknown). */
export function useAirport(code: string | null | undefined): AirportOut | null | undefined {
  const key = code?.toUpperCase() ?? "";
  const [airport, setAirport] = useState<AirportOut | null | undefined>(() =>
    cache.has(key) ? cache.get(key) : undefined,
  );
  useEffect(() => {
    if (!key) {
      setAirport(null);
      return;
    }
    let alive = true;
    if (cache.has(key)) setAirport(cache.get(key));
    else {
      setAirport(undefined);
      void lookupAirport(key).then((a) => alive && setAirport(a));
    }
    return () => {
      alive = false;
    };
  }, [key]);
  return airport;
}

/**
 * City name for display: Korean ("도쿄") in the Korean UI when known, else the English city
 * ("Tokyo"); metros use their city, airports their city or name.
 */
export function cityName(airport: AirportOut | null | undefined): string | null {
  if (!airport) return null;
  return (getLang() === "ko" ? airport.city_ko : null) || airport.city || airport.name || null;
}
