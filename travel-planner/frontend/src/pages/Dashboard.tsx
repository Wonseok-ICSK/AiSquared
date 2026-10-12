/** Dashboard "/": headline stats and the monitored trips as photo cards with latest price,
 * trend, status and actions. */

import { useState } from "react";
import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import {
  ArrowLeftRight,
  ArrowRight,
  ArrowUpRight,
  CalendarClock,
  CalendarDays,
  ChevronRight,
  Pencil,
  PiggyBank,
  PlaneTakeoff,
  Plus,
  Radar,
  RefreshCw,
  Sparkles,
  TrendingDown,
  Users,
} from "lucide-react";
import { api } from "../api/endpoints";
import { errorMessage } from "../api/client";
import type { PriceBasis, TripOut } from "../api/types";
import { PriceDelta } from "../components/PriceDelta";
import { Sparkline } from "../components/charts/Sparkline";
import { Badge, Button, EmptyState, ErrorBanner, Skeleton, StatusChip, Switch } from "../components/ui";
import { cityName, useAirport } from "../lib/airports";
import {
  CABIN_LABEL,
  formatAdults,
  formatDateRange,
  formatMoney,
  formatRelative,
  formatStops,
  formatUtc,
} from "../lib/format";
import { useAsync, useNow, useTitle } from "../lib/hooks";
import { historyValues, priceTrend } from "../lib/history";
import { tr } from "../lib/i18n";
import { useRunWatcher } from "../lib/runs";

// ----------------------------------------------------------------------------- helpers

const TOKYO_CODES = new Set(["TYO", "NRT", "HND"]);
const OSAKA_CODES = new Set(["OSA", "KIX", "ITM", "UKB"]);

/** Photo for well-known destinations (Unsplash, see README credits), else a gradient variant. */
function heroVariant(code: string, cityCode: string | null | undefined): string {
  const keys = [code.toUpperCase(), (cityCode ?? "").toUpperCase()];
  if (keys.some((k) => TOKYO_CODES.has(k))) return "tokyo";
  if (keys.some((k) => OSAKA_CODES.has(k))) return "osaka";
  const sum = [...code].reduce((acc, ch) => acc + ch.charCodeAt(0), 0);
  return `g${sum % 3}`;
}

/** Light tagline after the city name ("Tokyo, closer than ever."). */
function tagline(variant: string): string {
  if (variant === "tokyo") return tr("closer than ever.", "더 가까이.");
  if (variant === "osaka") return tr("even tastier.", "더 맛있게.");
  return tr("here we come.", "곧 만나요.");
}

function basisNote(basis: PriceBasis, adults: number): string {
  return basis === "per_person"
    ? tr("per person", "1인 기준")
    : tr(`total · ${formatAdults(adults)}`, `총액 · ${formatAdults(adults)}`);
}

function isBelowTarget(t: TripOut): boolean {
  return t.target_price !== null && t.latest_min_price_basis_value !== null && t.latest_min_price_basis_value <= t.target_price;
}

// ----------------------------------------------------------------------------- stats

function StatCard({
  label,
  icon,
  value,
  tone,
  foot,
}: {
  label: string;
  icon: ReactNode;
  value: string;
  tone?: "good";
  foot: ReactNode;
}) {
  return (
    <div className="dash-stat">
      <div className="dash-stat__label">
        <span>{label}</span>
        {icon}
      </div>
      <strong className={`dash-stat__value${tone ? ` dash-stat__value--${tone}` : ""}`}>{value}</strong>
      <div className="dash-stat__foot">{foot}</div>
    </div>
  );
}

/** Sum of (target − current lowest) over trips at or below target, per currency. */
function savings(list: TripOut[]): { amount: number; currency: string; otherCurrencies: number; bases: Set<PriceBasis> } {
  const byCurrency = new Map<string, number>();
  const bases = new Set<PriceBasis>();
  for (const t of list) {
    if (!isBelowTarget(t) || t.target_price === null || t.latest_min_price_basis_value === null) continue;
    byCurrency.set(t.currency, (byCurrency.get(t.currency) ?? 0) + (t.target_price - t.latest_min_price_basis_value));
    bases.add(t.price_basis);
  }
  let currency = list[0]?.currency ?? "KRW";
  let amount = 0;
  for (const [cur, value] of byCurrency) {
    if (value > amount) {
      amount = value;
      currency = cur;
    }
  }
  return { amount, currency, otherCurrencies: Math.max(0, byCurrency.size - 1), bases };
}

