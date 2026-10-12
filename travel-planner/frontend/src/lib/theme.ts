/** Light/dark theme: follows prefers-color-scheme unless the user picks one (stored locally). */

import { useCallback, useEffect, useState } from "react";

export type ThemeChoice = "system" | "light" | "dark";
const STORAGE_KEY = "ast-theme";

function readChoice(): ThemeChoice {
  try {
    const value = localStorage.getItem(STORAGE_KEY);
    return value === "light" || value === "dark" ? value : "system";
  } catch {
    return "system";
  }
}

function apply(choice: ThemeChoice): void {
  const root = document.documentElement;
  if (choice === "system") delete root.dataset.theme;
  else root.dataset.theme = choice;
}

/** Current theme choice and a function cycling system -> light -> dark. */
export function useTheme(): { choice: ThemeChoice; cycle: () => void } {
  const [choice, setChoice] = useState<ThemeChoice>(readChoice);

  useEffect(() => {
    apply(choice);
    try {
      if (choice === "system") localStorage.removeItem(STORAGE_KEY);
      else localStorage.setItem(STORAGE_KEY, choice);
    } catch {
      /* storage unavailable: theme still applies for this session */
    }
  }, [choice]);

  const cycle = useCallback(() => {
    setChoice((c) => (c === "system" ? "light" : c === "light" ? "dark" : "system"));
  }, []);
  return { choice, cycle };
}
