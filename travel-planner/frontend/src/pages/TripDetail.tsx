/** Trip detail "/trips/:id" with tabs: Flights, Price history, Alerts, Trip plan, Itineraries. */

import { useCallback, useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import {
  ArrowLeft,
  ArrowRight,
  Bell,
  LineChart,
  Map as MapIcon,
  Pencil,
  Plane,
  RefreshCw,
  SlidersHorizontal,
  Trash2,
} from "lucide-react";
import { api } from "../api/endpoints";
import { errorMessage } from "../api/client";
import type { FlightOfferOut, SearchRunOut, TripOut } from "../api/types";
import { AssistantNote } from "../components/BrandMark";
import { Route } from "../components/Place";
import { Tabs } from "../components/Tabs";
import type { TabDef } from "../components/Tabs";
import { Badge, Banner, Button, ErrorBanner, SkeletonBlock, StatusChip } from "../components/ui";
import { BASIS_LABEL, formatMoney, formatRelative, formatUtc } from "../lib/format";
import { useAsync, useInterval, useNow, useTitle } from "../lib/hooks";
import { tr } from "../lib/i18n";
import { useRunWatcher } from "../lib/runs";
import { tripFacts } from "../lib/trip";
import { tripSummary } from "./trip/tripText";
import { AlertsTab } from "./trip/AlertsTab";
import { FlightsTab } from "./trip/FlightsTab";
import { HistoryTab } from "./trip/HistoryTab";
import { ItinerariesTab } from "./trip/ItinerariesTab";
import { PlanTab } from "./trip/PlanTab";

type TabKey = "flights" | "history" | "alerts" | "plan" | "itineraries";

const TAB_KEYS: readonly TabKey[] = ["flights", "history", "alerts", "plan", "itineraries"];

/** How often to look for a run started elsewhere (scheduler) while none is being watched. */
const RUN_DISCOVERY_MS = 60_000;

/** Tab definitions in the current language (built at render time). */
function tripTabs(): readonly TabDef<TabKey>[] {
  return [
    { key: "flights", label: tr("Flights", "항공권"), icon: <Plane size={15} aria-hidden /> },
    { key: "history", label: tr("Price history", "가격 추이"), icon: <LineChart size={15} aria-hidden /> },
    { key: "alerts", label: tr("Alerts", "알림 규칙"), icon: <Bell size={15} aria-hidden /> },
    { key: "plan", label: tr("Trip plan", "일정 계획"), icon: <SlidersHorizontal size={15} aria-hidden /> },
    { key: "itineraries", label: tr("Itineraries", "여행 일정"), icon: <MapIcon size={15} aria-hidden /> },
  ];
}

function isTab(value: string | null): value is TabKey {
  return TAB_KEYS.some((k) => k === value);
}

function RunProgress({ run }: { run: SearchRunOut }) {
  const parts: string[] = [];
  if (run.combinations_total > 0) {
    parts.push(
      tr(
        `${run.combinations_searched}/${run.combinations_total} searches`,
        `검색 ${run.combinations_searched}/${run.combinations_total}`,
      ),
    );
  }
  if (run.offers_found > 0) parts.push(tr(`${run.offers_found} offers found`, `항공권 ${run.offers_found}개 발견`));
  return (
    <Banner tone="info" title={tr("Checking prices…", "가격 확인 중…")}>
      {parts.length > 0
        ? parts.join(" · ")
        : tr(
            "Searching the selected flight sources. This can take a minute.",
            "선택한 검색 소스에서 항공권을 찾고 있어요. 1분 정도 걸릴 수 있어요.",
          )}
    </Banner>
  );
}

/** "₩4,606 under target" / "₩12,000 to go" line under the target stat. */
function targetGap(t: TripOut, current: number | null): string | null {
  if (current === null || t.target_price === null) return null;
  const gap = t.target_price - current;
  const amount = formatMoney(Math.abs(gap), t.currency);
  if (gap >= 0) return tr(`${amount} under target`, `목표가보다 ${amount} 저렴`);
  return tr(`${amount} to go`, `목표가까지 ${amount}`);
}

/** The page summary in the app's voice, with "Plan booking" for the cheapest flight. */
function TripNote({
  trip,
  best,
  now,
  checking,
}: {
  trip: TripOut;
  best: FlightOfferOut | null;
  now: number;
  checking: boolean;
}) {
  const summary = tripSummary(trip, best, now, checking);
  return (
    <AssistantNote
      className="trip-note"
      tone={summary.tone}
      title={summary.title}
      action={
        best && (
          <Link
            className="btn btn--primary"
            to={`/trips/${trip.id}/book/${best.id}`}
            title={tr(
              "See both flights, the tickets to buy and where to book them",
              "가는 편·오는 편과 예약할 항공권, 예약할 곳을 확인해요",
            )}
          >
            {tr("Plan booking", "예약 준비")} <ArrowRight size={15} aria-hidden />
          </Link>
        )
      }
    >
      {summary.text && <p>{summary.text}</p>}
      {summary.timing && <p className="trip-note__timing">{summary.timing}</p>}
    </AssistantNote>
  );
}

/** One fact of the stats strip: label, a serif value and a muted line under it. */
function Stat({
  label,
  value,
  foot,
  hero = false,
  title,
}: {
  label: string;
  value: string;
  foot?: ReactNode;
  hero?: boolean;
  title?: string;
}) {
  return (
    <div className={`tstat${hero ? " tstat--hero" : ""}`}>
      <dt className="tstat__label">{label}</dt>
      <dd className="tstat__value serif tnum" title={title}>
        {value}
      </dd>
      {foot && <dd className="tstat__foot">{foot}</dd>}
    </div>
  );
}

/** Trip detail page. */
export function TripDetail() {
  const tripId = Number(useParams().id);
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const tab: TabKey = isTab(params.get("tab")) ? (params.get("tab") as TabKey) : "flights";
  const trip = useAsync(() => api.getTrip(tripId), [tripId]);
  const [refreshKey, setRefreshKey] = useState(0);
  // Cheapest matching offer of the latest finished run, for the summary and "Plan booking".
  const best = useAsync(
    () => api.listOffers(tripId, { only_matching: true, sort: "price", limit: 1 }).then((list) => list[0] ?? null),
    [tripId, refreshKey],
  );
  const [actionError, setActionError] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [finishedRun, setFinishedRun] = useState<SearchRunOut | null>(null);
  const now = useNow();
  useTitle(trip.data?.name ?? tr("Trip", "여행"));

  /** Runs already seen finishing, so a stale "running" row never restarts their polling. */
  const finishedIds = useRef(new Set<number>());
  const { run, watch } = useRunWatcher((done) => {
    finishedIds.current.add(done.id);
    setFinishedRun(done);
    setRefreshKey((k) => k + 1);
    void trip.reload();
  });
  const checking = starting || run?.status === "running";
  const watchingRunning = run?.status === "running";

  /** Poll a run that is in progress but not watched yet (e.g. a scheduled check). */
  const adoptRunning = useCallback(
    (candidate: SearchRunOut) => {
      if (candidate.status !== "running" || finishedIds.current.has(candidate.id)) return;
      if (!watchingRunning) watch(candidate);
    },
    [watch, watchingRunning],
  );

  const discoverRun = useCallback(() => {
    api
      .listRuns(tripId, 1)
      .then((runs) => {
        if (runs[0]) adoptRunning(runs[0]);
      })
      .catch(() => undefined);
  }, [tripId, adoptRunning]);

  // On open, then periodically while nothing is watched: scheduled checks start on their own.
  useEffect(() => {
    finishedIds.current.clear();
    discoverRun();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tripId]);
  useInterval(discoverRun, watchingRunning || starting ? null : RUN_DISCOVERY_MS);

  const checkNow = async () => {
    setActionError(null);
    setFinishedRun(null);
    setStarting(true);
    try {
      watch(await api.checkTrip(tripId));
    } catch (err) {
      setActionError(errorMessage(err));
    } finally {
      setStarting(false);
    }
  };

  const remove = async (t: TripOut) => {
    const question = tr(
      `Delete “${t.name}” with its price history, alerts and itineraries?`,
      `“${t.name}” 여행을 삭제할까요? 가격 추이, 알림 규칙, 여행 일정도 함께 삭제돼요.`,
    );
    if (!window.confirm(question)) return;
    setDeleting(true);
    try {
      await api.deleteTrip(t.id);
      navigate("/");
    } catch (err) {
      setActionError(errorMessage(err));
      setDeleting(false);
    }
  };

  if (trip.error && !trip.data) {
    return (
      <div className="trip-detail">
        <Link className="breadcrumb trip-detail__back" to="/">
          <ArrowLeft size={14} aria-hidden /> {tr("Trips", "내 여행")}
        </Link>
        <ErrorBanner
          error={trip.error}
          title={tr("Could not load this trip", "여행 정보를 불러오지 못했어요")}
          onRetry={() => void trip.reload()}
        />
      </div>
    );
  }
  if (!trip.data) {
    return (
      <div className="card">
        <div className="card__body">
          <SkeletonBlock lines={5} label={tr("Loading trip", "여행 불러오는 중")} />
        </div>
      </div>
    );
  }

  const t = trip.data;
  const status = checking ? "running" : (t.last_status ?? "none");
  const current = t.latest_min_price_basis_value;
  const belowTarget = current !== null && t.target_price !== null && current <= t.target_price;
  const sample = t.providers.length > 0 && t.providers.every((p) => p === "mock");
  const gap = targetGap(t, current);
  const bestOffer = best.data && best.data.trip_id === t.id ? best.data : null;

  return (
    <div className="trip-detail">
      <Link className="breadcrumb trip-detail__back" to="/">
        <ArrowLeft size={14} aria-hidden /> {tr("Trips", "내 여행")}
      </Link>

      <header className="detail-head">
        <div className="detail-head__main">
          <h1 className="detail-head__title">{t.name}</h1>
          <div className="detail-head__route">
            <Route origin={t.origin} destination={t.destination} tripType={t.trip_type} />
            <span className="detail-head__badges">
              <StatusChip status={status} title={t.last_error ?? undefined} />
              {sample ? (
                <Badge tone="warn">{tr("Sample data", "예시 데이터")}</Badge>
              ) : (
                <Badge tone="outline">{tr("Live fares", "실시간 요금")}</Badge>
              )}
              {!t.active && <Badge tone="outline">{tr("Paused", "일시 중지")}</Badge>}
            </span>
          </div>
          <p className="detail-head__meta">{tripFacts(t)}</p>
        </div>
        <div className="detail-head__actions">
          <Link
            className="btn btn--ghost btn--icon"
            to={`/trips/${t.id}/edit`}
            aria-label={tr("Edit trip", "여행 수정")}
            title={tr("Edit trip", "여행 수정")}
          >
            <Pencil size={17} aria-hidden />
          </Link>
          <Button icon={<RefreshCw size={16} aria-hidden />} busy={checking} onClick={checkNow}>
            {checking ? tr("Checking", "확인 중") : tr("Check now", "지금 확인")}
          </Button>
          <Button
            variant="ghost"
            className="detail-head__delete"
            iconOnly
            busy={deleting}
            icon={<Trash2 size={16} aria-hidden />}
            aria-label={tr("Delete trip", "여행 삭제")}
            title={tr("Delete trip", "여행 삭제")}
            onClick={() => void remove(t)}
          />
        </div>
      </header>

      <TripNote trip={t} best={bestOffer} now={now} checking={checking} />

      <div className="stack-sm detail-banners" aria-live="polite">
        {run?.status === "running" && <RunProgress run={run} />}
        {finishedRun && finishedRun.status !== "running" && !checking && (
          <Banner
            tone={finishedRun.status === "ok" ? "good" : finishedRun.status === "partial" ? "warn" : "error"}
            title={
              finishedRun.status === "ok"
                ? tr("Check finished", "가격 확인 완료")
                : finishedRun.status === "partial"
                  ? tr("Check finished with some errors", "가격 확인 완료 (일부 오류)")
                  : tr("Check failed", "가격 확인 실패")
            }
          >
            {finishedRun.error ??
              (() => {
                const lowest = formatMoney(
                  t.price_basis === "total" ? finishedRun.min_price_total : finishedRun.min_price_per_person,
                  finishedRun.currency,
                );
                return tr(
                  `${finishedRun.offers_matching} matching of ${finishedRun.offers_found} offers · lowest ${lowest}`,
                  `항공권 ${finishedRun.offers_found}개 중 ${finishedRun.offers_matching}개가 조건에 맞아요 · 최저가 ${lowest}`,
                );
              })()}
          </Banner>
        )}
        {actionError && (
          <Banner tone="error" title={tr("Action failed", "작업에 실패했어요")}>
            {actionError}
          </Banner>
        )}
        {!checking && !finishedRun && t.last_status === "error" && t.last_error && (
          <Banner tone="error" title={tr("The last check failed", "마지막 가격 확인에 실패했어요")}>
            {t.last_error}
          </Banner>
        )}
        {!checking && !finishedRun && t.last_status === "partial" && t.last_error && (
          <Banner tone="warn" title={tr("Some sources failed in the last check", "마지막 확인에서 일부 검색 소스가 실패했어요")}>
            {t.last_error}
          </Banner>
        )}
      </div>

      <dl className="trip-stats" aria-label={tr("Price facts", "가격 요약")}>
        <Stat
          hero
          label={`${tr("Lowest now", "현재 최저가")} · ${BASIS_LABEL[t.price_basis]}`}
          value={formatMoney(current, t.currency)}
          foot={
            current === null ? (
              tr("No check yet", "아직 확인 전")
            ) : (
              <>
                {t.adults > 1 && t.latest_min_price !== null && (
                  <span>
                    {t.price_basis === "per_person"
                      ? tr(
                          `${formatMoney(t.latest_min_price, t.currency)} total for ${t.adults} adults`,
                          `성인 ${t.adults}명 총액 ${formatMoney(t.latest_min_price, t.currency)}`,
                        )
                      : tr(
                          `${formatMoney(t.latest_min_price / t.adults, t.currency)} per person`,
                          `1인 ${formatMoney(t.latest_min_price / t.adults, t.currency)}`,
                        )}
                  </span>
                )}
                {belowTarget && <Badge tone="good">{tr("Below target", "목표가 이하")}</Badge>}
              </>
            )
          }
        />
        <Stat
          label={tr("Target", "목표 가격")}
          value={formatMoney(t.target_price, t.currency)}
          foot={<span className={belowTarget ? "good-text" : undefined}>{gap ?? tr("No target set", "목표 가격 없음")}</span>}
        />
        <Stat
          label={tr("Lowest ever", "역대 최저가")}
          value={formatMoney(t.lowest_ever, t.currency)}
          foot={tr("Across all checks", "모든 확인 기준")}
        />
        <Stat
          label={tr("Next check", "다음 확인")}
          value={!t.active ? tr("Paused", "일시 중지") : t.next_run_at ? formatUtc(t.next_run_at) : "—"}
          foot={t.active && t.next_run_at ? formatRelative(t.next_run_at, now) : tr("Not scheduled", "예약 없음")}
        />
        <Stat
          label={tr("Last check", "마지막 확인")}
          value={t.last_checked_at ? formatRelative(t.last_checked_at, now) : tr("Never", "없음")}
          title={t.last_checked_at ? formatUtc(t.last_checked_at) : undefined}
          foot={t.last_checked_at ? formatUtc(t.last_checked_at) : tr("Not checked yet", "아직 확인 전")}
        />
      </dl>

      <div className="detail-tabs">
        <Tabs
          tabs={tripTabs()}
          value={tab}
          idPrefix="trip"
          label={tr("Trip sections", "여행 메뉴")}
          onChange={(key) => setParams(key === "flights" ? {} : { tab: key }, { replace: true })}
        />
      </div>
      <div role="tabpanel" id={`trip-panel-${tab}`} aria-labelledby={`trip-tab-${tab}`} className="detail-panel">
        {tab === "flights" && <FlightsTab trip={t} refreshKey={refreshKey} onCheck={checkNow} checking={checking} />}
        {tab === "history" && (
          <HistoryTab trip={t} refreshKey={refreshKey} activeRun={run} onRunningRun={adoptRunning} />
        )}
        {tab === "alerts" && <AlertsTab trip={t} />}
        {tab === "plan" && <PlanTab trip={t} />}
        {tab === "itineraries" && <ItinerariesTab trip={t} refreshKey={refreshKey} />}
      </div>
    </div>
  );
}
