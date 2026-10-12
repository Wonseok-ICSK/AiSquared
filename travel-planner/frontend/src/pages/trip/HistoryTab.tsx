/** Price history tab: line chart of run minima (+ per provider, target line), an insight panel
 * comparing the latest price with the target, and the runs table. */

import { useEffect, useMemo } from "react";
import { Link } from "react-router-dom";
import {
  ArrowDownRight,
  ArrowRight,
  ArrowUpRight,
  CheckCircle2,
  LineChart,
  Minus,
  MinusCircle,
  ShieldCheck,
  TrendingDown,
  XCircle,
} from "lucide-react";
import { api } from "../../api/endpoints";
import type { ProviderRunStat, SearchRunOut, TripOut } from "../../api/types";
import { PriceChart } from "../../components/charts/PriceChart";
import type { ChartSeries } from "../../components/charts/PriceChart";
import { EmptyState, ErrorBanner, SkeletonBlock, Spinner, StatusChip } from "../../components/ui";
import {
  BASIS_LABEL,
  elapsedSeconds,
  formatMoney,
  formatSeconds,
  formatUtc,
  inBasis,
} from "../../lib/format";
import { useAsync } from "../../lib/hooks";
import { tr } from "../../lib/i18n";
import { providerColor, useProviderNames, useProviderOrder } from "../../lib/providers";
import { historyValues } from "../../lib/history";

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
          {run.error && <span className="xsmall" style={{ color: "var(--bad)" }}>{run.error}</span>}
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

interface ChartData {
  times: string[];
  series: ChartSeries[];
  currency: string;
  basis: TripOut["price_basis"];
  first: number | null;
  latest: number | null;
  low: number | null;
  high: number | null;
  checks: number;
}