function DashboardStats({ list }: { list: TripOut[] }) {
  const monitored = list.filter((t) => t.active).length;
  const below = list.filter(isBelowTarget).length;
  const saved = savings(list);
  const basisText =
    saved.bases.size === 1
      ? saved.bases.has("per_person")
        ? tr("per person, summed over trips", "1인 기준 금액을 여행별로 합산")
        : tr("all travellers, summed over trips", "전체 인원 기준 금액을 여행별로 합산")
      : tr("in each trip's own price basis", "여행별 가격 기준(1인/총액)으로 합산");
  return (
    <section className="dash-stats" aria-label={tr("Summary", "요약")}>
      <StatCard
        label={tr("Trips being monitored", "모니터링 중인 여행")}
        icon={<Radar size={17} aria-hidden />}
        value={String(monitored).padStart(2, "0")}
        foot={
          <>
            <i aria-hidden />
            {tr(`Automatic checks · ${list.length} in total`, `자동 가격 확인 중 · 전체 ${list.length}개`)}
          </>
        }
      />
      <StatCard
        label={tr("At or below target", "목표가 이하 항공권")}
        icon={<TrendingDown size={17} aria-hidden />}
        value={String(below).padStart(2, "0")}
        tone={below > 0 ? "good" : undefined}
        foot={
          below > 0 ? (
            <>
              <TrendingDown size={13} aria-hidden /> {tr("Good fares — take a look", "좋은 가격을 확인해 보세요")}
            </>
          ) : (
            tr("Nothing below target yet", "아직 목표가에 닿은 여행이 없어요")
          )
        }
      />
      <StatCard
        label={tr("Below your targets by", "목표가 대비 절약 금액")}
        icon={<PiggyBank size={17} aria-hidden />}
        value={formatMoney(saved.amount, saved.currency)}
        foot={
          <>
            <i aria-hidden />
            {below > 0 ? basisText : tr("Shown once a fare reaches its target", "가격이 목표가에 닿으면 표시돼요")}
            {saved.otherCurrencies > 0 && tr(` · +${saved.otherCurrencies} more currencies`, ` · 다른 통화 ${saved.otherCurrencies}개`)}
          </>
        }
      />
      <Link to="/trips/new" className="dash-stat dash-promo">
        <span className="dash-promo__icon" aria-hidden>
          <Sparkles size={18} />
        </span>
        <span className="dash-promo__text">
          <b>
            {tr("Plan lightly.", "준비는 가볍게.")}
            <br />
            {tr("Travel richly.", "여행은 다채롭게.")}
          </b>
          <span className="dash-promo__link">
            {tr("Plan a new trip", "새 여행 준비하기")} <ArrowUpRight size={13} aria-hidden />
          </span>
        </span>
      </Link>
    </section>
  );
}

// ----------------------------------------------------------------------------- trip card

function TripCardSkeleton() {
  return (
    <div className="card trip-card" aria-hidden>
      <div className="trip-card__hero trip-card__hero--skeleton" />
      <div className="trip-card__body stack-sm">
        <Skeleton width="55%" height={18} />
        <Skeleton width="80%" />
        <Skeleton width="45%" height={26} />
        <div className="row" style={{ marginTop: 12 }}>
          <Skeleton width={120} height={30} />
          <Skeleton width={110} height={36} />
        </div>
      </div>
    </div>
  );
}

