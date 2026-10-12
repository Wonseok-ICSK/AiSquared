/** A watched trip on the home page: a photo banner of the destination (route codes and city
 * name), the trip's name and facts, the current lowest fare with its change and trend, the
 * target price as one plain line, check times and the trip's actions. */

import { useState } from "react";
import { Link } from "react-router-dom";
import { ArrowUpRight, Pencil, RefreshCw } from "lucide-react";
import { api } from "../../api/endpoints";
import { errorMessage } from "../../api/client";
import type { PriceBasis, TripOut } from "../../api/types";
import { PriceDelta } from "../PriceDelta";
import { Sparkline } from "../charts/Sparkline";
import { Badge, Button, Skeleton, StatusChip, Switch } from "../ui";
import {
  CABIN_LABEL,
  formatAdults,
  formatDateRange,
  formatMoney,
  formatRelative,
  formatStops,
  formatUtc,
} from "../../lib/format";
import { useAsync, useNow } from "../../lib/hooks";
import { historyValues, priceTrend } from "../../lib/history";
import { tr } from "../../lib/i18n";
import { useRunWatcher } from "../../lib/runs";
import { targetLine } from "./home";
import { TripBanner, TripBannerSkeleton, useDestinationCity } from "./TripBanner";

function basisNote(basis: PriceBasis, adults: number): string {
  return basis === "per_person"
    ? tr("per person", "1인 기준")
    : tr(`total · ${formatAdults(adults)}`, `총액 · ${formatAdults(adults)}`);
}

/** Placeholder card while the trips load. */
export function TripCardSkeleton() {
  return (
    <div className="card home-trip" aria-hidden>
      <TripBannerSkeleton />
      <div className="home-trip__body">
        <div className="stack-sm">
          <Skeleton width="45%" height={18} />
          <Skeleton width="70%" height={12} />
        </div>
        <div className="home-trip__fare">
          <div className="stack-sm grow">
            <Skeleton width={90} height={12} />
            <Skeleton width={160} height={28} />
          </div>
          <Skeleton width={120} height={40} />
        </div>
        <Skeleton width="55%" height={12} />
      </div>
      <div className="home-trip__foot">
        <Skeleton width={110} height={24} />
        <span className="grow" />
        <Skeleton width={160} height={32} />
      </div>
    </div>
  );
}

