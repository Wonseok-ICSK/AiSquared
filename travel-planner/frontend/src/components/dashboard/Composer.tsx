/** Home composer: a message-box-like card where the next trip is sketched (route, dates,
 * travellers) and sent on to the full trip form, plus suggestion chips that fill it in. */

import { useId, useState } from "react";
import type { FormEvent, MouseEvent, ReactElement } from "react";
import { useNavigate } from "react-router-dom";
import { ArrowLeftRight, ArrowUp, CalendarDays, TreePalm, Users, Utensils, Waves, X } from "lucide-react";
import { AirportInput } from "../AirportInput";
import { Segmented } from "../ui";
import { useApp } from "../../lib/appContext";
import { todaySeoul } from "../../lib/format";
import { tr } from "../../lib/i18n";
import {
  addDays,
  clampAdults,
  composerHint,
  composerQuery,
  nightsBetween,
  suggestions,
} from "./home";
import type { Suggestion, SuggestionIcon } from "./home";

const CHIP_ICON: Record<SuggestionIcon, ReactElement> = {
  weekend: <CalendarDays size={15} aria-hidden />,
  food: <Utensils size={15} aria-hidden />,
  sea: <Waves size={15} aria-hidden />,
  palm: <TreePalm size={15} aria-hidden />,
};

/** Open the native date picker on click where the browser supports it. */
function openPicker(e: MouseEvent<HTMLInputElement>) {
  try {
    e.currentTarget.showPicker?.();
  } catch {
    /* not allowed here (e.g. cross-origin frame): typing still works */
  }
}

/**
 * The composer. `defaultOrigin` fills "From" until the user picks something else (the most
 * common origin among the watched trips, else ICN).
 */
export function Composer({ defaultOrigin }: { defaultOrigin: string }) {
  const navigate = useNavigate();
  const { defaults } = useApp();
  const uid = useId();
  const ids = {
    prompt: `${uid}-prompt`,
    from: `${uid}-from`,
    to: `${uid}-to`,
    depart: `${uid}-depart`,
    ret: `${uid}-return`,
    hint: `${uid}-hint`,
  };
  /** null until the user touches "From": then `defaultOrigin` is shown. */
  const [origin, setOrigin] = useState<string | null>(null);
  const [destination, setDestination] = useState("");
  const [depart, setDepart] = useState("");
  const [returnDate, setReturnDate] = useState("");
  const [adults, setAdults] = useState<number | null>(null);

  const today = todaySeoul();
  const shownOrigin = origin ?? defaultOrigin;
  const shownAdults = adults ?? clampAdults(defaults.adults);
  const chips = suggestions(today);

  const changeDepart = (next: string) => {
    // Keep the trip length when the new departure would land after the return date.
    if (next && returnDate && returnDate < next) {
      const nights = depart ? Math.max(0, nightsBetween(depart, returnDate)) : 0;
      setReturnDate(addDays(next, nights));
    }
    setDepart(next);
  };

  const swap = () => {
    setOrigin(destination);
    setDestination(shownOrigin);
  };

  const apply = (s: Suggestion) => {
    setOrigin(s.origin);
    setDestination(s.destination);
    setDepart(s.depart);
    setReturnDate(s.returnDate);
  };

  const isApplied = (s: Suggestion) =>
    shownOrigin === s.origin && destination === s.destination && depart === s.depart && returnDate === s.returnDate;

  const submit = (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    const query = composerQuery({ origin: shownOrigin, destination, depart, returnDate, adults: shownAdults });
    navigate(query ? `/trips/new?${query}` : "/trips/new");
  };

  const focusDestination = () => document.getElementById(ids.to)?.focus();

  return (
    <div className="home-compose">
      <form className="composer" onSubmit={submit} aria-labelledby={ids.prompt} noValidate>
        {/* Reads like the message box's placeholder; a click goes to the destination. */}
        <p className="composer__prompt" id={ids.prompt} onClick={focusDestination}>
          {tr("Where to next?", "어디로 떠나볼까요?")}
        </p>

        <div className="composer__route">
          <div className="composer-field">
            <label className="composer-field__label" htmlFor={ids.from}>
              {tr("From", "출발")}
            </label>
            <AirportInput
              id={ids.from}
              value={shownOrigin}
              placeholder={tr("City or airport", "도시 또는 공항")}
              onChange={(code) => setOrigin(code)}
            />
          </div>
          <button
            type="button"
            className="composer__swap"
            onClick={swap}
            aria-label={tr("Swap origin and destination", "출발지와 도착지 바꾸기")}
            title={tr("Swap", "바꾸기")}
          >
            <ArrowLeftRight size={15} aria-hidden />
          </button>
          <div className="composer-field">
            <label className="composer-field__label" htmlFor={ids.to}>
              {tr("To", "도착")}
            </label>
            <AirportInput
              id={ids.to}
              value={destination}
              placeholder={tr("City or airport", "도시 또는 공항")}
              onChange={(code) => setDestination(code)}
            />
          </div>
        </div>

        <div className="composer__dates">
          <div className="composer-field">
            <label className="composer-field__label" htmlFor={ids.depart}>
              {tr("Depart", "가는 날")}
            </label>
            <input
              id={ids.depart}
              type="date"
              className={`input composer-field__date${depart ? "" : " is-empty"}`}
              min={today}
              value={depart}
              onClick={openPicker}
              onChange={(e) => changeDepart(e.target.value)}
            />
          </div>
          <div className="composer-field composer-field--return">
            <label className="composer-field__label" htmlFor={ids.ret}>
              {tr("Return", "오는 날")}
              <span className="composer-field__opt">{tr("optional", "선택")}</span>
            </label>
            <input
              id={ids.ret}
              type="date"
              className={`input composer-field__date${returnDate ? "" : " is-empty"}`}
              min={depart || today}
              value={returnDate}
              aria-describedby={ids.hint}
              onClick={openPicker}
              onChange={(e) => setReturnDate(e.target.value)}
            />
            {returnDate && (
              <button
                type="button"
                className="composer-field__clear"
                onClick={() => setReturnDate("")}
                aria-label={tr("Clear the return date (one way)", "오는 날 지우기 (편도)")}
                title={tr("Clear (one way)", "지우기 (편도)")}
              >
                <X size={14} aria-hidden />
              </button>
            )}
          </div>
        </div>

        <div className="composer__bar">
          <span className="composer__people">
            <Users size={16} aria-hidden />
            <Segmented
              label={tr("Travellers", "인원")}
              value={shownAdults}
              onChange={(n) => setAdults(n)}
              options={[
                { value: 1, label: tr("1 adult", "1명") },
                { value: 2, label: tr("2 adults", "2명") },
              ]}
            />
          </span>
          <span className="composer__hint" id={ids.hint} aria-live="polite">
            {composerHint({ depart, returnDate })}
          </span>
          <button
            type="submit"
            className="composer__send"
            aria-label={tr("Continue with these details", "이 조건으로 이어서 설정하기")}
            title={tr("Continue", "이어서 설정하기")}
          >
            <ArrowUp size={18} strokeWidth={2.4} aria-hidden />
          </button>
        </div>
      </form>

      <div className="home-chips" role="group" aria-label={tr("Suggestions", "추천 여행")}>
        {chips.map((s) => (
          <button
            key={s.key}
            type="button"
            className={`home-chip${isApplied(s) ? " is-on" : ""}`}
            onClick={() => apply(s)}
          >
            {CHIP_ICON[s.icon]}
            {s.label}
          </button>
        ))}
      </div>
    </div>
  );
}
