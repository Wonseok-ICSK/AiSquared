/** Price history tab: a one-line summary (latest price vs target), the price line on a fixed
 * relative time window (24 hours / 7 days / 30 days) and the runs table (the chart's table view). */

import { useEffect, useMemo, useState } from "react";
import { ArrowDownRight, ArrowUpRight, CheckCircle2, LineChart, Minus, MinusCircle, XCircle } from "lucide-react";
import { api } from "../../api/endpoints";
import type { ProviderRunStat, SearchRunOut, TripOut } from "../../api/types";
import { BrandMark } from "../../components/BrandMark";
import { PriceChart } from "../../components/charts/PriceChart";
import type { ChartPoint } from "../../components/charts/PriceChart";
import { windowLabel, windowPhrase } from "../../components/charts/chartText";
import { inWindow, longerWindow, TIME_WINDOWS, WINDOW_MS } from "../../components/charts/scale";
import type { TimeWindow } from "../../components/charts/scale";
import { Button, EmptyState, ErrorBanner, Segmented, SkeletonBlock, Spinner, StatusChip } from "../../components/ui";
import { BASIS_LABEL, elapsedSeconds, formatMoney, formatSeconds, formatUtc, parseUtc } from "../../lib/format";
import { useAsync, useNow } from "../../lib/hooks";
import { tr } from "../../lib/i18n";
import { useProviderNames } from "../../lib/providers";
import { historyValues } from "../../lib/history";
import { historySummary } from "./tripText";
import type { HistorySummary } from "./tripText";

function ProviderStat({ name, stat }: { name: string; stat: ProviderRunStat }) {
  const Icon = stat.status === "ok" ? CheckCircle2 : stat.status === "error" ? XCircle : MinusCircle;
  const tone = stat.status === "ok" ? "good" : stat.status === "error" ? "bad" : "neutral";
  const detail = [
    tr(`${stat.offers} offer${stat.offers === 1 ? "" : "s"}`, `항공권 ${stat.offers}개`),
    tr(`${stat.queries} quer${stat.queries === 1 ? "y" : "ies"}`, `조회 ${stat.queries}회`),
    tr(`${stat.duration_s.toFixed(1)} s`, `${stat.duration_s.toFixed(1)}초`),
  ].join(" · ");
  return (
    <div className={`pstat pstat--${tone}`} title={stat.error ?? undefined}>
      <Icon size={13} aria-hidden />
      <span className="strong">{name}</span>
      <span className="subtle">{stat.status === "skipped" ? tr("skipped", "건너뜀") : detail}</span>
      {stat.error && <span className="pstat__err">{stat.error}</span>}
    </div>
  );
}

function RunRow({ run, trip, names }: { run: SearchRunOut; trip: TripOut; names: (k: string) => string }) {
  const running = run.status === "running";
  const min = trip.price_basis === "total" ? run.min_price_total : run.min_price_per_person;
  const stats = Object.entries(run.provider_stats);
  return (
    <tr>
      <td className="cell-main" data-label="">
        <div className="strong nowrap">{formatUtc(run.started_at)}</div>
        <div className="xsmall subtle">
          {run.trigger === "manual" ? tr("Manual check", "수동 확인") : tr("Scheduled", "자동 확인")}
        </div>
      </td>
      <td data-label={tr("Status", "상태")}>
        {running ? (
          <span className="live-status">
            <Spinner size={14} label={tr("Run in progress", "검색 진행 중")} /> {tr("Running", "검색 중")}
          </span>
        ) : (
          <StatusChip status={run.status} title={run.error ?? undefined} />
        )}
      </td>
      <td className="num" data-label={tr("Duration", "소요 시간")}>
        {formatSeconds(elapsedSeconds(run.started_at, run.finished_at))}
      </td>
      <td className="num" data-label={tr("Searches", "검색 조합")}>
        {run.combinations_searched}/{run.combinations_total}
      </td>
      <td className="num" data-label={tr("Offers", "조건 일치 / 전체")}>
        {run.offers_matching}/{run.offers_found}
      </td>
      <td className="num" data-label={tr(`Lowest (${BASIS_LABEL[trip.price_basis]})`, `최저가 (${BASIS_LABEL[trip.price_basis]})`)}>
        <span className="strong">{formatMoney(min, run.currency)}</span>
      </td>
      <td data-label={tr("Sources", "검색 소스")} className="cell-wide">
        <div className="stack-xs">
          {stats.length === 0 && <span className="xsmall subtle">—</span>}
          {stats.map(([key, stat]) => (
            <ProviderStat key={key} name={names(key)} stat={stat} />
          ))}
          {run.error && <span className="xsmall run-error">{run.error}</span>}
          {run.log.length > 0 && (
            <details className="disclosure">
              <summary>{tr(`Log (${run.log.length})`, `로그 (${run.log.length})`)}</summary>
              <pre className="log">{run.log.join("\n")}</pre>
            </details>
          )}
        </div>
      </td>
    </tr>
  );
}

