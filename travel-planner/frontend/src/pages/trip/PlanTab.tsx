/** Trip plan tab: travel style weights, companions, food, places and budget (PreferencesIn). */

import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import { Compass, RotateCcw, Save, Scale, UtensilsCrossed, Wallet } from "lucide-react";
import { api } from "../../api/endpoints";
import { ApiError } from "../../api/client";
import type {
  Companion,
  OptionalBudgetField,
  Pace,
  PreferencesIn,
  PreferencesOut,
  PriceBasis,
  StyleKey,
  StyleMode,
  TripOut,
} from "../../api/types";
import { COMPANIONS, OPTIONAL_BUDGET_FIELDS, PACES, STYLE_KEYS, STYLE_PERCENT_MAX, STYLE_SCORE_MAX } from "../../api/types";
import { TagInput } from "../../components/TagInput";
import { Banner, Button, ErrorBanner, Field, Segmented, SkeletonBlock } from "../../components/ui";
import { useApp } from "../../lib/appContext";
import { COMPANION_LABEL, PACE_LABEL, STYLE_LABEL, formatMoney, formatUtc } from "../../lib/format";
import { labels, tr } from "../../lib/i18n";
import type { Errors } from "../../lib/validation";
import { checkBudget, checkLength, checkMoney, collect, isCurrencyCode, numberText, parseNumber } from "../../lib/validation";

type Weights = Record<StyleKey, number>;

interface PrefsDraft {
  style_mode: StyleMode;
  percent: Weights;
  score: Weights;
  companion: Companion;
  pace: Pace;
  food_wishlist: string[];
  preferred_cuisines: string[];
  avoid_foods: string[];
  restaurant_budget_min: string;
  restaurant_budget_max: string;
  restaurant_budget_currency: string;
  hotel_area: string;
  must_visit: string[];
  avoid: string[];
  notes: string;
  budget_basis: PriceBasis;
  budget_currency: string;
  budget_total: string;
  budget_flight: string;
  optional: Record<OptionalBudgetField, string>;
}

const BUDGET_LABEL = labels<OptionalBudgetField>({
  budget_hotel: ["Hotel", "숙소"],
  budget_food: ["Food", "식비"],
  budget_transport: ["Transportation", "교통"],
  budget_activities: ["Activities", "액티비티"],
  budget_shopping: ["Shopping", "쇼핑"],
  budget_other: ["Other", "기타"],
});

function weightsFrom(source: Record<string, number> | undefined, fallback: number): Weights {
  return Object.fromEntries(STYLE_KEYS.map((k) => [k, source?.[k] ?? fallback])) as Weights;
}

function emptyOptional(): Record<OptionalBudgetField, string> {
  return Object.fromEntries(OPTIONAL_BUDGET_FIELDS.map((f) => [f, ""])) as Record<OptionalBudgetField, string>;
}

function draftFrom(p: PreferencesOut | null, defaults: ReturnType<typeof useApp>["defaults"]): PrefsDraft {
  const percentDefault = weightsFrom(defaults.style_weights, 10);
  const scoreDefault = weightsFrom(defaults.score_style_weights, 3);
  const optional = emptyOptional();
  if (p) for (const f of OPTIONAL_BUDGET_FIELDS) optional[f] = numberText(p[f]);
  return {
    style_mode: p?.style_mode ?? defaults.style_mode,
    percent: p?.style_mode === "percent" ? weightsFrom(p.style_weights, 0) : percentDefault,
    score: p?.style_mode === "score" ? weightsFrom(p.style_weights, 0) : scoreDefault,
    companion: p?.companion ?? "solo",
    pace: p?.pace ?? "moderate",
    food_wishlist: p?.food_wishlist ?? [],
    preferred_cuisines: p?.preferred_cuisines ?? [],
    avoid_foods: p?.avoid_foods ?? [],
    restaurant_budget_min: numberText(p?.restaurant_budget_min),
    restaurant_budget_max: numberText(p?.restaurant_budget_max),
    restaurant_budget_currency: p?.restaurant_budget_currency ?? "",
    hotel_area: p?.hotel_area ?? "",
    must_visit: p?.must_visit ?? [],
    avoid: p?.avoid ?? [],
    notes: p?.notes ?? "",
    budget_basis: p?.budget_basis ?? defaults.budget_basis,
    budget_currency: p?.budget_currency ?? defaults.currency,
    budget_total: numberText(p?.budget_total),
    budget_flight: numberText(p?.budget_flight),
    optional,
  };
}

