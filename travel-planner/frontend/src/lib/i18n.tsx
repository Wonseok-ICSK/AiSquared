/**
 * UI language (English / Korean), chosen in the header and remembered in localStorage.
 *
 * Strings are written inline in both languages: `tr("Trips", "내 여행")`. `tr` reads the
 * current language at call time, so it must run during render (or inside a function called
 * from render) — never in a module-level constant. Fixed label maps use `labels()`, whose
 * properties are getters. Switching language remounts the whole tree (`LangRoot`), so plain
 * helpers such as the formatters in ./format pick the new language up without hooks.
 */

import { Fragment, useSyncExternalStore } from "react";
import type { ReactNode } from "react";

export type Lang = "en" | "ko";
export const LANGS: readonly Lang[] = ["ko", "en"];

const STORAGE_KEY = "ast-lang";
const DEFAULT_LANG: Lang = "ko";

function readStored(): Lang {
  try {
    const value = localStorage.getItem(STORAGE_KEY);
    return value === "en" || value === "ko" ? value : DEFAULT_LANG;
  } catch {
    return DEFAULT_LANG;
  }
}

let current: Lang = readStored();
const listeners = new Set<() => void>();

function applyDocumentLang(lang: Lang): void {
  if (typeof document !== "undefined") document.documentElement.lang = lang;
}
applyDocumentLang(current);

/** The current UI language. */
export function getLang(): Lang {
  return current;
}

/** Switch the UI language (stored locally; the app re-renders in the new language). */
export function setLang(lang: Lang): void {
  if (lang === current) return;
  current = lang;
  applyDocumentLang(lang);
  try {
    localStorage.setItem(STORAGE_KEY, lang);
  } catch {
    /* storage unavailable: the choice lasts for this session */
  }
  for (const listener of listeners) listener();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** `en` or `ko`, whichever is the current UI language. */
export function tr(en: string, ko: string): string {
  return current === "ko" ? ko : en;
}

/** Pick any value (not only strings) by the current language. */
export function pick<T>(en: T, ko: T): T {
  return current === "ko" ? ko : en;
}

/** Fixed label map whose values follow the current language: `labels({ a: ["A", "가"] })`. */
export function labels<K extends string>(pairs: Record<K, readonly [en: string, ko: string]>): Readonly<Record<K, string>> {
  const out = {} as Record<K, string>;
  for (const key of Object.keys(pairs) as K[]) {
    Object.defineProperty(out, key, { enumerable: true, get: () => tr(pairs[key][0], pairs[key][1]) });
  }
  return out;
}

/** Current language and a setter, re-rendering on change. */
export function useLang(): [Lang, (lang: Lang) => void] {
  const lang = useSyncExternalStore(subscribe, getLang, getLang);
  return [lang, setLang];
}

/** Re-mounts its children when the language changes so every string is re-evaluated. */
export function LangRoot({ children }: { children: ReactNode }) {
  const [lang] = useLang();
  return <Fragment key={lang}>{children}</Fragment>;
}
