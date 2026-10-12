/** Flights tab: offers of a run (default: latest finished) with sorting, filters and history. */

import { useState } from "react";
import { ExternalLink, History, Plane, PlaneTakeoff, RefreshCw, SearchX } from "lucide-react";
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

function sortOptions(): { value: OfferSort; label: string }[] {
  return [
    { value: "price", label: tr("Price", "가격") },
    { value: "departure", label: tr("Departure", "출발 시간") },
    { value: "duration", label: tr("Duration", "소요 시간") },
  ];
}

/** Stable avatar tint per airline (one of four soft series colors). */
function airlineTint(name: string): number {
  let h = 0;
  for (const ch of name) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return h % 4;
}

function plusDaysTitle(plusDays: number): string {
  return tr(`Arrives ${plusDays} day${plusDays > 1 ? "s" : ""} later`, `${plusDays}일 뒤 도착`);
}

/** Wayfinder-style leg: big departure time — dotted line with duration/stops — big arrival time. */
export function LegCell({ leg, showDate }: { leg: FlightLegOut; showDate: boolean }) {
  const plusDays = dayDiff(leg.departure_time, leg.arrival_time);
  const stops = formatStops(leg.stops);
  return (
    <div
      className="leg-line"
      aria-label={tr(
        `${leg.departure_airport} ${formatLocalTime(leg.departure_time)} to ${leg.arrival_airport} ${formatLocalTime(leg.arrival_time)}, ${formatDuration(leg.duration_minutes)}, ${stops}`,
        `${leg.departure_airport} ${formatLocalTime(leg.departure_time)} 출발, ${leg.arrival_airport} ${formatLocalTime(leg.arrival_time)} 도착, ${formatDuration(leg.duration_minutes)}, ${stops}`,
      )}
    >
      <div className="leg-line__end" aria-hidden>
        <b className="tnum">{formatLocalTime(leg.departure_time)}</b>
        <span>{leg.departure_airport}</span>
        <small>{formatDate(leg.departure_time, showDate)}</small>
      </div>
      <div className="leg-line__mid" aria-hidden>
        <span>{formatDuration(leg.duration_minutes)}</span>
        <div className="leg-line__track">
          <i />
          <span />
          <Plane size={13} />
        </div>
        <small className={leg.stops === 0 ? "is-direct" : undefined}>
          {stops}
          {leg.layovers.length > 0 && ` · ${leg.layovers.join(", ")}`}
        </small>
      </div>
      <div className="leg-line__end" aria-hidden>
        <b className="tnum">
          {formatLocalTime(leg.arrival_time)}
          {plusDays > 0 && (
            <sup className="leg__plus" title={plusDaysTitle(plusDays)}>
              +{plusDays}
            </sup>
          )}
        </b>
        <span>{leg.arrival_airport}</span>
        <small>{formatDate(leg.arrival_time, showDate)}</small>
      </div>
    </div>
  );
}

