/** Small data hooks: async loading with race protection, polling, intervals, debouncing. */

import { useCallback, useEffect, useRef, useState } from "react";
import { isAbort } from "../api/client";

export interface AsyncState<T> {
  data: T | undefined;
  error: unknown;
  loading: boolean;
  /** Re-run the loader; keeps the previous data while loading (no flash). */
  reload: () => Promise<void>;
  setData: (value: T | ((prev: T | undefined) => T)) => void;
}

/**
 * Load data with `loader` whenever `deps` change. Out-of-order responses are discarded.
 * Errors are captured in `error` (aborts are ignored).
 */
export function useAsync<T>(loader: () => Promise<T>, deps: readonly unknown[]): AsyncState<T> {
  const [data, setDataState] = useState<T | undefined>(undefined);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const seq = useRef(0);
  const loaderRef = useRef(loader);
  loaderRef.current = loader;

  const run = useCallback(async () => {
    const id = ++seq.current;
    setLoading(true);
    try {
      const value = await loaderRef.current();
      if (id !== seq.current) return;
      setDataState(value);
      setError(null);
    } catch (err) {
      if (id !== seq.current || isAbort(err)) return;
      setError(err);
    } finally {
      if (id === seq.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    void run();
    return () => {
      seq.current++;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  const setData = useCallback((value: T | ((prev: T | undefined) => T)) => {
    setDataState((prev) =>
      typeof value === "function" ? (value as (p: T | undefined) => T)(prev) : value,
    );
  }, []);

  return { data, error, loading, reload: run, setData };
}

/** Call `callback` every `delayMs` while `delayMs` is not null. */
export function useInterval(callback: () => void, delayMs: number | null): void {
  const saved = useRef(callback);
  saved.current = callback;
  useEffect(() => {
    if (delayMs === null) return;
    const id = window.setInterval(() => saved.current(), delayMs);
    return () => window.clearInterval(id);
  }, [delayMs]);
}

/**
 * Poll `fetcher` every `intervalMs` while `active`; `onResult` receives each result and the
 * loop stops when it returns false. Requests never overlap.
 */
export function usePoll<T>(
  fetcher: (() => Promise<T>) | null,
  onResult: (value: T) => boolean,
  intervalMs: number,
  onError?: (err: unknown) => boolean,
): void {
  const fetchRef = useRef(fetcher);
  const resultRef = useRef(onResult);
  const errorRef = useRef(onError);
  fetchRef.current = fetcher;
  resultRef.current = onResult;
  errorRef.current = onError;
  const active = fetcher !== null;

  useEffect(() => {
    if (!active) return;
    let cancelled = false;
    let timer = 0;
    const tick = async () => {
      const fn = fetchRef.current;
      if (!fn || cancelled) return;
      let keepGoing = true;
      try {
        const value = await fn();
        if (cancelled) return;
        keepGoing = resultRef.current(value);
      } catch (err) {
        if (cancelled) return;
        keepGoing = errorRef.current ? errorRef.current(err) : true;
      }
      if (keepGoing && !cancelled) timer = window.setTimeout(tick, intervalMs);
    };
    timer = window.setTimeout(tick, intervalMs);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [active, intervalMs]);
}

/** `value` after it has been stable for `delayMs`. */
export function useDebounced<T>(value: T, delayMs: number): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const id = window.setTimeout(() => setDebounced(value), delayMs);
    return () => window.clearTimeout(id);
  }, [value, delayMs]);
  return debounced;
}

/** Re-render every `ms` (for relative times like "5 min ago"). */
export function useNow(ms = 30_000): number {
  const [now, setNow] = useState(() => Date.now());
  useInterval(() => setNow(Date.now()), ms);
  return now;
}

/** Set document.title for the current page. */
export function useTitle(title: string): void {
  useEffect(() => {
    document.title = title ? `${title} · AiSquared Travel` : "AiSquared Travel";
  }, [title]);
}
