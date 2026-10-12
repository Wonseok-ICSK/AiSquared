/** Flights tab: offers of a run (default: latest finished) with sorting, filters and history. */

import { useState } from "react";
import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import { ArrowRight, History, Plane, PlaneLanding, PlaneTakeoff, RefreshCw, SearchX } from "lucide-react";
import { api } from "../../api/endpoints";
import type { FlightLegOut, FlightOfferOut, OfferSort, TripOut } from "../../api/types";
import { Popover } from "../../components/Popover";
import { PriceDelta } from "../../components/PriceDelta";
import { Sparkline } from "../../components/charts/Sparkline";
import { Badge, Button, EmptyState, ErrorBanner, Segmented, SkeletonBlock, Switch } from "../../components/ui";
import {
  CABIN_LABEL,
  dayDiff,
  formatAdults,
  formatDate,
  formatDuration,
  formatLocalTime,
  formatMoney,
  formatStops,
  formatUtc,
} from "../../lib/format";
import { useAsync } from "../../lib/hooks";
import { tr } from "../../lib/i18n";
import { useProviderNames } from "../../lib/providers";
import {
  legAirline,
  legCarriers,
  legFlights,
  legRoute,
  offerAirline,
  offerFlights,
  sameAirline,
  separateTickets,
} from "./tripText";

function sortOptions(): { value: OfferSort; label: string }[] {
  return [
    { value: "price", label: tr("Price", "가격") },
    { value: "departure", label: tr("Departure", "출발 시간") },
    { value: "duration", label: tr("Duration", "소요 시간") },
  ];
}

function plusDaysTitle(plusDays: number): string {
  return tr(`Arrives ${plusDays} day${plusDays > 1 ? "s" : ""} later`, `${plusDays}일 뒤 도착`);
}

/** "2시간 45분 · 직항" / "3h 10m · 1 stop (PEK)". */
function legShape(leg: FlightLegOut): string {
  const stops = formatStops(leg.stops);
  return [
    formatDuration(leg.duration_minutes),
    leg.layovers.length > 0 ? `${stops} (${leg.layovers.join(", ")})` : stops,
  ].join(" · ");
}

/** Departure time — line with duration and stops — arrival time, airports under the times. */
export function LegCell({ leg }: { leg: FlightLegOut }) {
  const plusDays = dayDiff(leg.departure_time, leg.arrival_time);
  const stops = formatStops(leg.stops);
  return (
    <div
      className="leg-line"
      role="group"
      aria-label={tr(
        `${leg.departure_airport} ${formatLocalTime(leg.departure_time)} to ${leg.arrival_airport} ${formatLocalTime(leg.arrival_time)}, ${formatDuration(leg.duration_minutes)}, ${stops}`,
        `${leg.departure_airport} ${formatLocalTime(leg.departure_time)} 출발, ${leg.arrival_airport} ${formatLocalTime(leg.arrival_time)} 도착, ${formatDuration(leg.duration_minutes)}, ${stops}`,
      )}
    >
      <div className="leg-line__end" aria-hidden>
        <b className="serif tnum">{formatLocalTime(leg.departure_time)}</b>
        <span>{leg.departure_airport}</span>
      </div>
      <div className="leg-line__mid" aria-hidden>
        <span>{formatDuration(leg.duration_minutes)}</span>
        <div className="leg-line__track">
          <i />
          <span />
          <Plane size={12} />
        </div>
        <small className={leg.stops === 0 ? "is-direct" : undefined}>
          {stops}
          {leg.layovers.length > 0 && ` · ${leg.layovers.join(", ")}`}
        </small>
      </div>
      <div className="leg-line__end leg-line__end--arr" aria-hidden>
        <b className="serif tnum">
          {formatLocalTime(leg.arrival_time)}
          {plusDays > 0 && (
            <sup className="leg__plus" title={plusDaysTitle(plusDays)}>
              +{plusDays}
            </sup>
          )}
        </b>
        <span>{leg.arrival_airport}</span>
      </div>
    </div>
  );
}

/** "Air Seoul RS260 · 11월 27일 12:50 NRT → 15:35 ICN · 2시간 45분 · 직항". */
function inboundText(leg: FlightLegOut): string {
  return [`${legAirline(leg)} ${legFlights(leg)}`.trim(), legRoute(leg), legShape(leg)].filter(Boolean).join(" · ");
}