function TripCard({ trip, onChange }: { trip: TripOut; onChange: (trip: TripOut) => void }) {
  const history = useAsync(() => api.priceHistory(trip.id), [trip.id, trip.last_checked_at]);
  const [error, setError] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const [toggling, setToggling] = useState(false);
  const now = useNow();
  const destAirport = useAirport(trip.destination);
  const { run, watch } = useRunWatcher((done) => {
    if (done.status === "error") setError(done.error ?? tr("The check failed", "가격 확인에 실패했어요"));
    void api.getTrip(trip.id).then(onChange).catch(() => undefined);
    void history.reload();
  });
  const checking = starting || run?.status === "running";

  const values = historyValues(history.data, trip.price_basis);
  const trend = priceTrend(values);
  // "Lowest now" is the latest finished run only; null = that run found no matching offer.
  const current = trip.latest_min_price_basis_value;
  // The delta needs history that already includes that run (it reloads after each check).
  const historyCurrent = current !== null && trend.latest !== null && Math.abs(trend.latest - current) < 1;
  const belowTarget = current !== null && trip.target_price !== null && current <= trip.target_price;
  const status = checking ? "running" : (trip.last_status ?? "none");

  const checkNow = async () => {
    setError(null);
    setStarting(true);
    try {
      watch(await api.checkTrip(trip.id));
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setStarting(false);
    }
  };

  const toggleActive = async (active: boolean) => {
    setToggling(true);
    setError(null);
    try {
      onChange(await api.updateTrip(trip.id, { active }));
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setToggling(false);
    }
  };

  const variant = heroVariant(trip.destination, destAirport?.city_code);
  const city = cityName(destAirport) ?? trip.destination;
  const roundTrip = trip.trip_type === "round_trip";
  const RouteArrow = roundTrip ? ArrowLeftRight : ArrowRight;
  const sparkValues = values.filter((v): v is number => v !== null);
  const diff = current !== null && trip.target_price !== null ? trip.target_price - current : null;

  return (
    <article className={`card trip-card${trip.active ? "" : " is-paused"}`} aria-labelledby={`trip-${trip.id}-name`}>
      <div className={`trip-card__hero trip-card__hero--${variant}`}>
        <span className="trip-card__shade" aria-hidden />
        <div className="trip-card__hero-top">
          <span className="trip-card__glass">
            {trip.active ? tr("Monitoring", "추적 중") : tr("Paused", "일시 중지")}
          </span>
          <Link
            to={`/trips/${trip.id}`}
            className="trip-card__open"
            aria-label={tr(`Open ${trip.name}`, `${trip.name} 열기`)}
            title={tr("Open trip", "여행 열기")}
          >
            <ArrowUpRight size={16} aria-hidden />
          </Link>
        </div>
        <div className="trip-card__hero-bottom">
          <span className="trip-card__codes">
            {trip.origin}
            <RouteArrow size={14} aria-label={roundTrip ? tr("round trip", "왕복") : tr("one way", "편도")} />
            {trip.destination}
          </span>
          <p className="trip-card__city">
            <strong>{city}</strong>
            <span>, {tagline(variant)}</span>
          </p>
        </div>
      </div>

      <div className="trip-card__body">
        <div className="trip-card__titlebar">
          <h3 id={`trip-${trip.id}-name`} className="trip-card__name">
            <Link to={`/trips/${trip.id}`}>{trip.name}</Link>
          </h3>
          <div className="trip-card__chips">
            <StatusChip status={status} title={trip.last_error ?? undefined} />
            {trip.unread_alerts > 0 && (
              <Badge tone="bad">{tr(`${trip.unread_alerts} new`, `새 알림 ${trip.unread_alerts}`)}</Badge>
            )}
          </div>
        </div>

        <div className="trip-card__meta">
          <span>
            <CalendarDays size={14} aria-hidden />
            {formatDateRange(trip.depart_date, roundTrip ? trip.return_date : null)}
          </span>
          <span>
            <Users size={14} aria-hidden />
            {formatAdults(trip.adults)}
          </span>
          <span>{CABIN_LABEL[trip.cabin]}</span>
          {trip.direct_only && <span>{formatStops(0)}</span>}
        </div>

        <div className="trip-card__fare">
          <div className="trip-card__fare-main">
            <small>{tr("Lowest matching fare", "조건에 맞는 최저 요금")}</small>
            <b className="trip-card__price">
              {formatMoney(current, trip.currency)}
              {current !== null && <span> / {basisNote(trip.price_basis, trip.adults)}</span>}
            </b>
            {current !== null && trip.adults > 1 && trip.latest_min_price !== null && (
              <span className="trip-card__sub">
                {trip.price_basis === "per_person"
                  ? tr(
                      `${formatMoney(trip.latest_min_price, trip.currency)} total for ${trip.adults} adults`,
                      `성인 ${trip.adults}명 총액 ${formatMoney(trip.latest_min_price, trip.currency)}`,
                    )
                  : tr(
                      `${formatMoney(trip.latest_min_price / trip.adults, trip.currency)} per person`,
                      `1인 ${formatMoney(trip.latest_min_price / trip.adults, trip.currency)}`,
                    )}
              </span>
            )}
            {current === null && (
              <span className="trip-card__sub">
                {!trip.last_checked_at
                  ? tr("No checks yet", "아직 확인하지 않았어요")
                  : trip.last_status === "error"
                    ? tr("The latest check failed", "마지막 확인에 실패했어요")
                    : tr("No matching offers in the latest check", "마지막 확인에서 조건에 맞는 항공권이 없었어요")}
              </span>
            )}
            {current === null && trip.last_checked_at && trend.lastFound !== null && trend.lastFoundRunsAgo !== null && (
              <span className="trip-card__sub">
                {tr("Last found ", "마지막 발견가 ")}
                <span className="num">{formatMoney(trend.lastFound, trip.currency)}</span>
                {tr(
                  ` · ${trend.lastFoundRunsAgo} check${trend.lastFoundRunsAgo === 1 ? "" : "s"} ago`,
                  ` · ${trend.lastFoundRunsAgo}회 전 확인`,
                )}
              </span>
            )}
            {historyCurrent && (
              <span className="trip-card__delta">
                <PriceDelta
                  current={trend.latest}
                  previous={trend.previous}
                  currency={trip.currency}
                  context={
                    trend.previousIsAdjacent
                      ? tr("vs previous check", "직전 확인 대비")
                      : tr("vs last price found", "마지막 발견가 대비")
                  }
                />
              </span>
            )}
          </div>
          <div className="trip-card__fare-side">
            {belowTarget ? (
              <Badge tone="good" icon={<TrendingDown size={12} aria-hidden />}>
                {tr("Below target", "목표가 이하")}
              </Badge>
            ) : current !== null ? (
              <Badge tone="outline">{tr("Watching", "지켜보는 중")}</Badge>
            ) : null}
            <Sparkline
              values={sparkValues}
              target={trip.target_price}
              endIsCurrent={trend.latest !== null}
              width={120}
              height={38}
              label={tr(
                `Lowest price over the last ${sparkValues.length} checks`,
                `최근 ${sparkValues.length}회 확인의 최저가 추이`,
              )}
            />
          </div>
        </div>

        <dl className="trip-card__facts">
          <div>
            <dt>{tr("Lowest ever", "역대 최저가")}</dt>
            <dd>{formatMoney(trip.lowest_ever, trip.currency)}</dd>
          </div>
          <div>
            <dt>
              <CalendarClock size={13} aria-hidden /> {tr("Next check", "다음 확인")}
            </dt>
            <dd title={trip.next_run_at ? formatUtc(trip.next_run_at) : undefined}>
              {!trip.active ? tr("Paused", "일시 중지") : trip.next_run_at ? formatUtc(trip.next_run_at) : "—"}
            </dd>
          </div>
          <div>
            <dt>{tr("Last check", "마지막 확인")}</dt>
            <dd title={trip.last_checked_at ? formatUtc(trip.last_checked_at) : undefined}>
              {trip.last_checked_at ? formatRelative(trip.last_checked_at, now) : tr("Never", "없음")}
            </dd>
          </div>
        </dl>

        <div aria-live="polite" className="trip-card__live">
          {checking && (
            <span className="live-status">
              {tr("Checking prices…", "가격 확인 중…")}
              {run &&
                run.combinations_total > 0 &&
                tr(
                  ` ${run.combinations_searched}/${run.combinations_total} searches`,
                  ` 검색 ${run.combinations_searched}/${run.combinations_total}`,
                )}
            </span>
          )}
          {error && <span className="field__error" role="alert">{error}</span>}
        </div>

        <Link to={`/trips/${trip.id}`} className="trip-card__bottom">
          <span>
            {tr("Target", "목표 요금")}{" "}
            <b>{trip.target_price !== null ? formatMoney(trip.target_price, trip.currency) : tr("not set", "미설정")}</b>
          </span>
          <span className={diff !== null && diff >= 0 ? "trip-card__gap is-under" : "trip-card__gap"}>
            {diff === null
              ? tr("View details", "자세히 보기")
              : diff >= 0
                ? tr(`${formatMoney(diff, trip.currency)} under`, `${formatMoney(diff, trip.currency)} 절약`)
                : tr(`${formatMoney(-diff, trip.currency)} to go`, `목표까지 ${formatMoney(-diff, trip.currency)}`)}
            <ChevronRight size={14} aria-hidden />
          </span>
        </Link>
      </div>

      <div className="card__foot trip-card__foot">
        <Switch
          checked={trip.active}
          disabled={toggling}
          onChange={toggleActive}
          label={trip.active ? tr("Monitoring", "추적 중") : tr("Paused", "일시 중지")}
        />
        <span className="grow" />
        <Link
          className="btn btn--ghost btn--sm"
          to={`/trips/${trip.id}/edit`}
          aria-label={tr(`Edit ${trip.name}`, `${trip.name} 수정`)}
        >
          <Pencil size={14} aria-hidden /> {tr("Edit", "수정")}
        </Link>
        <Button size="sm" variant="primary" icon={<RefreshCw size={14} aria-hidden />} busy={checking} onClick={checkNow}>
          {checking ? tr("Checking", "확인 중") : tr("Check now", "지금 확인")}
        </Button>
      </div>
    </article>
  );
}

