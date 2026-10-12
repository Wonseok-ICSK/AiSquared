/** Airport / city autocomplete (ARIA combobox) backed by GET /api/airports?q=. */

import { useEffect, useId, useRef, useState } from "react";
import type { KeyboardEvent } from "react";
import { Building2, Plane } from "lucide-react";
import { api } from "../api/endpoints";
import { isAbort } from "../api/client";
import type { AirportOut } from "../api/types";
import { airportForText, looksLikeCode } from "../lib/airportMatch";
import { cityName, rememberAirports, useAirport } from "../lib/airports";
import { tr } from "../lib/i18n";
import { useDebounced } from "../lib/hooks";

interface AirportInputProps {
  id?: string;
  value: string;
  onChange: (code: string, airport: AirportOut | null) => void;
  placeholder?: string;
  describedBy?: string;
  invalid?: boolean;
  /** Clear the box after a pick (multi-value usage, e.g. alternative destinations). */
  clearOnSelect?: boolean;
  ariaLabel?: string;
}

/** Describe an airport/metro for the line under the input. */
export function describeAirport(a: AirportOut): string {
  if (a.is_metro) return `${cityName(a)} · ${tr("all airports", "모든 공항")} · ${a.country}`;
  const shown = cityName(a);
  const city = shown && !a.name.includes(shown) ? `${shown}, ` : "";
  return `${a.name} · ${city}${a.country}`;
}

/**
 * Text box with a listbox of matching airports and metro codes.
 *
 * The stored value only changes through a pick: clicking/Enter on an option, or leaving the box
 * (Tab/click away) with text that resolves to one airport (see `airportForText`; Tab takes the
 * highlighted option). Text that resolves to nothing is reverted to the stored code with a
 * note, so the box never shows something other than what will be saved.
 */