/** "오는 편" pill + date: the head of a leg block (both legs share it, so they read as a pair). */
function LegTag({ dir, date, children }: { dir: string | null; date: string | null; children?: ReactNode }) {
  return (
    <div className="flight-leg__tag">
      {dir && <span className="flight-leg__dir">{dir}</span>}
      {date && <span className="tnum">{formatDate(date)}</span>}
      {children}
    </div>
  );
}

/**
 * The return flight of a round trip, laid out like the outbound: tag and date, its flight
 * number (with its own airline and carrier mark when that differs from the outbound), then
 * times, airports, duration and stops. Unknown until the booking step: a dashed placeholder
 * of the same size.
 */
function ReturnLeg({ offer }: { offer: FlightOfferOut }) {
  const dir = tr("Return", "오는 편");
  const leg = offer.inbound;
  if (!leg) {
    return (
      <div
        className="flight-leg flight-leg--return"
        title={tr(
          "Google pairs a return flight with this price. The booking step looks it up and shows it before you book.",
          "Google이 이 가격에 맞춰 짝지은 오는 편이 있어요. 예약 준비에서 그 항공편을 찾아 보여드려요.",
        )}
      >
        <LegTag dir={dir} date={offer.return_date} />
        <div className="leg-pending">
          <PlaneLanding size={16} aria-hidden />
          <span className="leg-pending__text">
            <span className="leg-pending__title">
              {tr("Return flight is confirmed on the booking step", "오는 편은 예약 준비에서 확인해요")}
            </span>
            <span className="leg-pending__note">
              {tr("The flight Google pairs with this price", "Google이 이 가격에 짝지은 항공편이에요")}
            </span>
          </span>
        </div>
      </div>
    );
  }
  const ownAirline = !sameAirline(offer.outbound, leg);
  const code = legCarriers(leg)[0] ?? null;
  const flights = legFlights(leg);
  return (
    <div className="flight-leg flight-leg--return">
      <LegTag dir={dir} date={leg.departure_time}>
        {(ownAirline || flights) && (
          <span className="flight-leg__carrier">
            {ownAirline && (
              <>
                <span className="carrier-mark carrier-mark--sm" aria-hidden>
                  {code ?? <Plane size={11} />}
                </span>
                <span className="flight-leg__airline">{legAirline(leg) || tr("Unknown airline", "항공사 정보 없음")}</span>
              </>
            )}
            {flights && <span className="flight-leg__number mono">{flights}</span>}
          </span>
        )}
      </LegTag>
      <LegCell leg={leg} />
    </div>
  );
}