/** One watched trip with its live "check now" run (polled until it finishes). */
export function TripCard({ trip, onChange }: { trip: TripOut; onChange: (trip: TripOut) => void }) {
  const history = useAsync(() => api.priceHistory(trip.id), [trip.id, trip.last_checked_at]);
  const [error, setError] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const [toggling, setToggling] = useState(false);
  const now = useNow();
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
  const target = targetLine(current, trip.target_price, trip.currency);
  const city = useDestinationCity(trip.destination);
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

  const roundTrip = trip.trip_type === "round_trip";
  const sparkValues = values.filter((v): v is number => v !== null);
  const nameId = `trip-${trip.id}-name`;

  return (
    <article className={`card home-trip${trip.active ? "" : " is-paused"}`} aria-labelledby={nameId}>
      <TripBanner trip={trip} city={city} />
      <div className="home-trip__body">
        <header className="home-trip__head">
          <div className="home-trip__title">
            <h3 id={nameId} className="home-trip__name">
              <Link to={`/trips/${trip.id}`}>{trip.name}</Link>
            </h3>
            {/* inner row is shifted left so a separator starting a wrapped line is clipped */}
            <p className="home-trip__meta">
              <span className="home-trip__meta-row">
                {/* the banner shows the route visually; it is aria-hidden, so say it here */}
                <span className="sr-only">
                  {roundTrip
                    ? tr(`${trip.origin} to ${trip.destination} (${city}) and back`, `${trip.origin}–${trip.destination}(${city}) 왕복`)
                    : tr(`${trip.origin} to ${trip.destination} (${city}), one way`, `${trip.origin}–${trip.destination}(${city}) 편도`)}
                </span>
                <span>{formatDateRange(trip.depart_date, roundTrip ? trip.return_date : null)}</span>
                <span>{formatAdults(trip.adults)}</span>
                <span>{CABIN_LABEL[trip.cabin]}</span>
                {trip.direct_only && <span>{formatStops(0)}</span>}
              </span>
            </p>
          </div>
          <div className="home-trip__chips">
            {status !== "ok" && <StatusChip status={status} title={trip.last_error ?? undefined} />}
            {!trip.active && <Badge tone="outline">{tr("Paused", "일시 중지")}</Badge>}
            {trip.unread_alerts > 0 && (
              <Badge tone="bad">{tr(`${trip.unread_alerts} new`, `새 알림 ${trip.unread_alerts}`)}</Badge>
            )}
          </div>
        </header>

        <div className="home-trip__fare">
          <div className="home-trip__price-block">
            <span className="home-trip__label">{tr("Lowest matching fare", "조건에 맞는 최저가")}</span>
            {current !== null ? (
              <p className="home-trip__price">
                <span className="home-trip__amount">{formatMoney(current, trip.currency)}</span>
                <span className="home-trip__basis">{basisNote(trip.price_basis, trip.adults)}</span>
              </p>
            ) : (
              <p className="home-trip__none">
                {!trip.last_checked_at
                  ? tr("No checks yet", "아직 확인하지 않았어요")
                  : trip.last_status === "error"
                    ? tr("The latest check failed", "마지막 확인에 실패했어요")
                    : tr("No matching offers in the latest check", "마지막 확인에서 조건에 맞는 항공권이 없었어요")}
              </p>
            )}
            <p className="home-trip__sub">
              {current !== null && trip.adults > 1 && trip.latest_min_price !== null && (
                <span>
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
              {current === null && trip.last_checked_at && trend.lastFound !== null && trend.lastFoundRunsAgo !== null && (
                <span>
                  {tr("Last found ", "마지막 발견가 ")}
                  <span className="num">{formatMoney(trend.lastFound, trip.currency)}</span>
                  {tr(
                    ` · ${trend.lastFoundRunsAgo} check${trend.lastFoundRunsAgo === 1 ? "" : "s"} ago`,
                    ` · ${trend.lastFoundRunsAgo}회 전 확인`,
                  )}
                </span>
              )}
              {historyCurrent && (
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
              )}
            </p>
          </div>
          <div className="home-trip__spark">
            <Sparkline
              values={sparkValues}
              target={trip.target_price}
              endIsCurrent={trend.latest !== null}
              width={132}
              height={44}
              label={tr(
                `Lowest price over the last ${sparkValues.length} checks`,
                `최근 ${sparkValues.length}회 확인의 최저가 추이`,
              )}
            />
          </div>
        </div>

        <p className={`home-trip__target${target?.reached ? " is-reached" : ""}`}>
          {target ? (
            <span className="num">{target.text}</span>
          ) : (
            <>
              {tr("Target not set", "목표가 미설정")}
              {" · "}
              <Link to={`/trips/${trip.id}/edit`}>{tr("Set one", "정하기")}</Link>
            </>
          )}
        </p>

        <dl className="home-trip__facts">
          <div>
            <dt>{tr("Last check", "마지막 확인")}</dt>
            <dd title={trip.last_checked_at ? formatUtc(trip.last_checked_at) : undefined}>
              {trip.last_checked_at ? formatRelative(trip.last_checked_at, now) : tr("Never", "없음")}
            </dd>
          </div>
          <div>
            <dt>{tr("Next check", "다음 확인")}</dt>
            <dd title={trip.next_run_at ? formatUtc(trip.next_run_at) : undefined}>
              {!trip.active ? tr("Paused", "일시 중지") : trip.next_run_at ? formatUtc(trip.next_run_at) : "—"}
            </dd>
          </div>
          <div>
            <dt>{tr("Lowest ever", "역대 최저가")}</dt>
            <dd className="num">{formatMoney(trip.lowest_ever, trip.currency)}</dd>
          </div>
        </dl>

        <div aria-live="polite" className="home-trip__live">
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
          {error && (
            <span className="field__error" role="alert">
              {error}
            </span>
          )}
        </div>
      </div>

      <div className="home-trip__foot">
        <Switch
          checked={trip.active}
          disabled={toggling}
          onChange={toggleActive}
          label={trip.active ? tr("Watching", "추적 중") : tr("Paused", "일시 중지")}
        />
        <span className="grow" />
        <Link
          className="btn btn--ghost btn--sm home-trip__edit"
          to={`/trips/${trip.id}/edit`}
          aria-label={tr(`Edit ${trip.name}`, `${trip.name} 수정`)}
          title={tr("Edit trip", "여행 수정")}
        >
          <Pencil size={14} aria-hidden />
          <span className="home-trip__edit-text">{tr("Edit", "수정")}</span>
        </Link>
        <Button size="sm" icon={<RefreshCw size={14} aria-hidden />} busy={checking} onClick={checkNow}>
          {checking ? tr("Checking", "확인 중") : tr("Check now", "지금 확인")}
        </Button>
        <Link
          className="btn btn--ghost btn--sm btn--icon"
          to={`/trips/${trip.id}`}
          aria-label={tr(`Open ${trip.name}`, `${trip.name} 열기`)}
          title={tr("Open trip", "여행 열기")}
        >
          <ArrowUpRight size={16} aria-hidden />
        </Link>
      </div>
    </article>
  );
}
