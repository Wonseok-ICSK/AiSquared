/** Itinerary view "/itineraries/:id": day-by-day timeline, budget, metrics, refine box. */

import { useState } from "react";
import type { FormEvent, ReactElement } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import {
  ArrowLeft,
  BedDouble,
  CloudSun,
  Coffee,
  Compass,
  ExternalLink,
  GitBranch,
  Landmark,
  Lightbulb,
  Map as MapIcon,
  Navigation,
  PlaneLanding,
  PlaneTakeoff,
  Route as RouteIcon,
  Sparkles,
  TrainFront,
  UtensilsCrossed,
  Wallet,
  Wand2,
} from "lucide-react";
import { api } from "../api/endpoints";
import { ApiError } from "../api/client";
import type { BudgetSummary, ItineraryOut, PlanDay, PlanItem, PlanItemKind, TravelHop } from "../api/types";
import {
  Badge,
  Banner,
  Button,
  ErrorBanner,
  Field,
  SkeletonBlock,
  Spinner,
  StatusChip,
} from "../components/ui";
import {
  ITINERARY_STATUS_LABEL,
  formatDate,
  formatLocalDateTime,
  formatMinutes,
  formatMoney,
  formatNumber,
  formatUtc,
  humanize,
} from "../lib/format";
import { useAsync, usePoll, useTitle } from "../lib/hooks";
import { getLang, labels, tr } from "../lib/i18n";
import { costBasisLabel, planAttributions } from "../lib/itinerary";

const KIND_ICON: Record<PlanItemKind, ReactElement> = {
  arrival: <PlaneLanding size={16} aria-hidden />,
  transit: <TrainFront size={16} aria-hidden />,
  hotel: <BedDouble size={16} aria-hidden />,
  activity: <Landmark size={16} aria-hidden />,
  meal: <UtensilsCrossed size={16} aria-hidden />,
  free: <Coffee size={16} aria-hidden />,
  departure: <PlaneTakeoff size={16} aria-hidden />,
};

const KIND_LABEL = labels<PlanItemKind>({
  arrival: ["Arrival", "도착"],
  transit: ["Transit", "이동"],
  hotel: ["Hotel", "숙소"],
  activity: ["Activity", "명소·활동"],
  meal: ["Meal", "식사"],
  free: ["Free time", "자유 시간"],
  departure: ["Departure", "출발"],
});

/** Emoji for a travel mode, as in "🚇 18 min · 6.2 km". */
export function hopEmoji(mode: string): string {
  const m = mode.toLowerCase();
  if (m.includes("walk")) return "🚶";
  if (m.includes("bus")) return "🚌";
  if (m.includes("taxi") || m.includes("car") || m.includes("drive")) return "🚕";
  if (m.includes("train") || m.includes("rail")) return "🚆";
  if (m.includes("ferry") || m.includes("boat")) return "⛴️";
  return "🚇";
}

const BUDGET_CATEGORY_KO: Record<string, string> = {
  flight: "항공",
  hotel: "숙소",
  food: "식비",
  transport: "교통",
  activities: "관광·체험",
  shopping: "쇼핑",
  other: "기타",
};

/** Budget category name ("food" -> "Food" / "식비"). */
function budgetCategoryLabel(category: string): string {
  if (getLang() === "ko") return BUDGET_CATEGORY_KO[category] ?? humanize(category);
  return humanize(category);
}

/** Screen-reader name of a travel mode: "by walk" / "도보". */
function hopModeLabel(mode: string): string {
  if (getLang() !== "ko") return `by ${mode}`;
  const m = mode.toLowerCase();
  if (m.includes("walk")) return "도보";
  if (m.includes("bus")) return "버스";
  if (m.includes("taxi") || m.includes("car") || m.includes("drive")) return "택시";
  if (m.includes("subway") || m.includes("metro")) return "지하철";
  if (m.includes("train") || m.includes("rail")) return "기차";
  if (m.includes("ferry") || m.includes("boat")) return "배";
  return "대중교통";
}

function hopText(hop: TravelHop): string {
  return `${hopEmoji(hop.mode)} ${formatMinutes(hop.minutes)} · ${hop.distance_km.toFixed(1)} km`;
}

