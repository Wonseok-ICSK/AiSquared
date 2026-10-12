/** Trip form "/trips/new" and "/trips/:id/edit" (spec §3.1 + flexible options + schedule). */

import { useEffect, useMemo, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { ArrowLeft, CalendarRange, Clock, NotebookPen, Plane, Plus, Save, Server, Users, Wallet } from "lucide-react";
import { api } from "../api/endpoints";
import { ApiError } from "../api/client";
import type { Cabin, FlexDays, PriceBasis, ProviderInfo, TripOut } from "../api/types";
import { CABINS, FLEX_DAYS, MAX_SCHEDULE_TIMES } from "../api/types";
import { AirportInput } from "../components/AirportInput";
import {
  Badge,
  Banner,
  Button,
  Checkbox,
  ErrorBanner,
  Field,
  Segmented,
  SkeletonBlock,
  Switch,
} from "../components/ui";
import { useAirport } from "../lib/airports";
import { useApp } from "../lib/appContext";
import { CABIN_LABEL, todaySeoul } from "../lib/format";
import { useTitle } from "../lib/hooks";
import { tr } from "../lib/i18n";
import { providerDescription, providerMessage, providerName } from "../lib/providerText";
import { naverTosWarning } from "../lib/trip";
import type { TimeWindowField, TripFormState } from "../lib/tripForm";
import {
  diffPayload,
  emptyTripForm,
  formToPayload,
  tripFormWarnings,
  tripToForm,
  validateTripForm,
} from "../lib/tripForm";
import type { Errors } from "../lib/validation";
import { isIataCode, normalizeHHMM } from "../lib/validation";

const CURRENCIES = ["KRW", "JPY", "USD", "EUR", "CNY", "TWD", "HKD", "THB", "VND", "SGD", "GBP", "AUD"];

const TIME_ROWS: { label: readonly [en: string, ko: string]; from: TimeWindowField; to: TimeWindowField; leg: "out" | "ret" }[] = [
  { label: ["Outbound departs", "가는 편 출발"], from: "outbound_depart_from", to: "outbound_depart_to", leg: "out" },
  { label: ["Outbound arrives", "가는 편 도착"], from: "outbound_arrive_from", to: "outbound_arrive_to", leg: "out" },
  { label: ["Return departs", "오는 편 출발"], from: "return_depart_from", to: "return_depart_to", leg: "ret" },
  { label: ["Return arrives", "오는 편 도착"], from: "return_arrive_from", to: "return_arrive_to", leg: "ret" },
];

function FormSection({ icon, title, desc, children }: { icon: ReactNode; title: string; desc?: string; children: ReactNode }) {
  return (
    <section className="card form-card">
      <div className="card__body section">
        <div className="form-card__head">
          <span className="form-card__icon" aria-hidden>
            {icon}
          </span>
          <div className="stack-xs">
            <h2 className="form-card__title">{title}</h2>
            {desc && <p className="form-card__desc">{desc}</p>}
          </div>
        </div>
        {children}
      </div>
    </section>
  );
}

function flexOptions(): { value: FlexDays; label: string }[] {
  return FLEX_DAYS.map((d) => ({ value: d, label: d === 0 ? tr("Exact", "정확히") : tr(`±${d}`, `±${d}일`) }));
}

function ScheduleTimes({
  value,
  onChange,
  error,
}: {
  value: string[];
  onChange: (times: string[]) => void;
  error?: string;
}) {
  const [draft, setDraft] = useState("");
  const add = () => {
    const t = normalizeHHMM(draft);
    if (!t || value.includes(t) || value.length >= MAX_SCHEDULE_TIMES) return;
    onChange([...value, t].sort());
    setDraft("");
  };
  return (
    <Field
      label={tr("Check at (Asia/Seoul)", "확인 시각 (한국 시간)")}
      error={error}
      hint={tr(`Up to ${MAX_SCHEDULE_TIMES} times a day.`, `하루 최대 ${MAX_SCHEDULE_TIMES}회까지 설정할 수 있어요.`)}
    >
      {({ id, describedBy, invalid }) => (
        <div className="stack-sm">
          <div className="row">
            {value.map((t) => (
              <span className="chip" key={t}>
                <span className="chip__text num">{t}</span>
                <button type="button" aria-label={tr(`Remove ${t}`, `${t} 삭제`)} onClick={() => onChange(value.filter((v) => v !== t))}>
                  ×
                </button>
              </span>
            ))}
            {value.length === 0 && <span className="small subtle">{tr("No times yet", "아직 설정된 시각이 없어요")}</span>}
          </div>
          <div className="row">
            <input
              id={id}
              type="time"
              className="input"
              style={{ width: 140 }}
              value={draft}
              aria-describedby={describedBy}
              aria-invalid={invalid || undefined}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  add();
                }
              }}
            />
            <Button size="sm" icon={<Plus size={14} aria-hidden />} onClick={add} disabled={!normalizeHHMM(draft)}>
              {tr("Add time", "시각 추가")}
            </Button>
          </div>
        </div>
      )}
    </Field>
  );
}