/** Right-hand panel: how the latest lowest price compares with the target and the first check. */
function PriceInsight({ trip, chart }: { trip: TripOut; chart: ChartData }) {
  const { latest, first, currency } = chart;
  const target = trip.target_price;
  const reached = latest !== null && target !== null && latest <= target;
  const gap = latest !== null && target !== null ? formatMoney(Math.abs(target - latest), currency) : null;

  let title: string;
  let body: string;
  if (target === null) {
    title = tr("Set a target price to get alerts.", "목표 가격을 정하면 알려드려요.");
    body = tr(
      "Add a target in the trip settings or an alert rule, and we'll tell you when fares reach it.",
      "여행 수정이나 알림 규칙에서 목표 가격을 정해 보세요. 그 가격에 닿으면 알려드릴게요.",
    );
  } else if (reached) {
    title = tr("Found the price you were waiting for.", "기다리던 가격을 찾았어요.");
    body = tr(
      `${gap} cheaper than your target. That leaves more room in your trip budget.`,
      `목표가보다 ${gap} 저렴한 항공권을 찾았어요. 여행 예산에 여유가 생겼네요.`,
    );
  } else {
    title = tr("Still watching for your price.", "계속 확인할게요.");
    body = tr(
      `${gap} above your target of ${formatMoney(target, currency)}. We'll let you know when it drops.`,
      `목표가 ${formatMoney(target, currency)}까지 ${gap} 남았어요. 가격이 내려가면 알려드릴게요.`,
    );
  }

  let change: { dir: "down" | "up" | "flat"; text: string } | null = null;
  if (chart.checks >= 2 && first !== null && latest !== null && first !== 0) {
    const pct = ((latest - first) / first) * 100;
    const abs = Math.abs(pct).toFixed(1);
    if (Math.abs(pct) < 0.05) change = { dir: "flat", text: tr("Same as the first check", "첫 확인과 같아요") };
    else if (pct < 0) change = { dir: "down", text: tr(`${abs}% lower than the first check`, `첫 확인 대비 ${abs}% 하락`) };
    else change = { dir: "up", text: tr(`${abs}% higher than the first check`, `첫 확인 대비 ${abs}% 상승`) };
  }
  const ChangeIcon = change?.dir === "down" ? ArrowDownRight : change?.dir === "up" ? ArrowUpRight : Minus;

  return (
    <aside className={`price-insight${reached ? " is-reached" : ""}`} aria-label={tr("Price insight", "가격 요약")}>
      <TrendingDown size={20} className="price-insight__icon" aria-hidden />
      <span className="price-insight__eyebrow">{tr("My price alert", "나의 가격 알림")}</span>
      <h3>{title}</h3>
      <p>{body}</p>
      <div className="price-insight__price">
        <span>{tr("Lowest right now", "현재 최저 요금")}</span>
        <b className="tnum">{formatMoney(latest, currency)}</b>
      </div>
      {change && (
        <div className={`price-insight__change is-${change.dir}`}>
          <ChangeIcon size={13} aria-hidden /> {change.text}
        </div>
      )}
      <Link className="btn btn--primary price-insight__cta" to={`/trips/${trip.id}`}>
        {tr("See flights", "항공편 살펴보기")} <ArrowRight size={15} aria-hidden />
      </Link>
    </aside>
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
  const order = useProviderOrder();

  const chart = useMemo((): ChartData | null => {
    const h = history.data;
    if (!h || h.points.length === 0) return null;
    const basis = h.price_basis;
    const times = h.points.map((p) => p.checked_at);
    const providerKeys = [...new Set(h.points.flatMap((p) => Object.keys(p.by_provider)))].sort(
      (a, b) => (order.indexOf(a) + 1 || 99) - (order.indexOf(b) + 1 || 99),
    );
    const series: ChartSeries[] = [
      {
        key: "__lowest",
        label: tr("Lowest overall", "전체 최저가"),
        color: "var(--series-1)",
        values: historyValues(h, basis),
        emphasis: true,
      },
      ...providerKeys.map((key) => ({
        key,
        label: names(key),
        color: providerColor(key, order),
        values: h.points.map((p) => {
          const v = p.by_provider[key];
          return v === undefined ? null : inBasis(v, trip.adults, basis);
        }),
      })),
    ];
    const present = series[0].values.filter((v): v is number => v !== null);
    return {
      times,
      series,
      currency: h.currency,
      basis,
      first: present.length ? present[0] : null,
      latest: present.length ? present[present.length - 1] : null,
      low: present.length ? Math.min(...present) : null,
      high: present.length ? Math.max(...present) : null,
      checks: h.points.length,
    };
  }, [history.data, names, order, trip.adults]);

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
      <section className={`card price-overview${chart ? "" : " is-empty"}`}>
        <div className="price-overview__chart">
          <div className="chart-head">
            <div>
              <h2>{tr("Watch the price while you wait", "기다리는 동안 가격을 살펴보세요")}</h2>
              <p className="small subtle">
                {tr(
                  `Lowest matching price per check · ${trip.currency} · ${basisText}`,
                  `확인할 때마다 조건에 맞는 최저 요금 · ${trip.currency} · ${basisText} 기준`,
                )}
              </p>
            </div>
          </div>
          {history.loading && !history.data && <SkeletonBlock lines={5} label={tr("Loading chart", "차트 불러오는 중")} />}
          {history.data && !chart && (
            <EmptyState compact icon={<LineChart size={24} />} title={tr("No price history yet", "아직 가격 기록이 없어요")}>
              {tr("Each finished check adds a point here.", "확인이 끝날 때마다 여기에 가격이 하나씩 쌓여요.")}
            </EmptyState>
          )}
          {chart && (
            <>
              <dl className="mini-stats">
                <div>
                  <dt>{tr("Latest", "최근")}</dt>
                  <dd>{formatMoney(chart.latest, chart.currency)}</dd>
                </div>
                <div>
                  <dt>{tr("Lowest", "최저")}</dt>
                  <dd>{formatMoney(chart.low, chart.currency)}</dd>
                </div>
                <div>
                  <dt>{tr("Highest", "최고")}</dt>
                  <dd>{formatMoney(chart.high, chart.currency)}</dd>
                </div>
                <div>
                  <dt>{tr("Checks", "확인 횟수")}</dt>
                  <dd>{chart.checks}</dd>
                </div>
              </dl>
              <div className={history.loading ? "is-refreshing" : undefined}>
                <PriceChart
                  times={chart.times}
                  series={chart.series}
                  currency={chart.currency}
                  target={trip.target_price}
                  summary={tr(
                    `Lowest ${BASIS_LABEL[chart.basis]} price over ${chart.checks} checks: latest ${formatMoney(
                      chart.latest,
                      chart.currency,
                    )}, lowest ${formatMoney(chart.low, chart.currency)}, highest ${formatMoney(chart.high, chart.currency)}.`,
                    `확인 ${chart.checks}회의 최저가(${BASIS_LABEL[chart.basis]}): 최근 ${formatMoney(
                      chart.latest,
                      chart.currency,
                    )}, 최저 ${formatMoney(chart.low, chart.currency)}, 최고 ${formatMoney(chart.high, chart.currency)}.`,
                  )}
                />
              </div>
              <div className="chart-foot">
                <ShieldCheck size={14} aria-hidden />
                <span>
                  {tr(
                    "Per-source lines show each source's cheapest matching offer. Hover or use the arrow keys on the chart for values; every run is also listed below.",
                    "검색 소스별 선은 소스마다 조건에 맞는 가장 저렴한 항공권을 보여줘요. 차트에 마우스를 올리거나 방향키를 누르면 값을 볼 수 있고, 모든 검색은 아래 목록에도 있어요.",
                  )}
                </span>
                {trip.active && trip.next_run_at && (
                  <span className="chart-foot__next">
                    {tr(`Next check: ${formatUtc(trip.next_run_at)}`, `다음 확인: ${formatUtc(trip.next_run_at)}`)}
                  </span>
                )}
              </div>
            </>
          )}
        </div>
        {chart && <PriceInsight trip={trip} chart={chart} />}
      </section>

      <section className="card">
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