function OfferHistory({ tripId, offer }: { tripId: number; offer: FlightOfferOut }) {
  const history = useAsync(() => api.offerHistory(tripId, offer.id), [tripId, offer.id]);
  const names = useProviderNames();
  if (history.error) return <ErrorBanner error={history.error} title={tr("Could not load history", "가격 기록을 불러오지 못했어요")} />;
  if (!history.data) return <SkeletonBlock lines={4} label={tr("Loading offer history", "항공편 가격 기록 불러오는 중")} />;
  const h = history.data;
  const values = h.points.map((p) => p.price_per_person);
  const n = h.points.length;
  const lowest = formatMoney(h.lowest_per_person, h.currency);
  return (
    <div className="stack-sm">
      <div className="row-between">
        <div>
          <div className="strong">{tr("Price history of this flight", "이 항공편의 가격 추이")}</div>
          <div className="xsmall subtle">
            {tr(
              `${n} observation${n === 1 ? "" : "s"} · lowest ${lowest} per person`,
              `조회 ${n}회 · 1인 최저 ${lowest}`,
            )}
          </div>
        </div>
        <Sparkline
          values={values}
          label={tr(`Per-person price over ${values.length} observations`, `최근 조회 ${values.length}회의 1인 가격`)}
          width={110}
          height={34}
        />
      </div>
      <div className="table-wrap" style={{ maxHeight: 240, overflowY: "auto" }}>
        <table className="table">
          <thead>
            <tr>
              <th scope="col">{tr("Checked", "확인 시각")}</th>
              <th scope="col">{tr("Source", "검색 소스")}</th>
              <th scope="col" className="num">
                {tr("Per person", "1인 가격")}
              </th>
              <th scope="col" className="num">
                {tr("Total", "총액")}
              </th>
            </tr>
          </thead>
          <tbody>
            {[...h.points].reverse().map((p) => (
              <tr key={p.offer_id}>
                <td className="nowrap">{formatUtc(p.searched_at)}</td>
                <td>{names(p.provider)}</td>
                <td className="num">{formatMoney(p.price_per_person, h.currency)}</td>
                <td className="num">{formatMoney(p.price_total, h.currency)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function OfferRow({
  offer,
  trip,
  best,
  showMatch,
}: {
  offer: FlightOfferOut;
  trip: TripOut;
  best: boolean;
  showMatch: boolean;
}) {
  const names = useProviderNames();
  const pax = formatAdults(trip.adults);
  const flights = offerFlights(offer);
  const airline = offerAirline(offer);
  const carrier = legCarriers(offer.outbound)[0] ?? null;
  const roundTrip = trip.trip_type === "round_trip";
  const twoTickets = roundTrip && separateTickets(offer) === true;
  const total = formatMoney(offer.price_total, offer.currency);
  const perPerson = formatMoney(offer.price_per_person, offer.currency);
  const historyLabel = tr("Price history of this flight", "이 항공편의 가격 추이");
  const source = [names(offer.provider), offer.seller ? tr(`via ${offer.seller}`, `판매처 ${offer.seller}`) : null]
    .filter(Boolean)
    .join(" · ");
  const ticketsTitle = tr(
    `Different airlines each way: ${airline} out, ${offer.inbound ? legAirline(offer.inbound) : ""} back. These are usually booked as two separate tickets.`,
    `가는 편(${airline})과 오는 편(${offer.inbound ? legAirline(offer.inbound) : ""}) 항공사가 달라요. 보통 항공권 두 장을 따로 예약해요.`,
  );

  return (
    <li className={`flight-row${offer.matches_filters ? "" : " is-dim"}${best ? " is-best" : ""}`}>
      <div className="flight-row__main">
        <div className="flight-row__head">
          <span className="carrier-mark" aria-hidden>
            {carrier ?? <Plane size={15} />}
          </span>
          <div className="flight-row__carrier">
            <span className="flight-row__airline">
              {airline}
              {flights && <span className="flight-row__number mono">{flights}</span>}
            </span>
            <span className="flight-row__source">{source}</span>
          </div>
          <div className="flight-row__chips">
            {best && <Badge tone="good">{tr("Cheapest", "최저가")}</Badge>}
            {twoTickets && <Badge title={ticketsTitle}>{tr("2 tickets", "항공권 2장")}</Badge>}
            {showMatch &&
              (offer.matches_filters ? (
                <Badge tone="info">{tr("Matches filters", "조건 일치")}</Badge>
              ) : (
                <Badge tone="outline">{tr("Outside filters", "조건 밖")}</Badge>
              ))}
          </div>
        </div>

        <div className="flight-legs">
          <div className="flight-leg">
            <LegTag dir={roundTrip ? tr("Outbound", "가는 편") : null} date={offer.outbound.departure_time} />
            <LegCell leg={offer.outbound} />
          </div>
          {roundTrip && <ReturnLeg offer={offer} />}
        </div>
      </div>

      <div className="flight-row__side">
        <div className="fare">
          <b className="fare__total serif tnum">{total}</b>
          <span className="fare__basis">
            {roundTrip ? tr(`Round trip · total for ${pax}`, `${pax} · 왕복 총액`) : tr(`One way · total for ${pax}`, `${pax} · 편도 총액`)}
          </span>
          {trip.adults > 1 && <span className="fare__pp tnum">{tr(`${perPerson} per person`, `1인 ${perPerson}`)}</span>}
          {offer.previous_price !== null ? (
            <PriceDelta
              current={offer.price_total}
              previous={offer.previous_price * trip.adults}
              currency={offer.currency}
              context={tr("total vs previous check", "직전 확인 대비 총액")}
            />
          ) : (
            <span className="fare__new">{tr("New this check", "이번에 새로 찾음")}</span>
          )}
        </div>
        <div className="flight-row__actions">
          <Popover
            label={tr(`Price history of ${flights || "this flight"}`, `${flights || "이 항공편"} 가격 추이`)}
            trigger={(props) => (
              <button type="button" className="btn btn--ghost btn--sm" {...props} aria-label={historyLabel} title={historyLabel}>
                <History size={14} aria-hidden />
                <span>{tr("History", "가격 추이")}</span>
              </button>
            )}
          >
            {() => <OfferHistory tripId={trip.id} offer={offer} />}
          </Popover>
          <Link
            className="btn btn--soft"
            to={`/trips/${trip.id}/book/${offer.id}`}
            title={tr(
              `See both flights, the tickets to buy and where to book them for ${pax}`,
              `${pax} 기준으로 가는 편·오는 편과 예약할 항공권, 예약할 곳을 확인해요`,
            )}
          >
            {tr("Book", "예약하기")} <ArrowRight size={14} aria-hidden />
          </Link>
        </div>
      </div>

      <details className="flight-row__details">
        <summary>{tr("Fare & baggage details", "운임·수하물 상세")}</summary>
        <dl className="fare-details">
          <div>
            <dt>{tr("Total", "총액")}</dt>
            <dd>
              {total} · {tr(`for ${pax}`, `${pax} 기준`)}
            </dd>
          </div>
          <div>
            <dt>{tr("Per person", "1인 가격")}</dt>
            <dd>{perPerson}</dd>
          </div>
          <div>
            <dt>{tr("Previous / lowest (per person)", "이전 / 최저 (1인)")}</dt>
            <dd>
              {offer.previous_price !== null ? formatMoney(offer.previous_price, offer.currency) : tr("New", "신규")} ·{" "}
              {tr(`low ${formatMoney(offer.lowest_price, offer.currency)}`, `최저 ${formatMoney(offer.lowest_price, offer.currency)}`)}
            </dd>
          </div>
          <div>
            <dt>{tr("Cabin", "좌석 등급")}</dt>
            <dd>
              {CABIN_LABEL[offer.cabin]}
              {offer.fare_type && ` · ${offer.fare_type}`}
            </dd>
          </div>
          <div>
            <dt>{tr("Baggage", "수하물")}</dt>
            <dd>{offer.baggage ?? tr("Not shown by the source — check on the booking page", "검색 소스에 정보가 없어요. 예약 페이지에서 확인하세요")}</dd>
          </div>
          <div>
            <dt>{tr("Outbound", "가는 편")}</dt>
            <dd>
              {`${airline} ${flights}`.trim()} · {legRoute(offer.outbound)} · {legShape(offer.outbound)}
            </dd>
          </div>
          {roundTrip && (
            <div>
              <dt>{tr("Return", "오는 편")}</dt>
              <dd>
                {offer.inbound
                  ? inboundText(offer.inbound)
                  : tr(
                      `${formatDate(offer.return_date)} · the flight Google pairs with this price, confirmed on the booking step`,
                      `${formatDate(offer.return_date)} · Google이 이 가격에 짝지은 항공편이에요. 예약 준비에서 확인해요`,
                    )}
              </dd>
            </div>
          )}
          {roundTrip && offer.inbound && (
            <div>
              <dt>{tr("Tickets", "항공권")}</dt>
              <dd>
                {twoTickets
                  ? tr("2 separate tickets, one per direction", "편도 항공권 2장 (방향마다 따로 예약)")
                  : tr("Usually one round-trip ticket", "보통 왕복 항공권 1장")}
              </dd>
            </div>
          )}
          <div>
            <dt>{tr("Source", "검색 소스")}</dt>
            <dd>
              {source} · {formatUtc(offer.searched_at)}
            </dd>
          </div>
          <div>
            <dt>{tr("Your filters", "검색 조건")}</dt>
            <dd>{offer.matches_filters ? tr("Matches filters", "조건 일치") : tr("Outside filters", "조건 밖")}</dd>
          </div>
        </dl>
      </details>
    </li>
  );
}

/** Offers list for the trip. */
export function FlightsTab({
  trip,
  refreshKey,
  onCheck,
  checking,
}: {
  trip: TripOut;
  refreshKey: number;
  onCheck: () => void;
  checking: boolean;
}) {
  const [showAll, setShowAll] = useState(false);
  const [sort, setSort] = useState<OfferSort>("price");
  const [runId, setRunId] = useState<number | "">("");
  const runs = useAsync(() => api.listRuns(trip.id, 30), [trip.id, refreshKey]);
  const offers = useAsync(
    () =>
      api.listOffers(trip.id, {
        run_id: runId === "" ? undefined : runId,
        only_matching: !showAll,
        sort,
        limit: 200,
      }),
    [trip.id, runId, showAll, sort, refreshKey],
  );

  const finishedRuns = (runs.data ?? []).filter((r) => r.status === "ok" || r.status === "partial");
  const list = offers.data ?? [];
  const minPrice = list.filter((o) => o.matches_filters).reduce<number | null>(
    (m, o) => (m === null || o.price_total < m ? o.price_total : m),
    null,
  );
  const selectedRun = runId === "" ? finishedRuns[0] : finishedRuns.find((r) => r.id === runId);
  const n = list.length;

  return (
    <div className="stack">
      <div className="toolbar flights-toolbar">
        <label className="toolbar__item">
          <span className="toolbar__label">{tr("Run", "검색 회차")}</span>
          <select
            className="select"
            value={runId}
            onChange={(e) => setRunId(e.target.value === "" ? "" : Number(e.target.value))}
          >
            <option value="">{tr("Latest finished run", "가장 최근 검색")}</option>
            {finishedRuns.map((r) => (
              <option key={r.id} value={r.id}>
                {formatUtc(r.started_at)} ·{" "}
                {tr(`${r.offers_matching}/${r.offers_found} offers`, `항공권 ${r.offers_matching}/${r.offers_found}개`)}
              </option>
            ))}
          </select>
        </label>
        <div className="toolbar__item">
          <span className="toolbar__label">{tr("Sort by", "정렬")}</span>
          <Segmented label={tr("Sort offers by", "항공권 정렬 기준")} value={sort} options={sortOptions()} onChange={setSort} />
        </div>
        <div className="toolbar__item toolbar__item--end">
          <Switch checked={showAll} onChange={setShowAll} label={tr("Show non-matching", "조건 밖 항공권도 보기")} />
        </div>
      </div>

      <ErrorBanner
        error={offers.error}
        title={tr("Could not load offers", "항공권을 불러오지 못했어요")}
        onRetry={() => void offers.reload()}
      />

      {offers.loading && !offers.data && (
        <div className="card">
          <div className="card__body">
            <SkeletonBlock lines={6} label={tr("Loading offers", "항공권 불러오는 중")} />
          </div>
        </div>
      )}

      {offers.data && list.length === 0 && (
        <div className="card">
          {finishedRuns.length === 0 && !runs.loading ? (
            <EmptyState
              icon={<PlaneTakeoff size={26} />}
              title={tr("No prices yet", "아직 가격 정보가 없어요")}
              action={
                <Button variant="primary" icon={<RefreshCw size={16} aria-hidden />} busy={checking} onClick={onCheck}>
                  {tr("Check now", "지금 확인")}
                </Button>
              }
            >
              {tr(
                "The first scheduled check has not run yet. Start one now to see current fares.",
                "첫 자동 확인이 아직 실행되지 않았어요. 지금 확인해서 현재 요금을 보세요.",
              )}
            </EmptyState>
          ) : (
            <EmptyState
              icon={<SearchX size={26} />}
              title={
                showAll
                  ? tr("No offers in this run", "이번 검색에서 찾은 항공권이 없어요")
                  : tr("No offers match your filters", "조건에 맞는 항공권이 없어요")
              }
              action={
                !showAll && (
                  <Button onClick={() => setShowAll(true)}>{tr("Show non-matching offers", "조건 밖 항공권 보기")}</Button>
                )
              }
            >
              {showAll
                ? tr(
                    "The sources returned no flights for this run. Check the run log in Price history.",
                    "이번 검색에서 검색 소스가 항공편을 하나도 찾지 못했어요. 가격 추이 탭의 검색 기록을 확인하세요.",
                  )
                : tr(
                    "Try widening the time windows, stops or maximum price in the trip settings.",
                    "여행 설정에서 시간대, 경유 조건, 최대 가격을 넓혀 보세요.",
                  )}
            </EmptyState>
          )}
        </div>
      )}

      {list.length > 0 && (
        <section className={`card flight-list${offers.loading ? " is-refreshing" : ""}`} aria-label={tr("Flight offers", "항공권 목록")}>
          {selectedRun && (
            <div className="flight-list__head" aria-live="polite">
              <span className="strong">
                {tr(`${n} offer${n === 1 ? "" : "s"} shown`, `항공권 ${n}개 표시`)}
                {!showAll &&
                  selectedRun.offers_found > selectedRun.offers_matching &&
                  tr(
                    ` · ${selectedRun.offers_found - selectedRun.offers_matching} hidden by your filters`,
                    ` · 조건에 맞지 않아 숨긴 항공권 ${selectedRun.offers_found - selectedRun.offers_matching}개`,
                  )}
              </span>
              <span className="xsmall subtle">
                {tr(`Searched ${formatUtc(selectedRun.started_at)}`, `검색 시각 ${formatUtc(selectedRun.started_at)}`)}
              </span>
            </div>
          )}
          <ul className="flight-rows">
            {list.map((offer) => (
              <OfferRow
                key={offer.id}
                offer={offer}
                trip={trip}
                best={offer.matches_filters && offer.price_total === minPrice}
                showMatch={showAll}
              />
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}