function ProviderPicker({
  providers,
  selected,
  onChange,
  acknowledged,
  onAcknowledge,
  error,
}: {
  providers: ProviderInfo[] | undefined;
  selected: string[];
  onChange: (keys: string[]) => void;
  acknowledged: boolean;
  onAcknowledge: (v: boolean) => void;
  error?: string;
}) {
  if (!providers) return <SkeletonBlock lines={3} label={tr("Loading providers", "검색 소스 불러오는 중")} />;
  const defaults = providers.filter((p) => p.enabled).map((p) => providerName(p));
  const toggle = (key: string, on: boolean) =>
    onChange(on ? [...selected, key] : selected.filter((k) => k !== key));
  return (
    <fieldset className="group" aria-describedby="providers-hint">
      <legend>{tr("Flight sources", "항공권 검색 소스")}</legend>
      <div className="provider-picks">
        {providers.map((p) => (
          <label key={p.key} className={`provider-pick${selected.includes(p.key) ? " is-on" : ""}`}>
            <input
              type="checkbox"
              checked={selected.includes(p.key)}
              onChange={(e) => toggle(p.key, e.target.checked)}
            />
            <span className="provider-pick__body">
              <span className="row" style={{ gap: 6 }}>
                <span className="strong">{providerName(p)}</span>
                {p.configured ? (
                  <Badge tone="good">{tr("Ready", "사용 가능")}</Badge>
                ) : (
                  <Badge tone="warn" title={providerMessage(p.message)}>
                    {tr("Not configured", "설정 필요")}
                  </Badge>
                )}
                {p.enabled && <Badge tone="info">{tr("Default", "기본")}</Badge>}
              </span>
              {p.description && <span className="xsmall subtle">{providerDescription(p)}</span>}
            </span>
          </label>
        ))}
      </div>
      <p id="providers-hint" className="field__hint" style={{ marginTop: 6 }}>
        {selected.length === 0
          ? tr(
              `None selected: the default sources are used (${defaults.join(", ") || "none configured"}).`,
              `선택하지 않으면 기본 검색 소스를 사용해요 (${defaults.join(", ") || "설정된 소스 없음"}).`,
            )
          : tr(
              "Only the selected sources are searched. Unconfigured sources are skipped.",
              "선택한 소스에서만 검색해요. 설정되지 않은 소스는 건너뛰어요.",
            )}
      </p>
      {selected.includes("naver") && (
        <div className="stack-sm" style={{ marginTop: 10 }}>
          <Banner tone="warn" title={tr("Naver Flights terms of service", "Naver 항공권 이용약관")}>
            {naverTosWarning()}
          </Banner>
          <Checkbox
            checked={acknowledged}
            onChange={onAcknowledge}
            label={tr(
              "I understand and want to use Naver Flights for this trip (personal, low-volume use).",
              "내용을 이해했으며 이 여행에 Naver 항공권 검색을 사용할게요 (개인용, 적은 횟수).",
            )}
          />
        </div>
      )}
      {error && <span className="field__error">{error}</span>}
    </fieldset>
  );
}