/** One-line summary of a return leg ("19:05 NRT → 22:05 ICN · 3h · Direct"). */
function legSummary(leg: FlightLegOut): string {
  const plusDays = dayDiff(leg.departure_time, leg.arrival_time);
  return [
    `${formatDate(leg.departure_time, false)} ${formatLocalTime(leg.departure_time)} ${leg.departure_airport} → ${formatLocalTime(
      leg.arrival_time,
    )}${plusDays > 0 ? `+${plusDays}` : ""} ${leg.arrival_airport}`,
    formatDuration(leg.duration_minutes),
    formatStops(leg.stops),
  ].join(" · ");
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
  showDates,
  showMatch,
}: {
  offer: FlightOfferOut;
  trip: TripOut;
  best: boolean;
  showDates: boolean;
  showMatch: boolean;
}) {
  const names = useProviderNames();
  // The backend resolves the exact "booking options" page (both flights, dates and passengers
  // filled in) on click and redirects there; it falls back to the stored search link.
  const link = offer.booking_url ?? offer.source_url ? `/api/offers/${offer.id}/book` : null;
  const pax = formatAdults(trip.adults);
  const flights = offer.outbound.flight_numbers.join(" / ") || offer.flight_number || "";
  const roundTrip = trip.trip_type === "round_trip";
  const airline = offer.airline || offer.outbound.airlines.join(", ") || tr("Unknown airline", "항공사 정보 없음");
  const total = formatMoney(offer.price_total, offer.currency);
  const perPerson = formatMoney(offer.price_per_person, offer.currency);
  const historyLabel = tr("Price history of this flight", "이 항공편의 가격 추이");

  const returnLine = offer.inbound
    ? legSummary(offer.inbound)
    : offer.return_date
      ? tr(
          `${formatDate(offer.return_date)} · Cheapest return is picked when you click Book`,
          `${formatDate(offer.return_date)} · 예약하기를 누르면 가장 저렴한 오는 편이 선택돼요`,
        )
      : null;

  return (
    <li className={`flight-row${offer.matches_filters ? "" : " is-dim"}${best ? " is-best" : ""}`}>
      <div className="flight-row__airline">
        <span className={`airline-mark airline-mark--${airlineTint(airline)}`} aria-hidden>
          <Plane size={18} />
        </span>
        <div className="flight-row__airline-text">
          <b>{airline}</b>
          {flights && <span className="mono">{flights}</span>}
          <small>
            {names(offer.provider)}
            {offer.seller && ` · ${tr(`via ${offer.seller}`, `판매처 ${offer.seller}`)}`}
          </small>
        </div>
      </div>

      <div className="flight-row__legs">
        <LegCell leg={offer.outbound} showDate={showDates} />
        {roundTrip && returnLine && (
          <div className="leg-return">
            <span className="leg-return__tag">{tr("Return", "오는 편")}</span>
            <span>{returnLine}</span>
          </div>
        )}
      </div>

      <div className="flight-row__fare">
        <div className="flight-row__chips">
          {best && <Badge tone="good">{tr("Cheapest", "최저가")}</Badge>}
          {showMatch &&
            (offer.matches_filters ? (
              <Badge tone="info">{tr("Matches filters", "조건 일치")}</Badge>
            ) : (
              <Badge tone="outline">{tr("Outside filters", "조건 밖")}</Badge>
            ))}
        </div>
        <b className="fare-total tnum">{total}</b>
        <small>
          {roundTrip ? tr("Round trip", "왕복") : tr("One way", "편도")} · {tr(`total for ${pax}`, `${pax} 총액`)}
        </small>
        {trip.adults > 1 && <small>{tr(`${perPerson} per person`, `1인 ${perPerson}`)}</small>}
        {offer.previous_price !== null ? (
          <PriceDelta
            current={offer.price_per_person}
            previous={offer.previous_price}
            currency={offer.currency}
            context={tr("vs previous observation", "이전 조회 대비")}
          />
        ) : (
          <small className="fare-new">{tr("New", "신규")}</small>
        )}
      </div>

      <div className="flight-row__actions">
        {link ? (
          <a
            className="btn btn--primary"
            href={link}
            target="_blank"
            rel="noopener noreferrer"
            title={tr(
              `Opens the booking options for these flights with your dates and ${pax} filled in`,
              `날짜와 ${pax} 정보가 입력된 이 항공편의 예약 옵션 페이지를 열어요`,
            )}
          >
            {tr("Book", "예약하기")} <ExternalLink size={14} aria-hidden />
            <span className="sr-only">{tr("(opens in a new tab)", "(새 탭에서 열림)")}</span>
          </a>
        ) : (
          <span className="btn" aria-disabled="true">
            {tr("No link", "링크 없음")}
          </span>
        )}
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
      </div>

      <details className="flight-row__details">
        <summary>{tr("Fare & return details", "운임·오는 편 상세")}</summary>
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
            <dt>{tr("Prev / lowest", "이전 / 최저")}</dt>
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
          {offer.baggage && (
            <div>
              <dt>{tr("Baggage", "수하물")}</dt>
              <dd>{offer.baggage}</dd>
            </div>
          )}
          {roundTrip && (
            <div>
              <dt>{tr("Return", "오는 편")}</dt>
              <dd>
                {offer.inbound
                  ? `${offer.inbound.airlines.join(", ")} ${offer.inbound.flight_numbers.join(" / ")} · ${legSummary(offer.inbound)}`
                  : (returnLine ?? "—")}
              </dd>
            </div>
          )}
          <div>
            <dt>{tr("Source", "검색 소스")}</dt>
            <dd>
              {names(offer.provider)}
              {offer.seller && ` · ${tr(`via ${offer.seller}`, `판매처 ${offer.seller}`)}`} · {formatUtc(offer.searched_at)}
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
  const showDates =
    trip.flex_depart_days > 0 || trip.flex_return_days > 0 || new Set(list.map((o) => o.depart_date)).size > 1;
  const selectedRun = runId === "" ? finishedRuns[0] : finishedRuns.find((r) => r.id === runId);
  const n = list.length;

  return (
    <div className="stack">
      <div className="toolbar flights-toolbar">
        <label className="toolbar__item">
          <span className="xsmall subtle strong">{tr("Run", "검색")}</span>
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
          <span className="xsmall subtle strong" id="sort-label">
            {tr("Sort by", "정렬")}
          </span>
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
                {trip.adults > 1 &&
                  tr(
                    ` · Totals are for all ${trip.adults} travellers incl. taxes, exactly as the source shows them; per person = total ÷ ${trip.adults}`,
                    ` · 총액은 세금·수수료를 포함한 성인 ${trip.adults}명 전체 금액으로, 검색 소스에 표시된 금액 그대로예요. 1인 가격 = 총액 ÷ ${trip.adults}`,
                  )}
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
                showDates={showDates}
                showMatch={showAll}
              />
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}
