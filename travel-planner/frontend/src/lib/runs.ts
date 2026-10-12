/** Tracking of a search run started with "Check now" (polls GET /api/runs/{id}). */

import { useCallback, useRef, useState } from "react";
import { api } from "../api/endpoints";
import type { SearchRunOut } from "../api/types";
import { usePoll } from "./hooks";

const RUN_POLL_MS = 2500;

/**
 * Holds the run being watched; while it is `running` it is polled, and `onFinished` fires once
 * when it reaches ok/partial/error.
 */
export function useRunWatcher(onFinished: (run: SearchRunOut) => void): {
  run: SearchRunOut | null;
  watch: (run: SearchRunOut | null) => void;
} {
  const [run, setRun] = useState<SearchRunOut | null>(null);
  const finishedRef = useRef(onFinished);
  finishedRef.current = onFinished;

  usePoll<SearchRunOut>(
    run && run.status === "running" ? () => api.getRun(run.id) : null,
    (next) => {
      setRun(next);
      if (next.status !== "running") {
        finishedRef.current(next);
        return false;
      }
      return true;
    },
    RUN_POLL_MS,
    () => true,
  );

  const watch = useCallback((next: SearchRunOut | null) => setRun(next), []);
  return { run, watch };
}