// ----------------------------------------------------------------------------- page

/** Dashboard page. */
export function Dashboard() {
  useTitle(tr("Trips", "내 여행"));
  const trips = useAsync(() => api.listTrips(), []);
  const list = trips.data ?? [];
  const replace = (next: TripOut) => trips.setData((prev) => (prev ?? []).map((t) => (t.id === next.id ? next : t)));
  const active = list.filter((t) => t.active).length;
  const below = list.filter(isBelowTarget).length;

  return (
    <>
      <div className="dash-head">
        <div className="dash-head__text">
          <span className="dash-head__eyebrow">{tr("One step closer to your next trip", "다음 여행에 한 걸음 더 가까이")}</span>
          <h1>{tr("Your next trip starts here.", "다음 여행, 여기서 시작하세요.")}</h1>
          <p>
            {tr(
              "Keep an eye on fares and catch the right moment to book.",
              "항공권 가격을 살피고, 좋은 여행의 기회를 만나보세요.",
            )}
          </p>
        </div>
        <Link className="btn btn--primary dash-head__cta" to="/trips/new">
          <Plus size={16} aria-hidden /> {tr("New trip", "새 여행")}
        </Link>
      </div>

      <ErrorBanner
        error={trips.error}
        title={tr("Could not load trips", "여행 목록을 불러오지 못했어요")}
        onRetry={() => void trips.reload()}
      />

      {list.length > 0 && <DashboardStats list={list} />}

      {trips.loading && !trips.data && (
        <div className="trip-grid" aria-busy="true" aria-label={tr("Loading trips", "여행 목록 불러오는 중")}>
          <TripCardSkeleton />
          <TripCardSkeleton />
        </div>
      )}

      {trips.data && list.length === 0 && (
        <div className="card dash-empty">
          <EmptyState
            icon={<PlaneTakeoff size={26} />}
            title={tr("No trips monitored yet", "아직 추적 중인 여행이 없어요")}
            action={
              <Link className="btn btn--primary" to="/trips/new">
                <Plus size={16} aria-hidden /> {tr("Create your first trip", "첫 여행 만들기")}
              </Link>
            }
          >
            {tr(
              "Add a route and dates, set a target price, and the app checks fares on a schedule and notifies you when the price drops.",
              "노선과 날짜, 목표 가격을 정해 두면 정해진 시각마다 항공권 가격을 확인하고 가격이 내려가면 알려 드려요.",
            )}
          </EmptyState>
        </div>
      )}

      {list.length > 0 && (
        <section aria-labelledby="watched-trips">
          <div className="dash-section">
            <div>
              <h2 id="watched-trips">
                {tr("Watched trips", "관심 여행")}
                <span className="dash-section__count">{list.length}</span>
              </h2>
              <p>
                {tr(
                  `${list.length} trip${list.length === 1 ? "" : "s"} · ${active} monitoring${below > 0 ? ` · ${below} below target` : ""}`,
                  `여행 ${list.length}개 · ${active}개 추적 중${below > 0 ? ` · ${below}개 목표가 이하` : ""}`,
                )}
              </p>
            </div>
          </div>
          <div className="trip-grid">
            {list.map((trip) => (
              <TripCard key={trip.id} trip={trip} onChange={replace} />
            ))}
          </div>
        </section>
      )}
    </>
  );
}