/** The app's one line above the chart: latest price vs target, and the move since the first check. */
function PriceSummaryLine({ summary }: { summary: HistorySummary }) {
  const { change } = summary;
  const ChangeIcon = change?.dir === "down" ? ArrowDownRight : change?.dir === "up" ? ArrowUpRight : Minus;
  return (
    <p className={`price-summary${summary.tone === "good" ? " is-good" : ""}`}>
      <BrandMark size={18} className="price-summary__mark" />
      <span className="price-summary__text">{summary.text}</span>
      {change && (
        <span className={`price-summary__change is-${change.dir}`}>
          <ChangeIcon size={13} aria-hidden />
          {change.text}
        </span>
      )}
    </p>
  );
}

/** Price history chart + runs table. */
export function HistoryTab({
  trip,
  refreshKey,
  activeRun,
  onRunningRun,
}: {
  trip: TripOut;
  refreshKey: number;
  /** The run the parent polls (GET /api/runs/{id}), merged into the table. */
  activeRun: SearchRunOut | null;
  /** Called with the newest run still "running" in the loaded list so the parent polls it. */
  onRunningRun?: (run: SearchRunOut) => void;
}) {
  const history = useAsync(() => api.priceHistory(trip.id), [trip.id, refreshKey]);
  const runs = useAsync(() => api.listRuns(trip.id, 50), [trip.id, refreshKey]);
  const names = useProviderNames();
  const now = useNow(60_000);
  const [range, setRange] = useState<TimeWindow>("7d");

  const chart = useMemo(() => {
    const h = history.data;
    if (!h || h.points.length === 0) return null;
    const values = historyValues(h, h.price_basis);
    const points: ChartPoint[] = h.points.map((p, i) => ({ at: p.checked_at, value: values[i] }));
    return { points, values, currency: h.currency, basis: h.price_basis };
  }, [history.data]);

  const summary = chart ? historySummary(trip, chart.values, chart.basis, chart.currency) : null;
  const windowMs = WINDOW_MS[range];
  const rangeValues = (chart?.points ?? [])
    .filter((p) => p.value !== null && inWindow(parseUtc(p.at).getTime(), now, windowMs))
    .map((p) => p.value as number);
  const longer = longerWindow(range);
  const lastPriced = chart ? ([...chart.points].reverse().find((p) => p.value !== null) ?? null) : null;
  const span = windowPhrase(range);
  const chartSummary =
    chart && rangeValues.length > 0
      ? tr(
          `Lowest ${BASIS_LABEL[chart.basis]} price per check over ${span} (${rangeValues.length} check${
            rangeValues.length === 1 ? "" : "s"
          }): latest ${formatMoney(rangeValues[rangeValues.length - 1], chart.currency)}, lowest ${formatMoney(
            Math.min(...rangeValues),
            chart.currency,
          )}, highest ${formatMoney(Math.max(...rangeValues), chart.currency)}.`,
          `${span} 동안 확인 ${rangeValues.length}회의 최저가(${BASIS_LABEL[chart.basis]}): 최근 ${formatMoney(
            rangeValues[rangeValues.length - 1],
            chart.currency,
          )}, 최저 ${formatMoney(Math.min(...rangeValues), chart.currency)}, 최고 ${formatMoney(
            Math.max(...rangeValues),
            chart.currency,
          )}.`,
        )
      : tr(`No prices checked in ${span}.`, `${span} 동안 확인한 가격이 없어요.`);

  // Merge the live run (polled by the parent) into the list so its spinner/progress is current.
  const runList = (runs.data ?? []).map((r) => (activeRun && r.id === activeRun.id ? activeRun : r));
  if (activeRun && !runList.some((r) => r.id === activeRun.id)) runList.unshift(activeRun);

  // A run shown as running that nobody polls (e.g. a scheduled check): hand it to the parent,
  // which polls it and bumps refreshKey when it finishes (reloading this chart and table).
  const unwatched = runList.find((r) => r.status === "running" && r.id !== activeRun?.id) ?? null;
  useEffect(() => {
    if (unwatched) onRunningRun?.(unwatched);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [unwatched?.id, activeRun?.id, activeRun?.status]);

  const basisText = BASIS_LABEL[trip.price_basis];

  return (
    <div className="stack">
      <ErrorBanner
        error={history.error}
        title={tr("Could not load price history", "가격 추이를 불러오지 못했어요")}
        onRetry={() => void history.reload()}
      />
      <section className="card price-overview">
        <div className="chart-head">
          <div className="chart-head__text">
            <h2>{tr("Watch the price while you wait", "기다리는 동안 가격을 살펴보세요")}</h2>
            <p className="small subtle">
              {tr(
                `Lowest matching price per check · ${trip.currency} · ${basisText}`,
                `확인할 때마다 조건에 맞는 최저 요금 · ${trip.currency} · ${basisText} 기준`,
              )}
            </p>
          </div>
          {chart && (
            <div className="chart-head__range">
              <Segmented
                label={tr("Time window", "기간")}
                value={range}
                options={TIME_WINDOWS.map((w) => ({ value: w, label: windowLabel(w) }))}
                onChange={setRange}
              />
            </div>
          )}
        </div>
        {history.loading && !history.data && <SkeletonBlock lines={5} label={tr("Loading chart", "차트 불러오는 중")} />}
        {history.data && !chart && (
          <EmptyState compact icon={<LineChart size={24} />} title={tr("No price history yet", "아직 가격 기록이 없어요")}>
            {tr("Each finished check adds a point here.", "확인이 끝날 때마다 여기에 가격이 하나씩 쌓여요.")}
          </EmptyState>
        )}
        {chart && (
          <>
            {summary && <PriceSummaryLine summary={summary} />}
            <div className={history.loading ? "is-refreshing" : undefined}>
              <PriceChart
                points={chart.points}
                range={range}
                now={now}
                currency={chart.currency}
                target={trip.target_price}
                summary={chartSummary}
                empty={
                  <div className="chart-empty">
                    <p className="chart-empty__title">
                      {tr(`No prices checked in ${span}.`, `${span} 동안 확인한 가격이 없어요.`)}
                    </p>
                    {lastPriced && (
                      <p className="chart-empty__note">
                        {tr(`The latest one is from ${formatUtc(lastPriced.at)}.`, `가장 최근 기록은 ${formatUtc(lastPriced.at)}이에요.`)}
                      </p>
                    )}
                    {longer && (
                      <Button size="sm" onClick={() => setRange(longer)}>
                        {tr(`Show ${windowLabel(longer)}`, `${windowLabel(longer)} 보기`)}
                      </Button>
                    )}
                  </div>
                }
              />
            </div>
          </>
        )}
      </section>

      <section className="card runs-card">
        <div className="card__head">
          <h2>{tr("Runs", "검색 기록")}</h2>
          <span className="small subtle">{tr(`${runList.length} most recent`, `최근 ${runList.length}건`)}</span>
        </div>
        <ErrorBanner error={runs.error} title={tr("Could not load runs", "검색 기록을 불러오지 못했어요")} onRetry={() => void runs.reload()} />
        {runs.loading && !runs.data && (
          <div className="card__body">
            <SkeletonBlock lines={4} label={tr("Loading runs", "검색 기록 불러오는 중")} />
          </div>
        )}
        {runs.data && runList.length === 0 && (
          <div className="card__body small subtle">
            {tr("No checks have run for this trip yet.", "이 여행은 아직 확인한 적이 없어요.")}
          </div>
        )}
        {runList.length > 0 && (
          <div className="table-wrap">
            <table className="table table--cards">
              <caption className="sr-only">{tr("Search runs", "검색 기록")}</caption>
              <thead>
                <tr>
                  <th scope="col">{tr("Started", "시작 시각")}</th>
                  <th scope="col">{tr("Status", "상태")}</th>
                  <th scope="col" className="num">
                    {tr("Duration", "소요 시간")}
                  </th>
                  <th scope="col" className="num">
                    {tr("Searches", "검색 조합")}
                  </th>
                  <th scope="col" className="num">
                    {tr("Matching / found", "조건 일치 / 전체")}
                  </th>
                  <th scope="col" className="num">
                    {tr("Lowest", "최저가")}
                  </th>
                  <th scope="col">{tr("Sources & log", "검색 소스 · 로그")}</th>
                </tr>
              </thead>
              <tbody>
                {runList.map((run) => (
                  <RunRow key={run.id} run={run} trip={trip} names={names} />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}