function directionsUrl(prev: PlanItem | undefined, item: PlanItem): string | null {
  const explicit = item["directions_url"];
  if (typeof explicit === "string" && explicit) return explicit;
  if (!prev || prev.lat === null || prev.lng === null || item.lat === null || item.lng === null) return null;
  const mode = item.travel_from_prev?.mode.toLowerCase().includes("walk") ? "walking" : "transit";
  return `https://www.google.com/maps/dir/?api=1&origin=${prev.lat},${prev.lng}&destination=${item.lat},${item.lng}&travelmode=${mode}`;
}

function hostOf(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return url;
  }
}

function Weather({ weather }: { weather: PlanDay["weather"] }) {
  if (!weather) return null;
  const parts: string[] = [];
  if (typeof weather.summary === "string" && weather.summary) parts.push(weather.summary);
  if (typeof weather.temp_min_c === "number" && typeof weather.temp_max_c === "number") {
    parts.push(`${Math.round(weather.temp_min_c)}–${Math.round(weather.temp_max_c)}°C`);
  }
  if (typeof weather.precip_mm === "number") parts.push(`${weather.precip_mm.toFixed(1)} mm`);
  if (parts.length === 0) return null;
  return (
    <span className="day__weather" title={tr("Weather forecast or climate average", "일기예보 또는 평년 기후")}>
      <CloudSun size={15} aria-hidden /> {parts.join(" · ")}
    </span>
  );
}

function TimelineItem({
  item,
  prev,
  currency,
  costBasis,
}: {
  item: PlanItem;
  prev?: PlanItem;
  currency: string;
  /** "per person" / "total" label for the cost estimate, when the plan states one. */
  costBasis: string | null;
}) {
  const directions = directionsUrl(prev, item);
  const perPerson = costBasis !== null && costBasis === tr("per person", "1인");
  return (
    <li className={`tl-item tl-item--${item.kind}`}>
      {item.travel_from_prev && (
        <div className="tl-hop">
          <span>{hopText(item.travel_from_prev)}</span>
          <span className="sr-only"> ({hopModeLabel(item.travel_from_prev.mode)})</span>
          {directions && (
            <a href={directions} target="_blank" rel="noopener noreferrer" className="tl-link">
              <Navigation size={12} aria-hidden /> {tr("Directions", "길찾기")}
            </a>
          )}
        </div>
      )}
      <div className="tl-row">
        <div className="tl-time num">
          <span className="strong">{item.start}</span>
          <span className="subtle">{item.end}</span>
        </div>
        <div className="tl-dot" aria-hidden>
          {KIND_ICON[item.kind]}
        </div>
        <div className="tl-body">
          <div className="row" style={{ gap: 6 }}>
            <span className="tl-title">{item.title}</span>
            <span className="sr-only">({KIND_LABEL[item.kind]})</span>
            {item.area && <Badge tone="outline">{item.area}</Badge>}
          </div>
          {item.description && <p className="tl-desc">{item.description}</p>}
          {item.tips && (
            <p className="tl-tip">
              <Lightbulb size={13} aria-hidden /> {item.tips}
            </p>
          )}
          <div className="tl-meta">
            {item.est_cost !== null && item.est_cost !== undefined && (
              <span
                title={
                  costBasis
                    ? tr(`Estimated cost, ${costBasis}`, `예상 비용 (${costBasis})`)
                    : tr("Estimated cost", "예상 비용")
                }
              >
                ~{formatMoney(item.est_cost, item.cost_currency ?? currency)}
                {costBasis && <span className="subtle"> {perPerson ? tr("/ person", "/ 1인") : costBasis}</span>}
              </span>
            )}
            {item.map_url && (
              <a href={item.map_url} target="_blank" rel="noopener noreferrer" className="tl-link">
                <MapIcon size={12} aria-hidden /> {tr("Map", "지도")}
              </a>
            )}
            {!item.travel_from_prev && directions && (
              <a href={directions} target="_blank" rel="noopener noreferrer" className="tl-link">
                <Navigation size={12} aria-hidden /> {tr("Directions", "길찾기")}
              </a>
            )}
            {item.source_urls.map((url, i) => (
              <a key={url} href={url} target="_blank" rel="noopener noreferrer" className="tl-link" title={url}>
                <ExternalLink size={12} aria-hidden /> {hostOf(url)}
                <span className="sr-only">{tr(` (source ${i + 1})`, ` (출처 ${i + 1})`)}</span>
              </a>
            ))}
          </div>
        </div>
      </div>
    </li>
  );
}

