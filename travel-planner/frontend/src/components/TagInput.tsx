/** Free-text list input rendered as removable chips (Enter or comma adds an item). */

import { useState } from "react";
import type { KeyboardEvent } from "react";
import { X } from "lucide-react";
import { tr } from "../lib/i18n";

interface TagInputProps {
  id?: string;
  value: string[];
  onChange: (value: string[]) => void;
  placeholder?: string;
  /** Normalize/validate a new entry; return null to reject it. */
  transform?: (raw: string) => string | null;
  maxItems?: number;
  describedBy?: string;
  invalid?: boolean;
  ariaLabel?: string;
}

/** Chips + text box; Backspace on an empty box removes the last chip. */
export function TagInput({
  id,
  value,
  onChange,
  placeholder,
  transform = (raw) => raw.trim() || null,
  maxItems = 50,
  describedBy,
  invalid,
  ariaLabel,
}: TagInputProps) {
  const [draft, setDraft] = useState("");
  const [rejected, setRejected] = useState(false);

  const commit = (raw: string): boolean => {
    const parts = raw.split(",").map((p) => p.trim()).filter(Boolean);
    if (parts.length === 0) return true;
    const next = [...value];
    let ok = true;
    for (const part of parts) {
      const item = transform(part);
      if (item === null) {
        ok = false;
        continue;
      }
      if (!next.includes(item) && next.length < maxItems) next.push(item);
    }
    if (next.length !== value.length) onChange(next);
    return ok;
  };

  const onKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Enter" || e.key === ",") {
      e.preventDefault();
      const ok = commit(draft);
      setRejected(!ok);
      if (ok) setDraft("");
    } else if (e.key === "Backspace" && draft === "" && value.length > 0) {
      onChange(value.slice(0, -1));
    }
  };

  const onBlur = () => {
    if (!draft.trim()) return;
    const ok = commit(draft);
    setRejected(!ok);
    if (ok) setDraft("");
  };

  return (
    <div className="chip-input" aria-invalid={invalid || rejected || undefined}>
      {value.map((item) => (
        <span className="chip" key={item}>
          <span className="chip__text">{item}</span>
          <button
            type="button"
            aria-label={tr(`Remove ${item}`, `${item} 삭제`)}
            onClick={() => onChange(value.filter((v) => v !== item))}
          >
            <X size={12} aria-hidden />
          </button>
        </span>
      ))}
      <input
        id={id}
        value={draft}
        placeholder={value.length >= maxItems ? "" : placeholder}
        aria-label={ariaLabel}
        aria-describedby={describedBy}
        disabled={value.length >= maxItems}
        onChange={(e) => {
          setDraft(e.target.value);
          setRejected(false);
        }}
        onKeyDown={onKeyDown}
        onBlur={onBlur}
      />
    </div>
  );
}