export function AirportInput({
  id,
  value,
  onChange,
  placeholder = tr("City or airport (e.g. Tokyo, ICN)", "도시 또는 공항 (예: 도쿄, ICN)"),
  describedBy,
  invalid,
  clearOnSelect = false,
  ariaLabel,
}: AirportInputProps) {
  const autoId = useId();
  const inputId = id ?? autoId;
  const listId = `${inputId}-list`;
  const [text, setText] = useState(value);
  const [open, setOpen] = useState(false);
  const [options, setOptions] = useState<AirportOut[]>([]);
  const [active, setActive] = useState(-1);
  const [loading, setLoading] = useState(false);
  /** The query `options` were loaded for. */
  const [optionsQuery, setOptionsQuery] = useState("");
  const [note, setNote] = useState<string | null>(null);
  const focused = useRef(false);
  const commitSeq = useRef(0);
  const query = useDebounced(text.trim(), 180);
  const selected = useAirport(clearOnSelect ? "" : value);

  useEffect(() => {
    if (!focused.current && !clearOnSelect) setText(value);
  }, [value, clearOnSelect]);

  useEffect(() => {
    if (!open || query.length < 1 || (!clearOnSelect && query.toUpperCase() === value && options.length > 0)) return;
    const ctrl = new AbortController();
    setLoading(true);
    api
      .airports(query, ctrl.signal)
      .then((list) => {
        rememberAirports(list);
        setOptions(list);
        setOptionsQuery(query);
        setActive(list.length > 0 ? 0 : -1);
      })
      .catch((err) => {
        if (!isAbort(err)) setOptions([]);
      })
      .finally(() => setLoading(false));
    return () => ctrl.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [query, open]);

  const pick = (airport: AirportOut) => {
    commitSeq.current++;
    setNote(null);
    onChange(airport.code, airport);
    setText(clearOnSelect ? "" : airport.code);
    setOpen(false);
    setOptions([]);
    setOptionsQuery("");
  };

  /** Put the stored code back in the box and say why the typed text was not used. */
  const revert = (typed: string) => {
    setText(value);
    setNote(
      tr(
        `No airport matches “${typed}”${value ? `, kept ${value}` : ""}. Pick one from the list.`,
        `“${typed}”와(과) 일치하는 공항이 없어요${value ? ` (${value} 유지)` : ""}. 목록에서 선택하세요.`,
      ),
    );
  };

  /** Resolve free text the user is leaving (or pressed Enter on) to an airport, or revert. */
  const commitTyped = () => {
    if (clearOnSelect) return;
    const typed = text.trim();
    const seq = ++commitSeq.current;
    setNote(null);
    if (typed === "") {
      if (value !== "") onChange("", null);
      setText("");
      return;
    }
    if (typed.toUpperCase() === value) {
      setText(value);
      return;
    }
    const fresh = optionsQuery.toLowerCase() === typed.toLowerCase();
    const match = airportForText(typed, options, open ? active : -1, fresh);
    if (match) {
      pick(match);
      return;
    }
    if (looksLikeCode(typed)) {
      const code = typed.toUpperCase();
      onChange(code, null);
      setText(code);
      return;
    }
    // Text typed faster than the suggestions loaded: look it up once more.
    setNote(tr(`Looking up “${typed}”…`, `“${typed}” 찾는 중…`));
    api
      .airports(typed)
      .then((list) => {
        if (seq !== commitSeq.current || focused.current) return;
        rememberAirports(list);
        const found = airportForText(typed, list, 0, true);
        if (found) pick(found);
        else revert(typed);
      })
      .catch(() => {
        if (seq === commitSeq.current && !focused.current) revert(typed);
      });
  };

  const onKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setOpen(true);
      setActive((i) => Math.min(options.length - 1, i + 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActive((i) => Math.max(0, i - 1));
    } else if (e.key === "Enter") {
      if (open && active >= 0 && options[active]) {
        e.preventDefault();
        pick(options[active]);
      } else if (clearOnSelect && looksLikeCode(text)) {
        e.preventDefault();
        onChange(text.trim().toUpperCase(), null);
        setText("");
      } else if (!clearOnSelect && text.trim().toUpperCase() !== value) {
        // never submit the form with text that differs from the stored code
        e.preventDefault();
        setOpen(false);
        commitTyped();
      }
    } else if (e.key === "Escape") {
      setOpen(false);
    }
  };

  const showList = open && query.length >= 1;
  return (
    <div className="combo">
      <input
        id={inputId}
        className="input"
        role="combobox"
        aria-autocomplete="list"
        aria-expanded={showList}
        aria-controls={listId}
        aria-activedescendant={showList && active >= 0 ? `${listId}-${active}` : undefined}
        aria-describedby={describedBy}
        aria-invalid={invalid || undefined}
        aria-label={ariaLabel}
        autoComplete="off"
        spellCheck={false}
        placeholder={placeholder}
        value={text}
        onChange={(e) => {
          setText(e.target.value);
          setNote(null);
          setOpen(true);
        }}
        onFocus={() => {
          focused.current = true;
          setNote(null);
        }}
        onBlur={() => {
          focused.current = false;
          setOpen(false);
          commitTyped();
        }}
        onKeyDown={onKeyDown}
      />
      {showList && (
        <ul className="combo__list" id={listId} role="listbox">
          {options.length === 0 && (
            <li className="combo__empty" role="presentation">
              {loading ? tr("Searching…", "검색 중…") : tr("No matching airports", "일치하는 공항이 없어요")}
            </li>
          )}
          {options.map((a, i) => (
            <li
              key={a.code}
              id={`${listId}-${i}`}
              role="option"
              aria-selected={i === active}
              className="combo__opt"
              onMouseDown={(e) => e.preventDefault()}
              onMouseEnter={() => setActive(i)}
              onClick={() => pick(a)}
            >
              <span className="combo__code">{a.code}</span>
              <span className="combo__name">
                {a.is_metro ? (
                  <Building2 size={13} aria-hidden style={{ verticalAlign: -2, marginRight: 4 }} />
                ) : (
                  <Plane size={13} aria-hidden style={{ verticalAlign: -2, marginRight: 4 }} />
                )}
                {a.is_metro ? `${cityName(a)} (${tr("all airports", "모든 공항")})` : a.name}
              </span>
              <span className="combo__meta">
                {a.is_metro ? a.country : `${cityName(a) ? `${cityName(a)}, ` : ""}${a.country}`}
              </span>
            </li>
          ))}
        </ul>
      )}
      {!clearOnSelect && (
        <span className={`combo__selected${note ? " combo__selected--warn" : ""}`} aria-live="polite">
          {note ?? (selected ? describeAirport(selected) : value && selected === null ? tr("Unknown code", "알 수 없는 코드") : "")}
        </span>
      )}
    </div>
  );
}