function DayCard({
  day,
  index,
  currency,
  costBasis,
}: {
  day: PlanDay;
  index: number;
  currency: string;
  costBasis: string | null;
}) {
  return (
    <section className="card day" aria-labelledby={`day-${index}`}>
      <header className="day__head">
        <span className="day__num" aria-hidden>
          {getLang() === "ko" ? (
            <>
              <span className="day__num-value">{index + 1}</span>
              <span className="day__num-label">일차</span>
            </>
          ) : (
            <>
              <span className="day__num-label">Day</span>
              <span className="day__num-value">{index + 1}</span>
            </>
          )}
        </span>
        <div className="stack-xs grow">
          <h3 id={`day-${index}`}>
            <span className="sr-only">{tr(`Day ${index + 1} · `, `${index + 1}일차 · `)}</span>
            {formatDate(day.date)}
          </h3>
          {day.theme && <span className="small muted">{day.theme}</span>}
          {day.areas.length > 0 && (
            <span className="row" style={{ gap: 4 }}>
              {day.areas.map((a) => (
                <Badge key={a}>{a}</Badge>
              ))}
            </span>
          )}
        </div>
        <div className="day__side">
          <Weather weather={day.weather} />
          {day.est_day_cost !== null && day.est_day_cost !== undefined && (
            <span className="small subtle">
              {tr(
                `Est. ${formatMoney(day.est_day_cost, currency)}${costBasis ? ` ${costBasis}` : ""}`,
                `예상 ${formatMoney(day.est_day_cost, currency)}${costBasis ? ` (${costBasis})` : ""}`,
              )}
            </span>
          )}
        </div>
      </header>
      <ol className="timeline">
        {day.items.map((item, i) => (
          <TimelineItem
            key={`${item.start}-${i}`}
            item={item}
            prev={day.items[i - 1]}
            currency={currency}
            costBasis={costBasis}
          />
        ))}
      </ol>
    </section>
  );
}

function BudgetTable({ budget }: { budget: BudgetSummary }) {
  const money = (v: number | null) => formatMoney(v, budget.currency);
  return (
    <section className="card itin-card">
      <div className="card__head">
        <h2 className="itin-card__title">
          <Wallet size={17} aria-hidden /> {tr("Budget", "예산")}
        </h2>
        {budget.within_budget !== null &&
          (budget.within_budget ? (
            <Badge tone="good">{tr("Within budget", "예산 안")}</Badge>
          ) : (
            <Badge tone="bad">{tr("Over budget", "예산 초과")}</Badge>
          ))}
      </div>
      <div className="table-wrap">
        <table className="table budget-table">
          <caption className="sr-only">
            {tr(
              `Budget versus estimated cost per category (${budget.currency})`,
              `항목별 예산과 예상 비용 (${budget.currency})`,
            )}
          </caption>
          <thead>
            <tr>
              <th scope="col">{tr("Category", "항목")}</th>
              <th scope="col" className="num">
                {tr("Budget", "예산")}
              </th>
              <th scope="col" className="num">
                {tr("Estimated", "예상")}
              </th>
            </tr>
          </thead>
          <tbody>
            {budget.lines.map((line) => {
              const over = !line.unlimited && line.budget !== null && line.estimated !== null && line.budget > 0 && line.estimated > line.budget;
              return (
                <tr key={line.category}>
                  <th scope="row" className="table__rowhead">
                    {budgetCategoryLabel(line.category)}
                  </th>
                  <td className="num">
                    {line.unlimited ? <Badge tone="outline">{tr("Unlimited", "제한 없음")}</Badge> : money(line.budget)}
                  </td>
                  <td className="num">
                    <span className={over ? "warn-text strong" : undefined}>{money(line.estimated)}</span>
                    {over && <span className="sr-only">{tr(" (over budget)", " (예산 초과)")}</span>}
                  </td>
                </tr>
              );
            })}
            <tr className="table__total">
              <th scope="row" className="table__rowhead">
                {tr("Total", "합계")}
              </th>
              <td className="num strong">{money(budget.total_budget)}</td>
              <td className="num strong">{money(budget.total_estimated)}</td>
            </tr>
          </tbody>
        </table>
      </div>
      {budget.notes && <div className="card__body small muted">{budget.notes}</div>}
    </section>
  );
}