function sum(w: Weights): number {
  return STYLE_KEYS.reduce((s, k) => s + (w[k] || 0), 0);
}

function validate(d: PrefsDraft): Errors {
  const rmin = parseNumber(d.restaurant_budget_min);
  const rmax = parseNumber(d.restaurant_budget_max);
  const total = sum(d.percent);
  return collect([
    ["style_weights", d.style_mode === "percent" && total > STYLE_PERCENT_MAX + 1e-6 ? tr(`Weights add up to ${total}% — keep the total at 100% or less`, `가중치 합계가 ${total}%예요. 합계를 100% 이하로 맞춰 주세요`) : null],
    ["restaurant_budget_min", checkMoney(d.restaurant_budget_min)],
    [
      "restaurant_budget_max",
      checkMoney(d.restaurant_budget_max) ??
        (rmin !== null && rmax !== null && !Number.isNaN(rmin) && !Number.isNaN(rmax) && rmin > rmax ? tr("Must be ≥ the minimum", "최소 금액 이상이어야 해요") : null),
    ],
    [
      "restaurant_budget_currency",
      d.restaurant_budget_currency && !isCurrencyCode(d.restaurant_budget_currency) ? tr("3-letter code, e.g. JPY", "3자리 통화 코드를 입력하세요 (예: JPY)") : null,
    ],
    ["hotel_area", checkLength(d.hotel_area, 200)],
    ["notes", checkLength(d.notes, 4000)],
    ["budget_currency", isCurrencyCode(d.budget_currency) ? null : tr("3-letter code, e.g. KRW", "3자리 통화 코드를 입력하세요 (예: KRW)")],
    ["budget_total", checkBudget(d.budget_total, true)],
    ["budget_flight", checkBudget(d.budget_flight, true)],
    ...OPTIONAL_BUDGET_FIELDS.map((f): [string, string | null] => [f, checkBudget(d.optional[f])]),
  ]);
}

function toPayload(d: PrefsDraft): PreferencesIn {
  const payload: PreferencesIn = {
    style_mode: d.style_mode,
    style_weights: d.style_mode === "percent" ? d.percent : d.score,
    companion: d.companion,
    pace: d.pace,
    food_wishlist: d.food_wishlist,
    preferred_cuisines: d.preferred_cuisines,
    avoid_foods: d.avoid_foods,
    restaurant_budget_min: parseNumber(d.restaurant_budget_min),
    restaurant_budget_max: parseNumber(d.restaurant_budget_max),
    restaurant_budget_currency: d.restaurant_budget_currency.trim() ? d.restaurant_budget_currency.trim().toUpperCase() : null,
    hotel_area: d.hotel_area.trim() || null,
    must_visit: d.must_visit,
    avoid: d.avoid,
    notes: d.notes.trim() ? d.notes : null,
    budget_basis: d.budget_basis,
    budget_currency: d.budget_currency.trim().toUpperCase(),
    budget_total: parseNumber(d.budget_total) ?? 0,
    budget_flight: parseNumber(d.budget_flight) ?? 0,
  };
  for (const f of OPTIONAL_BUDGET_FIELDS) payload[f] = parseNumber(d.optional[f]);
  return payload;
}

/** Hint under an optional budget input: what the typed value means. */
function budgetHint(text: string, currency: string): string {
  const v = parseNumber(text);
  if (v === null) return tr("Optional · 0 or -1 = unlimited", "선택 사항 · 0 또는 -1 = 제한 없음");
  if (v === 0 || v === -1) return tr("Unlimited (reasonable range)", "제한 없음 (적당한 범위로 계획)");
  return Number.isNaN(v) || v < 0 ? tr("0 or -1 = unlimited", "0 또는 -1 = 제한 없음") : formatMoney(v, currency);
}

const shortText = (raw: string) => {
  const v = raw.trim();
  return v && v.length <= 200 ? v : null;
};