/** Create/edit trip page. */
export function TripForm() {
  const params = useParams();
  const editId = params.id ? Number(params.id) : null;
  const navigate = useNavigate();
  const { defaults } = useApp();
  useTitle(editId ? tr("Edit trip", "여행 수정") : tr("New trip", "새 여행"));

  const [original, setOriginal] = useState<TripOut | null>(null);
  const [form, setForm] = useState<TripFormState>(() => emptyTripForm(defaults));
  const [loadError, setLoadError] = useState<unknown>(null);
  const [loading, setLoading] = useState(Boolean(editId));
  const [providers, setProviders] = useState<ProviderInfo[] | undefined>(undefined);
  const [providersError, setProvidersError] = useState<unknown>(null);
  const [naverAck, setNaverAck] = useState(false);
  const [errors, setErrors] = useState<Errors>({});
  const [submitted, setSubmitted] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<unknown>(null);
  const [touchedDefaults, setTouchedDefaults] = useState(false);

  const originAirport = useAirport(isIataCode(form.origin) ? form.origin : null);
  const destAirport = useAirport(isIataCode(form.destination) ? form.destination : null);

  // meta defaults may arrive after first render (new trip only)
  useEffect(() => {
    if (!editId && !touchedDefaults) setForm(emptyTripForm(defaults));
  }, [defaults, editId, touchedDefaults]);

  useEffect(() => {
    api
      .listProviders()
      .then(setProviders)
      .catch((err) => {
        setProviders([]);
        setProvidersError(err);
      });
  }, []);

  useEffect(() => {
    if (!editId) return;
    setLoading(true);
    api
      .getTrip(editId)
      .then((trip) => {
        setOriginal(trip);
        setForm(tripToForm(trip));
        setNaverAck(trip.providers.includes("naver"));
      })
      .catch(setLoadError)
      .finally(() => setLoading(false));
  }, [editId]);

  const update = <K extends keyof TripFormState>(key: K, value: TripFormState[K]) => {
    setTouchedDefaults(true);
    setErrors((prev) => (Object.keys(prev).length ? {} : prev));
    setForm((f) => ({ ...f, [key]: value }));
  };
  const setTime = (field: TimeWindowField, value: string) =>
    setForm((f) => ({ ...f, times: { ...f.times, [field]: value } }));

  const liveErrors = useMemo(
    () => validateTripForm(form, { origin: originAirport, destination: destAirport }, naverAck),
    [form, originAirport, destAirport, naverAck],
  );
  const shownErrors = submitted ? { ...errors, ...liveErrors } : errors;
  const warnings = tripFormWarnings(form, todaySeoul());
  const err = (key: string) => shownErrors[key];

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setSubmitted(true);
    setSaveError(null);
    setErrors({});
    if (Object.keys(liveErrors).length > 0) {
      requestAnimationFrame(() => {
        document.querySelector<HTMLElement>('.trip-form [aria-invalid="true"]')?.focus();
      });
      return;
    }
    const payload = formToPayload(form);
    setSaving(true);
    try {
      const saved = original
        ? await api.updateTrip(original.id, diffPayload(original, payload))
        : await api.createTrip(payload);
      navigate(`/trips/${saved.id}`);
    } catch (error) {
      setSaveError(error);
      if (error instanceof ApiError) setErrors(error.fieldErrors);
    } finally {
      setSaving(false);
    }
  };

  if (loading) {
    return (
      <div className="card">
        <div className="card__body">
          <SkeletonBlock lines={6} label={tr("Loading trip", "여행 불러오는 중")} />
        </div>
      </div>
    );
  }
  if (loadError) return <ErrorBanner error={loadError} title={tr("Could not load this trip", "여행 정보를 불러오지 못했어요")} />;

  const roundTrip = form.trip_type === "round_trip";
  const errorCount = Object.keys(shownErrors).length;

  return (
    <form className="trip-form" onSubmit={submit} noValidate>
      <div className="page-head form-head">
        <div className="page-head__title">
          <Link className="breadcrumb" to={original ? `/trips/${original.id}` : "/"}>
            <ArrowLeft size={14} aria-hidden /> {original ? original.name : tr("Trips", "내 여행")}
          </Link>
          <span className="form-head__eyebrow">
            {original ? tr("Fine-tune your trip", "여행 조건 다듬기") : tr("Plan a new trip", "새 여행 준비")}
          </span>
          <h1>{original ? tr("Edit trip", "여행 수정") : tr("New monitored trip", "새 가격 추적 여행")}</h1>
          <p className="form-head__lede">
            {tr(
              "Set the route, dates and target price — fares are checked on your schedule.",
              "노선과 날짜, 목표 가격을 정하면 정해진 주기마다 항공권 가격을 확인해요.",
            )}
          </p>
        </div>
      </div>

      <div className="stack">
        <FormSection icon={<Plane size={18} aria-hidden />} title={tr("Route & dates", "노선 및 날짜")}>
          <div className="form-grid">
            <Field
              label={tr("From", "출발지")}
              required
              error={err("origin")}
              className="span-6"
              hint={tr("Airport (ICN) or city code (SEL = all Seoul airports)", "공항 코드(ICN) 또는 도시 코드(SEL = 서울 전체 공항)")}
            >
              {({ id, describedBy, invalid }) => (
                <AirportInput
                  id={id}
                  value={form.origin}
                  describedBy={describedBy}
                  invalid={invalid}
                  onChange={(code) => update("origin", code)}
                />
              )}
            </Field>
            <Field
              label={tr("To", "도착지")}
              required
              error={err("destination")}
              className="span-6"
              hint={tr("e.g. TYO = Narita + Haneda", "예: TYO = 나리타 + 하네다")}
            >
              {({ id, describedBy, invalid }) => (
                <AirportInput
                  id={id}
                  value={form.destination}
                  describedBy={describedBy}
                  invalid={invalid}
                  onChange={(code) => update("destination", code)}
                />
              )}
            </Field>

            <div className="span-12">
              <Segmented
                label={tr("Trip type", "여정 유형")}
                value={form.trip_type}
                onChange={(v) => update("trip_type", v)}
                options={[
                  { value: "round_trip", label: tr("Round trip", "왕복") },
                  { value: "one_way", label: tr("One way", "편도") },
                ]}
              />
            </div>

            <Field label={tr("Departure date", "가는 날")} required error={err("depart_date")} className="span-3">
              {({ id, describedBy, invalid }) => (
                <input
                  id={id}
                  type="date"
                  className="input"
                  value={form.depart_date}
                  aria-describedby={describedBy}
                  aria-invalid={invalid || undefined}
                  onChange={(e) => update("depart_date", e.target.value)}
                />
              )}
            </Field>
            <Field
              label={tr("Flexible departure", "가는 날 유연성")}
              className="span-3"
              hint={tr("Search nearby dates too", "앞뒤 날짜도 함께 검색해요")}
            >
              {() => (
                <Segmented
                  label={tr("Flexible departure (days)", "가는 날 유연성 (일)")}
                  value={form.flex_depart_days}
                  options={flexOptions()}
                  onChange={(v) => update("flex_depart_days", v)}
                />
              )}
            </Field>
            {roundTrip && (
              <>
                <Field label={tr("Return date", "오는 날")} required error={err("return_date")} className="span-3">
                  {({ id, describedBy, invalid }) => (
                    <input
                      id={id}
                      type="date"
                      className="input"
                      min={form.depart_date || undefined}
                      value={form.return_date}
                      aria-describedby={describedBy}
                      aria-invalid={invalid || undefined}
                      onChange={(e) => update("return_date", e.target.value)}
                    />
                  )}
                </Field>
                <Field label={tr("Flexible return", "오는 날 유연성")} className="span-3">
                  {() => (
                    <Segmented
                      label={tr("Flexible return (days)", "오는 날 유연성 (일)")}
                      value={form.flex_return_days}
                      options={flexOptions()}
                      onChange={(v) => update("flex_return_days", v)}
                    />
                  )}
                </Field>
              </>
            )}

            <div className="span-12">
              <Checkbox
                checked={form.include_nearby_airports}
                onChange={(v) => update("include_nearby_airports", v)}
                label={tr("Include nearby airports", "인근 공항 포함")}
                hint={tr(
                  "Also search airports within ~100 km of the origin and destination.",
                  "출발지와 도착지에서 약 100km 이내의 공항도 함께 검색해요.",
                )}
              />
            </div>
            <Field
              label={tr("Alternative destination airports", "대체 도착 공항")}
              className="span-12"
              error={err("alt_destinations")}
              hint={tr(
                "Optional. Searched in addition to the main destination (max 10).",
                "선택 사항이에요. 기본 도착지와 함께 검색해요 (최대 10개).",
              )}
            >
              {({ id, describedBy }) => (
                <div className="stack-sm">
                  {form.alt_destinations.length > 0 && (
                    <div className="row">
                      {form.alt_destinations.map((code) => (
                        <span className="chip" key={code}>
                          <span className="chip__text mono">{code}</span>
                          <button
                            type="button"
                            aria-label={tr(`Remove ${code}`, `${code} 삭제`)}
                            onClick={() => update("alt_destinations", form.alt_destinations.filter((c) => c !== code))}
                          >
                            ×
                          </button>
                        </span>
                      ))}
                    </div>
                  )}
                  <AirportInput
                    id={id}
                    value=""
                    clearOnSelect
                    describedBy={describedBy}
                    placeholder={tr("Add an airport (e.g. KIX, FUK)", "공항 추가 (예: KIX, FUK)")}
                    onChange={(code) => {
                      if (code && !form.alt_destinations.includes(code) && form.alt_destinations.length < 10) {
                        update("alt_destinations", [...form.alt_destinations, code]);
                      }
                    }}
                  />
                </div>
              )}
            </Field>
            <Field
              label={tr("Trip name", "여행 이름")}
              className="span-12"
              error={err("name")}
              hint={tr("Optional. Leave blank for “ORIGIN → DESTINATION”.", "선택 사항이에요. 비워 두면 “출발지 → 도착지”로 표시돼요.")}
            >
              {({ id, describedBy, invalid }) => (
                <input
                  id={id}
                  className="input"
                  value={form.name}
                  maxLength={200}
                  placeholder={form.origin && form.destination ? `${form.origin} → ${form.destination}` : tr("e.g. Tokyo food trip", "예: 도쿄 맛집 여행")}
                  aria-describedby={describedBy}
                  aria-invalid={invalid || undefined}
                  onChange={(e) => update("name", e.target.value)}
                />
              )}
            </Field>
          </div>
        </FormSection>

        <FormSection icon={<Users size={18} aria-hidden />} title={tr("Passengers & flights", "인원 및 항공편")}>
          <div className="form-grid">
            <Field label={tr("Adults", "인원")} className="span-3" error={err("adults")}>
              {() => (
                <Segmented
                  label={tr("Adults", "인원")}
                  value={form.adults}
                  onChange={(v) => update("adults", v)}
                  options={[
                    { value: 1, label: tr("1 adult", "성인 1명") },
                    { value: 2, label: tr("2 adults", "성인 2명") },
                  ]}
                />
              )}
            </Field>
            <Field label={tr("Cabin", "좌석 등급")} className="span-3">
              {({ id }) => (
                <select id={id} className="select" value={form.cabin} onChange={(e) => update("cabin", e.target.value as Cabin)}>
                  {CABINS.map((c) => (
                    <option key={c} value={c}>
                      {CABIN_LABEL[c]}
                    </option>
                  ))}
                </select>
              )}
            </Field>
            <Field
              label={tr("Max stops", "최대 경유 횟수")}
              className="span-3"
              hint={form.direct_only ? tr("Ignored: direct flights only", "직항만 검색하므로 적용되지 않아요") : undefined}
            >
              {({ id, describedBy }) => (
                <select
                  id={id}
                  className="select"
                  value={form.direct_only ? "0" : form.max_stops}
                  disabled={form.direct_only}
                  aria-describedby={describedBy}
                  onChange={(e) => update("max_stops", e.target.value)}
                >
                  <option value="">{tr("Any", "제한 없음")}</option>
                  <option value="0">{tr("Direct (0)", "직항 (0회)")}</option>
                  <option value="1">{tr("Up to 1", "최대 1회")}</option>
                  <option value="2">{tr("Up to 2", "최대 2회")}</option>
                  <option value="3">{tr("Up to 3", "최대 3회")}</option>
                </select>
              )}
            </Field>
            <div className="span-3 field" style={{ justifyContent: "flex-end", paddingBottom: 8 }}>
              <Checkbox
                checked={form.direct_only}
                onChange={(v) => update("direct_only", v)}
                label={tr("Direct flights only", "직항만")}
              />
            </div>

            <fieldset className="group span-12">
              <legend>
                <Clock size={14} aria-hidden style={{ verticalAlign: -2 }} />{" "}
                {tr("Preferred times (airport-local, optional)", "희망 시간대 (공항 현지 시각, 선택 사항)")}
              </legend>
              <div className="time-windows">
                {TIME_ROWS.filter((r) => roundTrip || r.leg === "out").map((row) => {
                  const label = tr(...row.label);
                  return (
                  <div className="time-window" key={row.from}>
                    <span className="time-window__label">{label}</span>
                    <div className="time-window__inputs">
                      <input
                        type="time"
                        className="input"
                        aria-label={tr(`${label} from`, `${label} 시작 시각`)}
                        value={form.times[row.from]}
                        aria-invalid={Boolean(err(`times.${row.from}`)) || undefined}
                        onChange={(e) => setTime(row.from, e.target.value)}
                      />
                      <span className="subtle" aria-hidden>
                        –
                      </span>
                      <input
                        type="time"
                        className="input"
                        aria-label={tr(`${label} until`, `${label} 종료 시각`)}
                        value={form.times[row.to]}
                        aria-invalid={Boolean(err(`times.${row.to}`)) || undefined}
                        onChange={(e) => setTime(row.to, e.target.value)}
                      />
                    </div>
                    {(err(`times.${row.from}`) || err(`times.${row.to}`)) && (
                      <span className="field__error">{err(`times.${row.from}`) ?? err(`times.${row.to}`)}</span>
                    )}
                  </div>
                  );
                })}
              </div>
            </fieldset>
          </div>
        </FormSection>

        <FormSection
          icon={<Wallet size={18} aria-hidden />}
          title={tr("Price", "가격")}
          desc={tr(
            "Offers above the maximum are ignored. You are notified when the price falls to the target.",
            "최대 가격보다 비싼 항공권은 제외돼요. 가격이 목표 가격까지 내려가면 알려 드려요.",
          )}
        >
          <div className="form-grid">
            <Field label={tr("Currency", "통화")} className="span-3" error={err("currency")}>
              {({ id, describedBy, invalid }) => (
                <>
                  <input
                    id={id}
                    className="input mono"
                    list="currency-list"
                    maxLength={3}
                    value={form.currency}
                    aria-describedby={describedBy}
                    aria-invalid={invalid || undefined}
                    onChange={(e) => update("currency", e.target.value.toUpperCase())}
                  />
                  <datalist id="currency-list">
                    {CURRENCIES.map((c) => (
                      <option key={c} value={c} />
                    ))}
                  </datalist>
                </>
              )}
            </Field>
            <Field label={tr("Prices are", "가격 기준")} className="span-3">
              {() => (
                <Segmented
                  label={tr("Price basis", "가격 기준")}
                  value={form.price_basis}
                  onChange={(v: PriceBasis) => update("price_basis", v)}
                  options={[
                    { value: "per_person", label: tr("Per person", "1인") },
                    { value: "total", label: tr("Total", "총액") },
                  ]}
                />
              )}
            </Field>
            <Field
              label={tr("Target price", "목표 가격")}
              className="span-3"
              error={err("target_price")}
              hint={tr("Alert when at or below", "이 가격 이하가 되면 알려 드려요")}
            >
              {({ id, describedBy, invalid }) => (
                <div className="input-group">
                  <span className="addon">{form.currency || "—"}</span>
                  <input
                    id={id}
                    className="input input--num"
                    inputMode="decimal"
                    placeholder="300000"
                    value={form.target_price}
                    aria-describedby={describedBy}
                    aria-invalid={invalid || undefined}
                    onChange={(e) => update("target_price", e.target.value)}
                  />
                </div>
              )}
            </Field>
            <Field
              label={tr("Maximum price", "최대 가격")}
              className="span-3"
              error={err("max_price")}
              hint={tr("Optional cap", "선택 사항 (상한선)")}
            >
              {({ id, describedBy, invalid }) => (
                <div className="input-group">
                  <span className="addon">{form.currency || "—"}</span>
                  <input
                    id={id}
                    className="input input--num"
                    inputMode="decimal"
                    placeholder={tr("No limit", "제한 없음")}
                    value={form.max_price}
                    aria-describedby={describedBy}
                    aria-invalid={invalid || undefined}
                    onChange={(e) => update("max_price", e.target.value)}
                  />
                </div>
              )}
            </Field>
          </div>
        </FormSection>

        <FormSection icon={<Server size={18} aria-hidden />} title={tr("Sources", "검색 소스")}>
          {providersError ? <ErrorBanner error={providersError} title={tr("Could not load providers", "검색 소스를 불러오지 못했어요")} /> : null}
          <ProviderPicker
            providers={providers}
            selected={form.providers}
            onChange={(keys) => update("providers", keys)}
            acknowledged={naverAck}
            onAcknowledge={setNaverAck}
            error={err("providers")}
          />
        </FormSection>

        <FormSection icon={<CalendarRange size={18} aria-hidden />} title={tr("Schedule", "확인 주기")}>
          <div className="form-grid">
            <div className="span-12 row-between">
              <Segmented
                label={tr("Schedule mode", "확인 방식")}
                value={form.schedule_mode}
                onChange={(v) => update("schedule_mode", v)}
                options={[
                  { value: "times", label: tr("At set times", "정해진 시각") },
                  { value: "interval", label: tr("Every N hours", "N시간마다") },
                ]}
              />
              <Switch
                checked={form.active}
                onChange={(v) => update("active", v)}
                label={form.active ? tr("Monitoring active", "가격 추적 중") : tr("Paused", "일시 중지")}
              />
            </div>
            {form.schedule_mode === "times" ? (
              <div className="span-12 stack-sm">
                <ScheduleTimes value={form.schedule_times} onChange={(t) => update("schedule_times", t)} error={err("schedule_times")} />
                <div className="row">
                  <span className="xsmall subtle">{tr("Presets:", "빠른 설정:")}</span>
                  <Button size="sm" variant="ghost" onClick={() => update("schedule_times", ["08:30"])}>
                    {tr("Daily 08:30", "매일 08:30")}
                  </Button>
                  <Button size="sm" variant="ghost" onClick={() => update("schedule_times", ["08:30", "17:30"])}>
                    {tr("Twice a day", "매일 2회")}
                  </Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={() => {
                      update("schedule_mode", "interval");
                      update("schedule_interval_hours", "6");
                    }}
                  >
                    {tr("Every 6 hours", "6시간마다")}
                  </Button>
                </div>
              </div>
            ) : (
              <Field
                label={tr("Interval", "간격")}
                className="span-4"
                error={err("schedule_interval_hours")}
                hint={tr("1–168 hours", "1–168시간")}
              >
                {({ id, describedBy, invalid }) => (
                  <div className="input-group">
                    <input
                      id={id}
                      className="input input--num"
                      inputMode="decimal"
                      value={form.schedule_interval_hours}
                      placeholder="6"
                      aria-describedby={describedBy}
                      aria-invalid={invalid || undefined}
                      onChange={(e) => update("schedule_interval_hours", e.target.value)}
                    />
                    <span className="addon">{tr("hours", "시간")}</span>
                  </div>
                )}
              </Field>
            )}
          </div>
        </FormSection>

        <FormSection icon={<NotebookPen size={18} aria-hidden />} title={tr("Notes", "메모")}>
          <Field
            label={tr("Notes", "메모")}
            error={err("notes")}
            hint={tr("Private notes (max 4000 characters).", "나만 보는 메모예요 (최대 4000자).")}
          >
            {({ id, describedBy, invalid }) => (
              <textarea
                id={id}
                className="textarea"
                value={form.notes}
                maxLength={4000}
                aria-describedby={describedBy}
                aria-invalid={invalid || undefined}
                onChange={(e) => update("notes", e.target.value)}
              />
            )}
          </Field>
        </FormSection>

        {warnings.map((w) => (
          <Banner key={w} tone="warn">
            {w}
          </Banner>
        ))}
        <ErrorBanner error={saveError} title={tr("Could not save the trip", "여행을 저장하지 못했어요")} />
        {submitted && errorCount > 0 && !saveError && (
          <Banner tone="error" title={tr("Please fix the highlighted fields", "표시된 항목을 확인해 주세요")}>
            {tr(
              `${errorCount} field${errorCount === 1 ? "" : "s"} need attention.`,
              `${errorCount}개 항목을 수정해야 해요.`,
            )}
          </Banner>
        )}
      </div>

      <div className="form-actions">
        <Link className="btn btn--ghost" to={original ? `/trips/${original.id}` : "/"}>
          {tr("Cancel", "취소")}
        </Link>
        <Button type="submit" variant="primary" busy={saving} icon={<Save size={16} aria-hidden />}>
          {original ? tr("Save changes", "변경 사항 저장") : tr("Create trip", "여행 만들기")}
        </Button>
      </div>
    </form>
  );
}