function RefineBox({ it }: { it: ItineraryOut }) {
  const navigate = useNavigate();
  const [feedback, setFeedback] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [fieldError, setFieldError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const text = feedback.trim();
    if (!text) return setFieldError(tr("Describe what to change", "바꾸고 싶은 내용을 적어 주세요"));
    if (text.length > 4000) return setFieldError(tr("At most 4000 characters", "최대 4000자까지 입력할 수 있어요"));
    setFieldError(null);
    setBusy(true);
    setError(null);
    try {
      const next = await api.refineItinerary(it.id, text);
      setFeedback("");
      navigate(`/itineraries/${next.id}`);
    } catch (err) {
      setError(err);
      if (err instanceof ApiError && err.fieldErrors.feedback) setFieldError(err.fieldErrors.feedback);
    } finally {
      setBusy(false);
    }
  };
  return (
    <section className="card itin-card itin-refine">
      <div className="card__head">
        <h2 className="itin-card__title">
          <Sparkles size={17} aria-hidden /> {tr("Refine with feedback", "의견 반영해서 다듬기")}
        </h2>
      </div>
      <form className="card__body stack-sm" onSubmit={submit} noValidate>
        <Field
          label={tr("What should change?", "무엇을 바꿀까요?")}
          error={fieldError}
          hint={tr("Creates a new version; this one is kept.", "새 버전을 만들고, 지금 버전은 그대로 남겨 둬요.")}
        >
          {({ id, describedBy, invalid }) => (
            <textarea
              id={id}
              className="textarea"
              value={feedback}
              maxLength={4000}
              placeholder={tr(
                "e.g. Less walking on day 2, add a ramen dinner near the hotel, skip museums",
                "예: 2일차는 덜 걷게, 숙소 근처 라멘 저녁 추가, 박물관은 빼 주세요",
              )}
              aria-describedby={describedBy}
              aria-invalid={invalid || undefined}
              onChange={(e) => setFeedback(e.target.value)}
            />
          )}
        </Field>
        <ErrorBanner error={error} title={tr("Could not start the refinement", "일정 다듬기를 시작하지 못했어요")} />
        <div className="row" style={{ justifyContent: "flex-end" }}>
          <Button type="submit" variant="primary" busy={busy} icon={<Wand2 size={16} aria-hidden />}>
            {tr("Refine itinerary", "일정 다듬기")}
          </Button>
        </div>
      </form>
    </section>
  );
}

function usageLine(usage: ItineraryOut["usage"]): string[] {
  const lines: string[] = [];
  if (typeof usage.input_tokens === "number" || typeof usage.output_tokens === "number") {
    const input = formatNumber(usage.input_tokens ?? 0);
    const output = formatNumber(usage.output_tokens ?? 0);
    lines.push(tr(`${input} in / ${output} out tokens`, `입력 ${input} / 출력 ${output} 토큰`));
  }
  if (typeof usage.web_searches === "number") {
    lines.push(tr(`${usage.web_searches} web searches`, `웹 검색 ${usage.web_searches}회`));
  }
  if (typeof usage.est_usd === "number") lines.push(`≈ $${usage.est_usd.toFixed(2)}`);
  return lines;
}

function backtrackText(hops: number): string {
  if (hops === 0) return tr("None", "없음");
  return tr(`${hops} hop${hops === 1 ? "" : "s"}`, `${hops}회`);
}