function StyleWeights({ draft, setDraft, error }: { draft: PrefsDraft; setDraft: (fn: (d: PrefsDraft) => PrefsDraft) => void; error?: string }) {
  const total = sum(draft.percent);
  const setWeight = (key: StyleKey, value: number) =>
    setDraft((d) =>
      d.style_mode === "percent"
        ? { ...d, percent: { ...d.percent, [key]: value } }
        : { ...d, score: { ...d.score, [key]: value } },
    );
  const normalize = () =>
    setDraft((d) => {
      const s = sum(d.percent);
      if (s === 0) return d;
      const scaled = Object.fromEntries(STYLE_KEYS.map((k) => [k, Math.floor((d.percent[k] / s) * 100)])) as Weights;
      return { ...d, percent: scaled };
    });

  return (
    <div className="stack-sm">
      <div className="row-between">
        <Segmented
          label={tr("Weighting mode", "가중치 방식")}
          value={draft.style_mode}
          onChange={(v) => setDraft((d) => ({ ...d, style_mode: v }))}
          options={[
            { value: "percent", label: tr("Percent split", "비율(%) 배분") },
            { value: "score", label: tr("1–5 scores", "1–5점 점수") },
          ]}
        />
        {draft.style_mode === "percent" && (
          <div className="row" style={{ gap: 8 }}>
            <span className={`weight-total${total > STYLE_PERCENT_MAX ? " is-over" : ""}`} aria-live="polite">
              {tr(`Total ${total}% / 100%`, `합계 ${total}% / 100%`)}
            </span>
            <Button size="sm" variant="ghost" icon={<Scale size={14} aria-hidden />} onClick={normalize} disabled={total === 0}>
              {tr("Fit to 100%", "100%에 맞추기")}
            </Button>
          </div>
        )}
      </div>
      {draft.style_mode === "percent" && (
        <div className="weight-bar" aria-hidden>
          <span style={{ width: `${Math.min(100, total)}%` }} className={total > 100 ? "is-over" : undefined} />
        </div>
      )}
      <div className="weights">
        {STYLE_KEYS.map((key) =>
          draft.style_mode === "percent" ? (
            <label className="weight" key={key}>
              <span className="weight__label">{STYLE_LABEL[key]}</span>
              <input
                type="range"
                min={0}
                max={100}
                step={5}
                value={draft.percent[key]}
                aria-valuetext={`${draft.percent[key]}%`}
                onChange={(e) => setWeight(key, Number(e.target.value))}
              />
              <span className="weight__value num">{draft.percent[key]}%</span>
            </label>
          ) : (
            <div className="weight" key={key}>
              <span className="weight__label" id={`score-${key}`}>
                {STYLE_LABEL[key]}
              </span>
              <Segmented
                label={tr(`${STYLE_LABEL[key]} score`, `${STYLE_LABEL[key]} 점수`)}
                value={draft.score[key]}
                onChange={(v) => setWeight(key, v)}
                options={Array.from({ length: STYLE_SCORE_MAX + 1 }, (_, i) => ({ value: i, label: i === 0 ? tr("Off", "끔") : String(i) }))}
              />
            </div>
          ),
        )}
      </div>
      {error && <span className="field__error">{error}</span>}
    </div>
  );
}

