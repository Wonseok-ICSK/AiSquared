/** Accessible tab list (roving focus with arrow keys); selection is controlled by the parent. */

import { useEffect, useRef } from "react";
import type { KeyboardEvent, ReactNode } from "react";

export interface TabDef<K extends string> {
  key: K;
  label: string;
  icon?: ReactNode;
  badge?: ReactNode;
}

/** Tab buttons; panels are rendered by the parent with id `${idPrefix}-panel-${key}`. */
export function Tabs<K extends string>({
  tabs,
  value,
  onChange,
  idPrefix,
  label,
}: {
  tabs: readonly TabDef<K>[];
  value: K;
  onChange: (key: K) => void;
  idPrefix: string;
  label: string;
}) {
  const refs = useRef<(HTMLButtonElement | null)[]>([]);
  const index = tabs.findIndex((t) => t.key === value);

  // Keep the selected tab visible when the tab strip scrolls horizontally (phones).
  useEffect(() => {
    const el = refs.current[index];
    const strip = el?.parentElement;
    if (!el || !strip || strip.scrollWidth <= strip.clientWidth) return;
    strip.scrollTo({ left: el.offsetLeft - strip.clientWidth / 2 + el.clientWidth / 2, behavior: "smooth" });
  }, [index]);

  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    const index = tabs.findIndex((t) => t.key === value);
    let next = index;
    if (e.key === "ArrowRight") next = (index + 1) % tabs.length;
    else if (e.key === "ArrowLeft") next = (index - 1 + tabs.length) % tabs.length;
    else if (e.key === "Home") next = 0;
    else if (e.key === "End") next = tabs.length - 1;
    else return;
    e.preventDefault();
    onChange(tabs[next].key);
    refs.current[next]?.focus();
  };

  return (
    <div className="tabs" role="tablist" aria-label={label} onKeyDown={onKeyDown}>
      {tabs.map((tab, i) => (
        <button
          key={tab.key}
          ref={(el) => {
            refs.current[i] = el;
          }}
          type="button"
          role="tab"
          className="tab"
          id={`${idPrefix}-tab-${tab.key}`}
          aria-selected={tab.key === value}
          aria-controls={`${idPrefix}-panel-${tab.key}`}
          tabIndex={tab.key === value ? 0 : -1}
          onClick={() => onChange(tab.key)}
        >
          {tab.icon}
          {tab.label}
          {tab.badge}
        </button>
      ))}
    </div>
  );
}