/** Itinerary page. */
export function ItineraryView() {
  const id = Number(useParams().id);
  const itin = useAsync(() => api.getItinerary(id), [id]);
  const it = itin.data;
  const trip = useAsync(() => (it ? api.getTrip(it.trip_id) : Promise.resolve(null)), [it?.trip_id]);
  const versions = useAsync(
    () => (it ? api.listItineraries(it.trip_id) : Promise.resolve([] as ItineraryOut[])),
    [it?.trip_id, it?.status],
  );
  const busy = it ? it.status === "pending" || it.status === "running" : false;
  useTitle(it?.plan?.title ?? tr(`Itinerary #${id}`, `여행 일정 #${id}`));

  usePoll(
    busy ? () => api.getItinerary(id) : null,
    (next) => {
      itin.setData(next);
      return next.status === "pending" || next.status === "running";
    },
    3000,
  );

  if (itin.error && !it) {
    return (
      <ErrorBanner
        error={itin.error}
        title={tr("Could not load this itinerary", "여행 일정을 불러오지 못했어요")}
        onRetry={() => void itin.reload()}
      />
    );
  }
  if (!it) {
    return (
      <div className="card">
        <div className="card__body">
          <SkeletonBlock lines={8} label={tr("Loading itinerary", "여행 일정 불러오는 중")} />
        </div>
      </div>
    );
  }

  const plan = it.plan;
  const children = (versions.data ?? []).filter((v) => v.parent_id === it.id);
  const usage = usageLine(it.usage);

  return (
    <>
      <div className="page-head itin-head">
        <div className="page-head__title">
          <Link className="breadcrumb" to={`/trips/${it.trip_id}?tab=itineraries`}>
            <ArrowLeft size={14} aria-hidden /> {trip.data?.name ?? tr("Trip", "여행")} · {tr("Itineraries", "여행 일정")}
          </Link>
          <span className="itin-head__eyebrow">{tr("Your day-by-day plan", "날짜별로 정리한 여행 계획")}</span>
          <div className="row" style={{ gap: 10 }}>
            <h1>{plan?.title ?? tr(`Itinerary #${it.id}`, `여행 일정 #${it.id}`)}</h1>
            <StatusChip status={it.status} label={ITINERARY_STATUS_LABEL[it.status]} />
          </div>
          <span className="itin-head__meta">
            {plan?.destination ? `${plan.destination} · ` : ""}
            {it.arrival_at ? `${formatLocalDateTime(it.arrival_at)}${it.arrival_airport ? ` (${it.arrival_airport})` : ""}` : ""}
            {it.departure_at ? ` → ${formatLocalDateTime(it.departure_at)}${it.departure_airport ? ` (${it.departure_airport})` : ""}` : ""}
          </span>
          {(it.parent_id !== null || children.length > 0) && (
            <span className="row" style={{ gap: 6 }}>
              {it.parent_id !== null && (
                <Link className="badge badge--outline" to={`/itineraries/${it.parent_id}`}>
                  <GitBranch size={11} aria-hidden /> {tr(`Previous version #${it.parent_id}`, `이전 버전 #${it.parent_id}`)}
                </Link>
              )}
              {children.map((c) => (
                <Link key={c.id} className="badge badge--info" to={`/itineraries/${c.id}`}>
                  <GitBranch size={11} aria-hidden /> {tr(`Refined version #${c.id}`, `다듬은 버전 #${c.id}`)}
                </Link>
              ))}
            </span>
          )}
        </div>
      </div>

      {it.feedback && (
        <Banner tone="info" title={tr("Refined with your feedback", "내 의견을 반영해 다듬은 일정")}>
          “{it.feedback}”
        </Banner>
      )}

      {busy && (
        <div className="card itin-progress-card" aria-live="polite">
          <div className="card__body itin-progress">
            <Spinner size={22} label={tr("Generating itinerary", "여행 일정 만드는 중")} />
            <div className="stack-xs">
              <span className="strong">
                {it.status === "pending" ? tr("Queued…", "대기 중…") : tr("Building your itinerary…", "여행 일정을 만드는 중…")}
              </span>
              <span className="small subtle">
                {it.progress ??
                  tr(
                    "Researching places, routes and weather. This can take a few minutes.",
                    "장소, 이동 경로, 날씨를 조사하고 있어요. 몇 분 정도 걸릴 수 있어요.",
                  )}
              </span>
            </div>
          </div>
        </div>
      )}

      {it.status === "error" && (
        <div style={{ marginTop: 12 }}>
          <Banner tone="error" title={tr("Itinerary generation failed", "여행 일정을 만들지 못했어요")}>
            {it.error ?? tr("Unknown error", "알 수 없는 오류")}
          </Banner>
        </div>
      )}

      {plan && (
        <div className="itin-layout">
          <div className="stack itin-main">
            {plan.summary && (
              <div className="itin-summary">
                <Compass size={20} className="itin-summary__icon" aria-hidden />
                <p>{plan.summary}</p>
              </div>
            )}
            {plan.warnings.length > 0 && (
              <Banner tone="warn" title={tr("Things to check", "확인할 점")}>
                <ul className="plain-list">
                  {plan.warnings.map((w) => (
                    <li key={w}>{w}</li>
                  ))}
                </ul>
              </Banner>
            )}
            {plan.days.map((day, i) => (
              <DayCard key={day.date + i} day={day} index={i} currency={plan.currency_local} costBasis={costBasisLabel(plan)} />
            ))}
            {plan.days.length === 0 && <Banner tone="info">{tr("This plan has no days.", "이 일정에는 날짜별 계획이 없어요.")}</Banner>}
          </div>

          <aside className="stack itin-side" aria-label={tr("Itinerary details", "여행 일정 정보")}>
            {plan.budget && <BudgetTable budget={plan.budget} />}
            <section className="card itin-card">
              <div className="card__head">
                <h2 className="itin-card__title">
                  <RouteIcon size={17} aria-hidden /> {tr("Route", "이동 경로")}
                </h2>
              </div>
              <div className="card__body">
                <div className="route-metrics">
                  <div className="route-metric">
                    <span className="route-metric__label">{tr("Total travel", "총 이동 시간")}</span>
                    <span className="route-metric__value">{formatMinutes(plan.route_metrics.total_travel_minutes)}</span>
                  </div>
                  <div className="route-metric">
                    <span className="route-metric__label">{tr("Average hop", "평균 이동 거리")}</span>
                    <span className="route-metric__value">{plan.route_metrics.avg_hop_km.toFixed(1)} km</span>
                  </div>
                  <div className="route-metric">
                    <span className="route-metric__label">{tr("Longest hop", "최장 이동 거리")}</span>
                    <span className="route-metric__value">{plan.route_metrics.max_hop_km.toFixed(1)} km</span>
                  </div>
                  <div className="route-metric">
                    <span className="route-metric__label">{tr("Backtracking", "되돌아가는 이동")}</span>
                    <span className="route-metric__value">{backtrackText(plan.route_metrics.backtrack_hops)}</span>
                  </div>
                </div>
              </div>
            </section>
            <section className="card itin-card">
              <div className="card__head">
                <h2 className="itin-card__title">
                  <Wand2 size={17} aria-hidden /> {tr("Generated by", "생성 정보")}
                </h2>
              </div>
              <div className="card__body">
                <dl className="kv">
                  <dt>{tr("Generator", "생성 방식")}</dt>
                  <dd>{it.generator === "claude" ? "Claude" : tr("Rule-based planner", "규칙 기반 플래너")}</dd>
                  {it.model && (
                    <>
                      <dt>{tr("Model", "모델")}</dt>
                      <dd className="mono">{it.model}</dd>
                    </>
                  )}
                  {usage.length > 0 && (
                    <>
                      <dt>{tr("Usage", "사용량")}</dt>
                      <dd>{usage.join(" · ")}</dd>
                    </>
                  )}
                  <dt>{tr("Created", "생성 시각")}</dt>
                  <dd>{formatUtc(it.created_at)}</dd>
                  {it.hotel_area && (
                    <>
                      <dt>{tr("Hotel area", "숙소 지역")}</dt>
                      <dd>{it.hotel_area}</dd>
                    </>
                  )}
                </dl>
              </div>
            </section>
            {plan.sources.length > 0 && (
              <section className="card itin-card">
                <div className="card__head">
                  <h2 className="itin-card__title">
                    <ExternalLink size={16} aria-hidden /> {tr("Sources", "출처")}
                  </h2>
                </div>
                <ul className="card__body source-list">
                  {plan.sources.map((s) => (
                    <li key={s.url}>
                      <a href={s.url} target="_blank" rel="noopener noreferrer">
                        {s.title || hostOf(s.url)}
                      </a>
                      <span className="xsmall subtle"> · {hostOf(s.url)}</span>
                    </li>
                  ))}
                </ul>
              </section>
            )}
            {it.status === "done" && <RefineBox it={it} />}
            <p className="itin-credits">
              {planAttributions(plan, it.generator).map((a) => `${a}. `)}
              {tr(
                "Opening hours and prices can change — check before you go.",
                "영업시간과 가격은 바뀔 수 있으니 방문 전에 확인하세요.",
              )}
            </p>
          </aside>
        </div>
      )}
      {!plan && it.status === "done" && (
        <Banner tone="warn">
          {tr("This itinerary finished without a plan.", "여행 일정 생성이 끝났지만 계획이 만들어지지 않았어요.")}
        </Banner>
      )}
    </>
  );
}