/** Preferences + budget form. */
export function PlanTab({ trip }: { trip: TripOut }) {
  const { defaults } = useApp();
  const [stored, setStored] = useState<PreferencesOut | null>(null);
  const [draft, setDraftState] = useState<PrefsDraft>(() => draftFrom(null, defaults));
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<unknown>(null);
  const [errors, setErrors] = useState<Errors>({});
  const [submitted, setSubmitted] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<unknown>(null);
  const [savedAt, setSavedAt] = useState<string | null>(null);
  const setDraft = (fn: (d: PrefsDraft) => PrefsDraft) => setDraftState(fn);

  useEffect(() => {
    setLoading(true);
    api
      .getPreferences(trip.id)
      .then((p) => {
        setStored(p);
        setDraftState(draftFrom(p, defaults));
      })
      .catch(setLoadError)
      .finally(() => setLoading(false));
    // defaults only matter for unsaved preferences; avoid refetching when meta arrives
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [trip.id]);

  const set = <K extends keyof PrefsDraft>(key: K, value: PrefsDraft[K]) => setDraftState((d) => ({ ...d, [key]: value }));
  const setOptional = (f: OptionalBudgetField, v: string) =>
    setDraftState((d) => ({ ...d, optional: { ...d.optional, [f]: v } }));

  const live = validate(draft);
  const shown = submitted ? { ...errors, ...live } : errors;

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setSubmitted(true);
    setErrors({});
    setSaveError(null);
    if (Object.keys(live).length > 0) {
      requestAnimationFrame(() => document.querySelector<HTMLElement>('.plan-form [aria-invalid="true"]')?.focus());
      return;
    }
    setSaving(true);
    try {
      const saved = await api.putPreferences(trip.id, toPayload(draft));
      setStored(saved);
      setDraftState(draftFrom(saved, defaults));
      setSavedAt(saved.updated_at);
      setSubmitted(false);
    } catch (err) {
      setSaveError(err);
      if (err instanceof ApiError) setErrors(err.fieldErrors);
    } finally {
      setSaving(false);
    }
  };

  if (loading) return <SkeletonBlock lines={6} label={tr("Loading preferences", "일정 계획 불러오는 중")} />;
  if (loadError) return <ErrorBanner error={loadError} title={tr("Could not load preferences", "일정 계획을 불러오지 못했어요")} />;

  const cur = draft.budget_currency || trip.currency;
  const categorySum = OPTIONAL_BUDGET_FIELDS.reduce((s, f) => {
    const v = parseNumber(draft.optional[f]);
    return v !== null && !Number.isNaN(v) && v > 0 ? s + v : s;
  }, 0);
  const total = parseNumber(draft.budget_total);
  const flight = parseNumber(draft.budget_flight);

  return (
    <form className="plan-form stack" onSubmit={submit} noValidate>
      {!stored && (
        <Banner tone="info" title={tr("Not saved yet", "아직 저장하지 않았어요")}>
          {tr(
            "These preferences shape generated itineraries. Total and flight budgets are required.",
            "여기서 정한 취향과 예산으로 여행 일정을 만들어요. 총예산과 항공권 예산은 필수예요.",
          )}
        </Banner>
      )}

      <section className="card plan-card">
        <div className="card__body section">
          <h2 className="section__title">
            <span className="plan-card__icon" aria-hidden>
              <Compass size={17} />
            </span>
            {tr("Travel style", "여행 스타일")}
          </h2>
          <p className="section__desc">
            {tr("How much each kind of activity matters to you on this trip.", "이번 여행에서 각 활동이 얼마나 중요한지 정해 주세요.")}
          </p>
          <StyleWeights draft={draft} setDraft={setDraft} error={shown.style_weights} />
          <div className="form-grid" style={{ marginTop: 8 }}>
            <Field label={tr("Travelling as", "동행")} className="span-6">
              {() => (
                <Segmented
                  label={tr("Companion", "동행 유형")}
                  value={draft.companion}
                  onChange={(v) => set("companion", v)}
                  options={COMPANIONS.map((c) => ({ value: c, label: COMPANION_LABEL[c] }))}
                />
              )}
            </Field>
            <Field label={tr("Pace", "일정 강도")} className="span-6">
              {() => (
                <Segmented
                  label={tr("Pace", "일정 강도")}
                  value={draft.pace}
                  onChange={(v) => set("pace", v)}
                  options={PACES.map((p) => ({ value: p, label: PACE_LABEL[p] }))}
                />
              )}
            </Field>
          </div>
        </div>
      </section>

      <section className="card plan-card">
        <div className="card__body section">
          <h2 className="section__title">
            <span className="plan-card__icon" aria-hidden>
              <UtensilsCrossed size={17} />
            </span>
            {tr("Food & places", "음식 · 장소")}
          </h2>
          <div className="form-grid">
            <Field label={tr("Foods to try", "먹고 싶은 음식")} className="span-4" hint={tr("Enter or comma to add (e.g. sushi, ramen)", "Enter 또는 쉼표로 추가 (예: 스시, 라멘)")}>
              {({ id, describedBy }) => (
                <TagInput id={id} describedBy={describedBy} value={draft.food_wishlist} onChange={(v) => set("food_wishlist", v)} transform={shortText} placeholder={tr("sushi, izakaya…", "스시, 이자카야…")} />
              )}
            </Field>
            <Field label={tr("Preferred cuisines", "선호하는 음식 종류")} className="span-4">
              {({ id, describedBy }) => (
                <TagInput id={id} describedBy={describedBy} value={draft.preferred_cuisines} onChange={(v) => set("preferred_cuisines", v)} transform={shortText} placeholder={tr("Japanese, local…", "일식, 현지 음식…")} />
              )}
            </Field>
            <Field label={tr("Avoid foods", "피하고 싶은 음식")} className="span-4">
              {({ id, describedBy }) => (
                <TagInput id={id} describedBy={describedBy} value={draft.avoid_foods} onChange={(v) => set("avoid_foods", v)} transform={shortText} placeholder={tr("raw fish, spicy…", "날생선, 매운 음식…")} />
              )}
            </Field>
            <Field label={tr("Restaurant budget per meal — min", "한 끼 식사 예산 — 최소")} className="span-4" error={shown.restaurant_budget_min}>
              {({ id, describedBy, invalid }) => (
                <input id={id} className="input input--num" inputMode="decimal" placeholder="1000" value={draft.restaurant_budget_min} aria-describedby={describedBy} aria-invalid={invalid || undefined} onChange={(e) => set("restaurant_budget_min", e.target.value)} />
              )}
            </Field>
            <Field label={tr("max", "최대")} className="span-4" error={shown.restaurant_budget_max}>
              {({ id, describedBy, invalid }) => (
                <input id={id} className="input input--num" inputMode="decimal" placeholder="5000" value={draft.restaurant_budget_max} aria-describedby={describedBy} aria-invalid={invalid || undefined} onChange={(e) => set("restaurant_budget_max", e.target.value)} />
              )}
            </Field>
            <Field label={tr("Restaurant currency", "식사 예산 통화")} className="span-4" error={shown.restaurant_budget_currency} hint={tr("Usually the local currency, e.g. JPY", "보통 현지 통화예요 (예: JPY)")}>
              {({ id, describedBy, invalid }) => (
                <input id={id} className="input mono" maxLength={3} placeholder="JPY" value={draft.restaurant_budget_currency} aria-describedby={describedBy} aria-invalid={invalid || undefined} onChange={(e) => set("restaurant_budget_currency", e.target.value.toUpperCase())} />
              )}
            </Field>
            <Field label={tr("Hotel area", "숙소 지역")} className="span-4" error={shown.hotel_area} hint={tr("Neighbourhood you stay in (optional)", "머무는 동네 (선택)")}>
              {({ id, describedBy, invalid }) => (
                <input id={id} className="input" maxLength={200} placeholder={tr("Shinjuku", "신주쿠")} value={draft.hotel_area} aria-describedby={describedBy} aria-invalid={invalid || undefined} onChange={(e) => set("hotel_area", e.target.value)} />
              )}
            </Field>
            <Field label={tr("Must visit", "꼭 가고 싶은 곳")} className="span-4">
              {({ id, describedBy }) => (
                <TagInput id={id} describedBy={describedBy} value={draft.must_visit} onChange={(v) => set("must_visit", v)} transform={shortText} placeholder={tr("Senso-ji, teamLab…", "센소지, 팀랩…")} />
              )}
            </Field>
            <Field label={tr("Avoid places", "피하고 싶은 곳")} className="span-4">
              {({ id, describedBy }) => (
                <TagInput id={id} describedBy={describedBy} value={draft.avoid} onChange={(v) => set("avoid", v)} transform={shortText} placeholder={tr("crowded malls…", "붐비는 쇼핑몰…")} />
              )}
            </Field>
            <Field label={tr("Notes for the planner", "일정 작성 시 참고할 메모")} className="span-12" error={shown.notes}>
              {({ id, describedBy, invalid }) => (
                <textarea id={id} className="textarea" maxLength={4000} value={draft.notes} placeholder={tr("Anything else: mobility, kids' ages, events…", "그 밖에 참고할 점: 거동 불편, 아이 나이, 행사 등…")} aria-describedby={describedBy} aria-invalid={invalid || undefined} onChange={(e) => set("notes", e.target.value)} />
              )}
            </Field>
          </div>
        </div>
      </section>

      <section className="card plan-card">
        <div className="card__body section">
          <h2 className="section__title">
            <span className="plan-card__icon" aria-hidden>
              <Wallet size={17} />
            </span>
            {tr("Budget", "예산")}
          </h2>
          <p className="section__desc">
            {tr(
              "0 or -1 = unlimited (the planner picks a sensible mid-range). Blank = not specified.",
              "0 또는 -1 = 제한 없음 (적당한 중간 가격대로 계획해요). 비워 두면 지정하지 않은 것으로 봐요.",
            )}
          </p>
          <div className="form-grid">
            <Field label={tr("Budget is", "예산 기준")} className="span-6">
              {() => (
                <Segmented
                  label={tr("Budget basis", "예산 기준")}
                  value={draft.budget_basis}
                  onChange={(v) => set("budget_basis", v)}
                  options={[
                    { value: "total", label: tr("Total (all travellers)", "총액 (여행자 전체)") },
                    { value: "per_person", label: tr("Per person", "1인") },
                  ]}
                />
              )}
            </Field>
            <Field label={tr("Currency", "통화")} className="span-6" error={shown.budget_currency}>
              {({ id, describedBy, invalid }) => (
                <input id={id} className="input mono" maxLength={3} value={draft.budget_currency} aria-describedby={describedBy} aria-invalid={invalid || undefined} onChange={(e) => set("budget_currency", e.target.value.toUpperCase())} style={{ maxWidth: 120 }} />
              )}
            </Field>
            <Field label={tr("Total budget", "총예산")} required className="span-6" error={shown.budget_total} hint={budgetHint(draft.budget_total, cur)}>
              {({ id, describedBy, invalid }) => (
                <div className="input-group">
                  <span className="addon">{cur}</span>
                  <input id={id} className="input input--num" inputMode="decimal" placeholder="1500000" value={draft.budget_total} aria-describedby={describedBy} aria-invalid={invalid || undefined} onChange={(e) => set("budget_total", e.target.value)} />
                </div>
              )}
            </Field>
            <Field label={tr("Flight budget", "항공권 예산")} required className="span-6" error={shown.budget_flight} hint={budgetHint(draft.budget_flight, cur)}>
              {({ id, describedBy, invalid }) => (
                <div className="input-group">
                  <span className="addon">{cur}</span>
                  <input id={id} className="input input--num" inputMode="decimal" placeholder="350000" value={draft.budget_flight} aria-describedby={describedBy} aria-invalid={invalid || undefined} onChange={(e) => set("budget_flight", e.target.value)} />
                </div>
              )}
            </Field>
            {OPTIONAL_BUDGET_FIELDS.map((f) => (
              <Field key={f} label={BUDGET_LABEL[f]} className="span-4" error={shown[f]} hint={budgetHint(draft.optional[f], cur)}>
                {({ id, describedBy, invalid }) => (
                  <div className="input-group">
                    <span className="addon">{cur}</span>
                    <input id={id} className="input input--num" inputMode="decimal" value={draft.optional[f]} aria-describedby={describedBy} aria-invalid={invalid || undefined} onChange={(e) => setOptional(f, e.target.value)} />
                  </div>
                )}
              </Field>
            ))}
          </div>
          {categorySum > 0 && total !== null && total > 0 && (
            <p className={`small ${categorySum + (flight && flight > 0 ? flight : 0) > total ? "warn-text" : "subtle"}`}>
              {tr(
                `Flight + categories: ${formatMoney(categorySum + (flight && flight > 0 ? flight : 0), cur)} of ${formatMoney(total, cur)} total`,
                `항공권 + 항목별 예산: 총예산 ${formatMoney(total, cur)} 중 ${formatMoney(categorySum + (flight && flight > 0 ? flight : 0), cur)}`,
              )}
            </p>
          )}
        </div>
      </section>

      <ErrorBanner error={saveError} title={tr("Could not save preferences", "일정 계획을 저장하지 못했어요")} />
      {submitted && Object.keys(shown).length > 0 && !saveError && (
        <Banner tone="error" title={tr("Please fix the highlighted fields", "표시된 항목을 확인해 주세요")} />
      )}
      <div className="form-actions">
        <span className="small subtle" aria-live="polite">
          {savedAt
            ? tr(`Saved ${formatUtc(savedAt)}`, `저장됨 ${formatUtc(savedAt)}`)
            : stored
              ? tr(`Last saved ${formatUtc(stored.updated_at)}`, `마지막 저장 ${formatUtc(stored.updated_at)}`)
              : ""}
        </span>
        <Button variant="ghost" icon={<RotateCcw size={16} aria-hidden />} onClick={() => setDraftState(draftFrom(stored, defaults))}>
          {tr("Revert", "되돌리기")}
        </Button>
        <Button type="submit" variant="primary" busy={saving} icon={<Save size={16} aria-hidden />}>
          {tr("Save preferences", "저장")}
        </Button>
      </div>
    </form>
  );
}
