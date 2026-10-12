/**
 * Booking step "/trips/:tripId/book/:offerId": what booking an offer involves, in three steps
 * (check the flights, choose where to buy, book each ticket) beside a ticket-like summary panel.
 * Data: GET /api/offers/{id}/booking-plan, which may take ~45 s the first time while the
 * backend looks up the return flight Google pairs with the fare. The app's guidance ends at
 * the seller's site (step 3 closes with that line); payment happens there.
 */

import { useEffect, useId, useState } from "react";
import type { ReactNode } from "react";
import { Link, useParams } from "react-router-dom";
import {
  ArrowLeft,
  ArrowLeftRight,
  ArrowRight,
  Building2,
  Check,
  Copy,
  ExternalLink,
  Globe,
  Plane,
  RefreshCw,
  Ticket,
} from "lucide-react";
import { ApiError, errorMessage, isAbort } from "../api/client";
import { api } from "../api/endpoints";
import type { BookingLeg, BookingPlanOut, BookingTicketOut, FlightLegOut } from "../api/types";
import { AssistantNote, BrandMark } from "../components/BrandMark";
import { Place } from "../components/Place";
import { Badge, Banner, Button, Skeleton, SkeletonBlock } from "../components/ui";
import { cityName, useAirport } from "../lib/airports";
import {
  airlineName,
  detailsCopyText,
  isWholeTrip,
  legCarrier,
  legsLabel,
  planSummary,
  priceMoved,
  ticketDetails,
  ticketFlights,
} from "../lib/booking";
import type { DetailRow } from "../lib/booking";
import {
  CABIN_LABEL,
  dayDiff,
  formatAdults,
  formatDate,
  formatDateRange,
  formatDuration,
  formatLocalTime,
  formatMoney,
  formatStops,
  formatUtc,
} from "../lib/format";
import { useAsync, useNow, useTitle } from "../lib/hooks";
import { tr } from "../lib/i18n";

/** The page gives up waiting after this long (the backend itself stops at ~45 s). */
const PLAN_TIMEOUT_MS = 90_000;
/** After this long the waiting note says it is taking longer than usual. */
const SLOW_AFTER_S = 45;

type PlanState =
  | { status: "loading"; startedAt: number }
  | { status: "ready"; plan: BookingPlanOut }
  | { status: "error"; error: unknown; timedOut: boolean };

function parseId(value: string | undefined): number | null {
  return value && /^\d+$/.test(value) ? Number(value) : null;
}

/** Copy text with the async clipboard API, falling back to a hidden textarea. */
async function copyText(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    /* fall through to the legacy path */
  }
  try {
    const area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.style.position = "fixed";
    area.style.top = "0";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    const ok = document.execCommand("copy");
    area.remove();
    return ok;
  } catch {
    return false;
  }
}

function newTabHint(): string {
  return tr("(opens in a new tab)", "(새 탭에서 열림)");
}

// ----------------------------------------------------------------------------- page

export function BookingPrep() {
  const params = useParams();
  const tripId = parseId(params.tripId);
  const offerId = parseId(params.offerId);
  useTitle(tr("Plan your booking", "예약 준비"));

  const trip = useAsync(() => (tripId === null ? Promise.resolve(null) : api.getTrip(tripId)), [tripId]);
  const [state, setState] = useState<PlanState>(() => ({ status: "loading", startedAt: Date.now() }));
  const [attempt, setAttempt] = useState(0);
  const [opened, setOpened] = useState(false);

  useEffect(() => {
    if (offerId === null) return;
    const controller = new AbortController();
    let alive = true;
    let timedOut = false;
    const timer = window.setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, PLAN_TIMEOUT_MS);
    setState({ status: "loading", startedAt: Date.now() });
    api
      .bookingPlan(offerId, controller.signal)
      .then((plan) => {
        if (alive) setState({ status: "ready", plan });
      })
      .catch((error: unknown) => {
        if (!alive || (isAbort(error) && !timedOut)) return;
        setState({ status: "error", error, timedOut });
      })
      .finally(() => window.clearTimeout(timer));
    return () => {
      alive = false;
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [offerId, attempt]);

  const backTo = tripId === null ? "/" : `/trips/${tripId}`;
  const back = (
    <Link className="breadcrumb" to={backTo}>
      <ArrowLeft size={14} aria-hidden /> {trip.data?.name ?? tr("Back to the trip", "여행으로 돌아가기")}
    </Link>
  );

  if (tripId === null || offerId === null) {
    return (
      <div className="bk">
        {back}
        <header className="bk-head">
          <h1>{tr("Plan your booking", "예약 준비")}</h1>
        </header>
        <Banner tone="error" title={tr("This booking link is not valid", "예약 링크가 올바르지 않아요")}>
          {tr("Open the trip and choose a flight again.", "여행 페이지에서 항공편을 다시 골라 주세요.")}
        </Banner>
      </div>
    );
  }

  const plan = state.status === "ready" ? state.plan : null;
  const t = trip.data;
  const head = plan ? (
    <RouteLine
      origin={plan.offer.origin}
      destination={plan.offer.destination}
      roundTrip={plan.round_trip}
      departDate={plan.offer.depart_date}
      returnDate={plan.offer.return_date}
      adults={plan.adults}
    />
  ) : t ? (
    <RouteLine
      origin={t.origin}
      destination={t.destination}
      roundTrip={t.trip_type === "round_trip"}
      departDate={t.depart_date}
      returnDate={t.return_date}
      adults={t.adults}
    />
  ) : null;

  return (
    <div className="bk">
      {back}
      <header className="bk-head">
        <h1>{tr("Plan your booking", "예약 준비")}</h1>
        {head}
      </header>

      {state.status === "loading" && <LoadingView startedAt={state.startedAt} />}
      {state.status === "error" && (
        <PlanError
          error={state.error}
          timedOut={state.timedOut}
          offerId={offerId}
          backTo={backTo}
          onRetry={() => setAttempt((n) => n + 1)}
        />
      )}
      {plan && <PlanView plan={plan} opened={opened} onOpen={() => setOpened(true)} />}
    </div>
  );
}

function RouteLine({
  origin,
  destination,
  roundTrip,
  departDate,
  returnDate,
  adults,
}: {
  origin: string;
  destination: string;
  roundTrip: boolean;
  departDate: string;
  returnDate: string | null;
  adults: number;
}) {
  const Arrow = roundTrip ? ArrowLeftRight : ArrowRight;
  return (
    <p className="bk-head__route">
      <span className="bk-head__places">
        <Place code={origin} />
        <Arrow size={14} className="bk-head__arrow" aria-label={roundTrip ? tr("round trip to", "왕복") : tr("to", "편도")} />
        <Place code={destination} />
      </span>
      <span aria-hidden className="bk-head__sep">
        ·
      </span>
      <span className="nowrap">{formatDateRange(departDate, roundTrip ? returnDate : null)}</span>
      <span aria-hidden className="bk-head__sep">
        ·
      </span>
      <span className="nowrap">{formatAdults(adults)}</span>
    </p>
  );
}

// ----------------------------------------------------------------------------- loading & errors

function LoadingView({ startedAt }: { startedAt: number }) {
  return (
    <>
      <WaitingNote startedAt={startedAt} />
      <div className="bk-layout" aria-hidden>
        <div className="bk-skel-steps">
          {[0, 1, 2].map((i) => (
            <div key={i} className="bk-skel-step">
              <Skeleton width={32} height={32} radius={16} />
              <div className="card bk-skel-card">
                <Skeleton width="40%" height={18} />
                <Skeleton width="85%" height={12} />
                <Skeleton width="70%" height={12} />
              </div>
            </div>
          ))}
        </div>
        <div className="card bk-skel-card bk-skel-card--aside">
          <SkeletonBlock lines={6} />
        </div>
      </div>
    </>
  );
}

function WaitingNote({ startedAt }: { startedAt: number }) {
  const now = useNow(1000);
  const seconds = Math.max(0, Math.floor((now - startedAt) / 1000));
  const slow = seconds >= SLOW_AFTER_S;
  return (
    <AssistantNote className="bk-wait" title={tr("Finding the return flight", "돌아오는 항공편을 찾고 있어요")}>
      <p>
        {tr(
          "Checking which return flight Google pairs with this fare, so both tickets are clear before you book. The first time can take about 45 seconds.",
          "이 가격에 Google이 짝지은 오는 편을 확인하고 있어요. 예약할 항공권을 정확히 보여 드리려고요. 처음 한 번은 45초쯤 걸릴 수 있어요.",
        )}
      </p>
      <div className="bk-wait__status">
        <span className="bk-wait__track" aria-hidden>
          <Plane size={14} />
        </span>
        <span className="bk-wait__time tnum" aria-hidden>
          {tr(`${seconds} s`, `${seconds}초`)}
        </span>
        <span role="status" className={slow ? "bk-wait__slow" : "sr-only"}>
          {slow
            ? tr("Taking a little longer than usual.", "평소보다 조금 더 걸리고 있어요.")
            : tr("Looking up the return flight", "오는 편을 찾는 중")}
        </span>
      </div>
    </AssistantNote>
  );
}

function PlanError({
  error,
  timedOut,
  offerId,
  backTo,
  onRetry,
}: {
  error: unknown;
  timedOut: boolean;
  offerId: number;
  backTo: string;
  onRetry: () => void;
}) {
  const missing = error instanceof ApiError && error.status === 404;
  if (missing) {
    return (
      <Banner tone="error" title={tr("This flight is no longer here", "이 항공편을 찾을 수 없어요")}>
        {tr(
          "It may have been replaced by a newer price check. Choose a flight again on the trip page.",
          "새 가격 확인으로 목록이 바뀌었을 수 있어요. 여행 페이지에서 항공편을 다시 골라 주세요.",
        )}{" "}
        <Link to={backTo}>{tr("Back to the trip", "여행으로 돌아가기")}</Link>
      </Banner>
    );
  }
  return (
    <Banner
      tone="error"
      title={
        timedOut
          ? tr("Finding the return flight is taking too long", "오는 편을 찾는 데 시간이 너무 오래 걸려요")
          : tr("Could not prepare the booking steps", "예약 안내를 준비하지 못했어요")
      }
      action={
        <Button size="sm" icon={<RefreshCw size={14} aria-hidden />} onClick={onRetry}>
          {tr("Try again", "다시 시도")}
        </Button>
      }
    >
      {!timedOut && <span className="bk-error__line">{errorMessage(error)}</span>}
      <span className="bk-error__line">
        {tr("You can still open the booking page on Google directly:", "Google 예약 페이지는 바로 열 수 있어요.")}{" "}
        <a href={`/api/offers/${offerId}/book`} target="_blank" rel="noopener noreferrer">
          {tr("Open on Google", "Google에서 바로 열기")}
          <span className="sr-only"> {newTabHint()}</span>
        </a>
      </span>
    </Banner>
  );
}

// ----------------------------------------------------------------------------- plan

function PlanView({ plan, opened, onOpen }: { plan: BookingPlanOut; opened: boolean; onOpen: () => void }) {
  return (
    <>
      <AssistantNote className="bk-summary-note">{planSummary(plan)}</AssistantNote>
      <div className="bk-layout">
        <ol className="bk-steps">
          <Step n={1} title={tr("Check the flights", "항공편 확인")}>
            <FlightsStep plan={plan} />
          </Step>
          <Step n={2} title={tr("Choose where to buy", "판매처 고르기")} done={opened}>
            <SellerStep plan={plan} onOpen={onOpen} />
          </Step>
          <Step n={3} title={tr("Book each ticket", "항공권 예약")}>
            <TicketsStep plan={plan} />
          </Step>
        </ol>
        <SummaryPanel plan={plan} onOpen={onOpen} />
      </div>
    </>
  );
}

function Step({ n, title, done = false, children }: { n: number; title: string; done?: boolean; children: ReactNode }) {
  const headingId = useId();
  return (
    <li className={`bk-step${done ? " is-done" : ""}`} aria-labelledby={headingId}>
      <span className="bk-step__marker" aria-hidden>
        {done ? <Check size={15} strokeWidth={2.5} /> : n}
      </span>
      <div className="bk-step__body">
        <h2 id={headingId} className="bk-step__title">
          <span className="sr-only">{tr(`Step ${n}: `, `${n}단계: `)}</span>
          {title}
          {done && <span className="sr-only"> {tr("(done)", "(완료)")}</span>}
        </h2>
        {children}
      </div>
    </li>
  );
}

// ----------------------------------------------------------------------------- step 1: flights

function FlightsStep({ plan }: { plan: BookingPlanOut }) {
  const { offer } = plan;
  const separate = plan.return_known && plan.tickets.length >= 2;
  return (
    <div className="bk-stack">
      <p className="bk-step__lead">
        {!plan.round_trip
          ? tr("This is the flight this price is for.", "이 가격에 포함된 항공편이에요.")
          : plan.return_known
            ? tr("These are the two flights this price is for.", "이 가격에 포함된 두 항공편이에요.")
            : tr(
                "The outbound flight is set; the return flight that goes with it could not be looked up yet.",
                "가는 편은 정해져 있고, 짝이 되는 오는 편은 아직 확인하지 못했어요.",
              )}
      </p>
      <FlightCard leg={offer.outbound} direction="outbound" />
      {plan.round_trip &&
        (offer.inbound ? (
          <FlightCard leg={offer.inbound} direction="inbound" />
        ) : (
          <PendingReturnCard returnDate={offer.return_date} />
        ))}
      {separate && <SeparateTicketsNote plan={plan} />}
      {plan.round_trip && plan.return_known && !separate && (
        <p className="bk-note">
          <Ticket size={16} aria-hidden />
          <span>
            {tr(
              "Both flights are on one ticket, so there is a single booking.",
              "두 항공편이 항공권 1장에 함께 들어 있어서 한 번만 예약하면 돼요.",
            )}
          </span>
        </p>
      )}
    </div>
  );
}

function FlightCard({ leg, direction }: { leg: FlightLegOut; direction: BookingLeg }) {
  const titleId = useId();
  const carrier = legCarrier(leg);
  const plusDays = dayDiff(leg.departure_time, leg.arrival_time);
  return (
    <article className="bk-flight" aria-labelledby={titleId}>
      <header className="bk-flight__head">
        <span className="bk-mark" aria-hidden>
          {carrier.code ?? <Plane size={14} />}
        </span>
        <div className="bk-flight__who">
          <h3 id={titleId} className="bk-flight__title">
            {direction === "outbound" ? tr("Outbound", "가는 편") : tr("Return", "오는 편")}
            <span className="bk-flight__date"> · {formatDate(leg.departure_time)}</span>
          </h3>
          <span className="bk-flight__airline">
            {carrier.name} <span className="mono">{leg.flight_numbers.join(" · ")}</span>
          </span>
        </div>
        <Badge tone="outline">{formatStops(leg.stops)}</Badge>
      </header>
      <div className="bk-flight__line">
        <AirportEnd time={leg.departure_time} code={leg.departure_airport} />
        <div className="bk-flight__mid">
          <span className="bk-flight__dur">
            <span className="sr-only">{tr("Flight time", "비행 시간")} </span>
            {formatDuration(leg.duration_minutes)}
          </span>
          <span className="bk-flight__track" aria-hidden>
            <Plane size={13} />
          </span>
          {leg.layovers.length > 0 && <span className="bk-flight__via">{leg.layovers.join(", ")}</span>}
        </div>
        <AirportEnd time={leg.arrival_time} code={leg.arrival_airport} plusDays={plusDays} arrival />
      </div>
    </article>
  );
}

function AirportEnd({
  time,
  code,
  plusDays = 0,
  arrival = false,
}: {
  time: string;
  code: string;
  plusDays?: number;
  arrival?: boolean;
}) {
  const airport = useAirport(code);
  const city = cityName(airport);
  return (
    <div className={`bk-end${arrival ? " bk-end--arrive" : ""}`}>
      <span className="sr-only">{arrival ? tr("Arrives", "도착") : tr("Departs", "출발")} </span>
      <b className="bk-end__time tnum">
        {formatLocalTime(time)}
        {plusDays > 0 && (
          <sup title={tr(`Arrives ${plusDays} day(s) later`, `${plusDays}일 뒤 도착`)}>+{plusDays}</sup>
        )}
      </b>
      <span className="bk-end__place" title={airport?.name}>
        <span className="mono">{code}</span>
        {city && <span className="bk-end__city"> {city}</span>}
      </span>
    </div>
  );
}

function PendingReturnCard({ returnDate }: { returnDate: string | null }) {
  return (
    <div className="bk-flight bk-flight--pending">
      <header className="bk-flight__head">
        <span className="bk-mark bk-mark--pending" aria-hidden>
          <Plane size={14} />
        </span>
        <div className="bk-flight__who">
          <h3 className="bk-flight__title">
            {tr("Return", "오는 편")}
            {returnDate && <span className="bk-flight__date"> · {formatDate(returnDate)}</span>}
          </h3>
          <span className="bk-flight__airline">
            {tr(
              "The return flight is chosen on Google's page. This price is the round-trip total.",
              "오는 편은 Google 예약 페이지에서 골라요. 이 가격은 왕복 총액이에요.",
            )}
          </span>
        </div>
      </header>
    </div>
  );
}

function SeparateTicketsNote({ plan }: { plan: BookingPlanOut }) {
  const [first, second] = plan.tickets;
  const a = airlineName(first.carrier_code, first.airline);
  const b = airlineName(second.carrier_code, second.airline);
  return (
    <div className="bk-callout">
      <Ticket size={18} aria-hidden className="bk-callout__icon" />
      <div className="bk-callout__body">
        <p className="bk-callout__title">{tr("This fare is two separate tickets", "이 가격은 항공권 2장이에요")}</p>
        <p>
          {tr(
            `${a} out and ${b} back are booked one after the other — or both together through a travel agency on Google's list.`,
            `가는 편은 ${a}, 오는 편은 ${b} 항공권이라 한 장씩 차례로 예약해요. Google 목록의 여행사를 고르면 두 장을 한 번에 예약할 수도 있어요.`,
          )}
        </p>
      </div>
    </div>
  );
}

// ----------------------------------------------------------------------------- step 2: seller

function bookingLinkLabel(plan: BookingPlanOut): string {
  if (plan.booking_page === "google_booking") return tr("Open booking options on Google", "Google에서 예약 옵션 열기");
  if (plan.booking_page === "google_search") return tr("Find these flights on Google", "Google에서 항공편 찾기");
  return tr("Open the seller's site", "판매처 사이트 열기");
}

/** The primary call to action (Google's booking options in a new tab); null without a link. */
function BookingLink({ plan, onOpen, block = false }: { plan: BookingPlanOut; onOpen: () => void; block?: boolean }) {
  if (!plan.booking_url || plan.booking_page === "none") return null;
  return (
    <a
      className={`btn btn--primary btn--lg bk-cta${block ? " bk-cta--block" : ""}`}
      href={plan.booking_url}
      target="_blank"
      rel="noopener noreferrer"
      onClick={onOpen}
    >
      <ExternalLink size={16} aria-hidden />
      {bookingLinkLabel(plan)}
      <span className="sr-only"> {newTabHint()}</span>
    </a>
  );
}

function SellerStep({ plan, onOpen }: { plan: BookingPlanOut; onOpen: () => void }) {
  const separate = plan.tickets.length >= 2;
  const flights = plan.tickets.flatMap((t) => ticketFlights(t, plan)).join(", ");
  const fromGoogle = plan.booking_page === "google_booking" || plan.booking_page === "google_search";
  let lead: string;
  switch (plan.booking_page) {
    case "google_booking":
      lead = tr(
        "Google's booking options page already has the flights, dates and travelers filled in. It lists the airline and travel agencies with their prices; pick one and press “Continue”.",
        "Google 예약 옵션 페이지에 항공편, 날짜, 승객 수가 미리 들어가 있어요. 항공사와 여행사가 가격과 함께 나오니 하나를 골라 ‘계속’을 누르세요.",
      );
      break;
    case "google_search":
      lead = tr(
        `This opens Google Flights search results. Pick the same flights (${flights}) to reach the booking options.`,
        `Google 항공권 검색 결과가 열려요. 같은 항공편(${flights})을 골라 예약 옵션으로 들어가세요.`,
      );
      break;
    case "other":
      lead = tr(
        "This opens the seller's site. Check that the flights match the ones above.",
        "판매처 사이트가 열려요. 위 항공편과 같은지 확인하세요.",
      );
      break;
    default:
      lead = tr(
        "There is no booking link for this fare. Search the airline's site with the details in the next step.",
        "이 항공권에는 예약 링크가 없어요. 다음 단계의 정보로 항공사 사이트에서 검색하세요.",
      );
  }
  return (
    <div className="bk-stack">
      <p className="bk-step__lead">{lead}</p>
      {fromGoogle && (
        <ul className="bk-sellers">
          <li className="bk-seller">
            <span className="bk-seller__icon" aria-hidden>
              <Building2 size={17} />
            </span>
            <div>
              <p className="bk-seller__title">{tr("The airline itself", "항공사에서 직접")}</p>
              <p className="bk-seller__text">
                {separate
                  ? tr(
                      "Each airline sells its own ticket, so you book twice. Changes and refunds go straight to the airline.",
                      "항공사마다 자기 항공권을 팔아서 두 번 예약해요. 변경이나 환불은 항공사에 바로 요청할 수 있어요.",
                    )
                  : tr(
                      "Book on the airline's site. Changes and refunds go straight to the airline.",
                      "항공사 사이트에서 예약해요. 변경이나 환불은 항공사에 바로 요청할 수 있어요.",
                    )}
              </p>
            </div>
          </li>
          <li className="bk-seller">
            <span className="bk-seller__icon" aria-hidden>
              <Globe size={17} />
            </span>
            <div>
              <p className="bk-seller__title">
                {tr("A travel agency", "여행사")} <span className="bk-seller__eg">Agoda, Kiwi.com, Mytrip…</span>
              </p>
              <p className="bk-seller__text">
                {separate
                  ? tr(
                      "Books both tickets in one go and is sometimes cheaper. Check its change and refund rules before paying.",
                      "두 항공권을 한 번에 예약해 주고, 가끔 더 싸요. 결제 전에 변경·환불 규정을 꼭 확인하세요.",
                    )
                  : tr(
                      "Sometimes cheaper than the airline. Check its change and refund rules before paying.",
                      "항공사보다 싼 경우가 있어요. 결제 전에 변경·환불 규정을 꼭 확인하세요.",
                    )}
              </p>
            </div>
          </li>
        </ul>
      )}
      {plan.booking_url && plan.booking_page !== "none" && (
        <div className="bk-cta-row">
          <BookingLink plan={plan} onOpen={onOpen} />
          <span className="bk-cta-row__hint">{tr("Opens in a new tab.", "새 탭에서 열려요.")}</span>
        </div>
      )}
    </div>
  );
}

// ----------------------------------------------------------------------------- step 3: tickets

/** True when step 2 sends the traveller through Google's pages (and its "Continue"). */
function viaGoogle(plan: BookingPlanOut): boolean {
  return Boolean(plan.booking_url) && (plan.booking_page === "google_booking" || plan.booking_page === "google_search");
}

function TicketsStep({ plan }: { plan: BookingPlanOut }) {
  const separate = plan.tickets.length >= 2;
  return (
    <div className="bk-stack">
      <p className="bk-step__lead">
        {!viaGoogle(plan)
          ? plan.booking_page === "other" && plan.booking_url
            ? tr(
                "The seller's site from step 2 takes the booking. Use the details below to check you have the right flights.",
                "2단계에서 연 판매처 사이트에서 예약해요. 아래 정보로 항공편이 맞는지 확인하세요.",
              )
            : tr(
                "Search the airline's own site with the details below and book there.",
                "항공사 사이트에서 아래 정보로 검색해 예약하세요.",
              )
          : separate
          ? tr(
              "If you chose the airlines on Google, each ticket has its own “Continue”. Book them one at a time. With a travel agency, both flights appear together on its passenger-details page — book them there in one go.",
              "Google에서 항공사를 골랐다면 항공권마다 ‘계속’ 버튼이 따로 있어요. 한 장씩 예약하세요. 여행사를 골랐다면 승객 정보 화면에 두 항공편이 함께 나오니 거기서 한 번에 예약하면 돼요.",
            )
          : tr(
              "Press “Continue” on Google to move to the seller. With a travel agency, the passenger-details page opens directly — fill it in there.",
              "Google에서 ‘계속’을 누르면 판매처로 넘어가요. 여행사를 골랐다면 승객 정보 화면이 바로 열리니 그대로 입력하면 돼요.",
            )}
      </p>
      {plan.tickets.map((ticket, index) => (
        <TicketCard key={`${ticket.legs.join("-")}-${index}`} plan={plan} ticket={ticket} index={index} count={plan.tickets.length} />
      ))}
      {plan.round_trip && !plan.return_known && (
        <p className="bk-note">
          <Plane size={16} aria-hidden />
          <span>
            {tr(
              "The return ticket depends on the return flight you pick on Google's page.",
              "오는 편 항공권은 Google 페이지에서 오는 편을 고른 뒤에 정해져요.",
            )}
          </span>
        </p>
      )}
      <div className="bk-closing">
        <BrandMark size={20} className="bk-closing__mark" />
        <p className="bk-closing__line">
          {tr(
            "That's where this app's guidance ends. Payment happens on the seller's site.",
            "앱의 안내는 여기까지예요. 결제는 판매처 사이트에서 진행돼요.",
          )}
        </p>
      </div>
    </div>
  );
}

function handoffBadge(ticket: BookingTicketOut): ReactNode {
  switch (ticket.handoff) {
    case "prefilled":
      return <Badge tone="good">{tr("Details filled in", "정보 자동 입력")}</Badge>;
    case "homepage":
      return <Badge tone="warn">{tr("Search by hand", "직접 검색")}</Badge>;
    default:
      return <Badge tone="outline">{tr("Not checked yet", "확인 전")}</Badge>;
  }
}

function TicketCard({
  plan,
  ticket,
  index,
  count,
}: {
  plan: BookingPlanOut;
  ticket: BookingTicketOut;
  index: number;
  count: number;
}) {
  const titleId = useId();
  const airline = airlineName(ticket.carrier_code, ticket.airline);
  const flights = ticketFlights(ticket, plan).join(", ");
  const rows = ticketDetails(ticket, plan);
  const heading = `${airline} · ${legsLabel(ticket.legs)}`;
  const n = plan.adults;
  const google = viaGoogle(plan);
  let text: string;
  switch (google ? ticket.handoff : "direct") {
    case "direct":
      text = tr(
        `Open ${airline}'s site and search with the details below.`,
        `${airline} 사이트를 열고 아래 정보로 검색하세요.`,
      );
      break;
    case "prefilled":
      text = tr(
        `After “Continue” on Google, ${airline}'s site opens with the route, date and ${n === 1 ? "1 traveler" : `${n} travelers`} filled in. Pick flight ${flights}.`,
        `Google에서 ‘계속’을 누르면 ${airline} 사이트가 열리고 노선, 날짜, 승객 ${n}명이 입력돼 있어요. ${flights} 항공편을 고르세요.`,
      );
      break;
    case "homepage":
      text = tr(
        `${airline} opens on its home page. Search again there with the details below.`,
        `Google에서 ‘계속’을 누르면 ${airline} 홈페이지가 열려요. 아래 정보로 직접 검색하세요.`,
      );
      break;
    default:
      text = tr(
        `After “Continue” on Google you move to ${airline}'s site. What opens there varies; if you need to search, use the details below.`,
        `Google에서 ‘계속’을 누르면 ${airline} 사이트로 넘어가요. 열리는 화면은 판매처마다 달라서, 검색이 필요하면 아래 정보를 쓰세요.`,
      );
  }
  const details = <TicketDetails heading={heading} rows={rows} airline={airline} airlineUrl={ticket.airline_url} />;
  return (
    <article className="bk-ticket" aria-labelledby={titleId}>
      <header className="bk-ticket__head">
        <span className="bk-mark" aria-hidden>
          {ticket.carrier_code ?? <Ticket size={14} />}
        </span>
        <div className="bk-ticket__who">
          {count > 1 && <span className="bk-ticket__no">{tr(`Ticket ${index + 1} of ${count}`, `항공권 ${index + 1}/${count}`)}</span>}
          <h3 id={titleId} className="bk-ticket__title">
            {airline}
            <span className="bk-ticket__legs"> · {legsLabel(ticket.legs)}</span>
          </h3>
          <span className="bk-ticket__meta">
            <span className="mono">{flights}</span> · {ticket.origin} → {ticket.destination}
            {isWholeTrip(ticket) && ` → ${ticket.origin}`} · {formatDate(ticket.depart_date)}
          </span>
        </div>
        {google && handoffBadge(ticket)}
      </header>
      <p className="bk-ticket__text">{text}</p>
      {google && ticket.handoff === "prefilled" ? (
        <details className="bk-disclosure">
          <summary>{tr("Details to double-check", "확인용 예약 정보")}</summary>
          {details}
        </details>
      ) : (
        details
      )}
    </article>
  );
}

function TicketDetails({
  heading,
  rows,
  airline,
  airlineUrl,
}: {
  heading: string;
  rows: DetailRow[];
  airline: string;
  airlineUrl: string | null;
}) {
  return (
    <div className="bk-details">
      <dl className="bk-details__list">
        {rows.map((row) => (
          <div key={row.key} className="bk-details__row">
            <dt>{row.label}</dt>
            <dd>{row.value}</dd>
          </div>
        ))}
      </dl>
      <div className="bk-details__actions">
        <CopyButton text={detailsCopyText(heading, rows)} what={heading} />
        {airlineUrl && (
          <a className="btn btn--sm" href={airlineUrl} target="_blank" rel="noopener noreferrer">
            <ExternalLink size={14} aria-hidden />
            {tr(`${airline} website`, `${airline} 홈페이지`)}
            <span className="sr-only"> {newTabHint()}</span>
          </a>
        )}
      </div>
    </div>
  );
}

function CopyButton({ text, what }: { text: string; what: string }) {
  const [result, setResult] = useState<"idle" | "done" | "failed">("idle");
  useEffect(() => {
    if (result === "idle") return;
    const id = window.setTimeout(() => setResult("idle"), 2500);
    return () => window.clearTimeout(id);
  }, [result]);
  return (
    <>
      <Button
        size="sm"
        icon={result === "done" ? <Check size={14} aria-hidden /> : <Copy size={14} aria-hidden />}
        aria-label={tr(`Copy details: ${what}`, `${what} 정보 복사`)}
        onClick={() => void copyText(text).then((ok) => setResult(ok ? "done" : "failed"))}
      >
        {result === "done" ? tr("Copied", "복사했어요") : tr("Copy", "복사")}
      </Button>
      <span role="status" className={result === "failed" ? "bk-copy-failed" : "sr-only"}>
        {result === "done" && tr("Copied to the clipboard.", "클립보드에 복사했어요.")}
        {result === "failed" && tr("Could not copy. Select the text instead.", "복사하지 못했어요. 글자를 직접 선택해 주세요.")}
      </span>
    </>
  );
}

// ----------------------------------------------------------------------------- summary panel

function City({ code }: { code: string }) {
  const airport = useAirport(code);
  const city = cityName(airport);
  return (
    <span className="bk-sum__city">
      {city ?? code}
      {city && <span className="bk-sum__code"> {code}</span>}
    </span>
  );
}

function SummaryFlight({ leg, direction }: { leg: FlightLegOut; direction: BookingLeg }) {
  const carrier = legCarrier(leg);
  const plusDays = dayDiff(leg.departure_time, leg.arrival_time);
  return (
    <li className="bk-sum__flight">
      <span className="bk-sum__dir">{direction === "outbound" ? tr("Outbound", "가는 편") : tr("Return", "오는 편")}</span>
      <span className="bk-sum__fl">
        <span className="sr-only">
          {tr(
            `Departs ${leg.departure_airport} ${formatLocalTime(leg.departure_time)}, arrives ${leg.arrival_airport} ${formatLocalTime(leg.arrival_time)}${plusDays > 0 ? ` (+${plusDays})` : ""}.`,
            `${leg.departure_airport} ${formatLocalTime(leg.departure_time)} 출발, ${leg.arrival_airport} ${formatLocalTime(leg.arrival_time)} 도착${plusDays > 0 ? ` (+${plusDays}일)` : ""}.`,
          )}
        </span>
        <span className="bk-sum__times tnum" aria-hidden>
          {formatLocalTime(leg.departure_time)} {leg.departure_airport}
          <ArrowRight size={12} className="bk-sum__arrow" />
          {formatLocalTime(leg.arrival_time)}
          {plusDays > 0 && <sup>+{plusDays}</sup>} {leg.arrival_airport}
        </span>
        <span className="bk-sum__sub">
          {formatDate(leg.departure_time)} · {carrier.name} <span className="mono">{leg.flight_numbers.join(" · ")}</span>
        </span>
      </span>
    </li>
  );
}

function SummaryPanel({ plan, onOpen }: { plan: BookingPlanOut; onOpen: () => void }) {
  const titleId = useId();
  const { offer } = plan;
  const moved = priceMoved(plan);
  const ticketCount = plan.tickets.length;
  return (
    <aside className="bk-aside" aria-labelledby={titleId}>
      <div className="bk-sum">
        <div className="bk-sum__bar">
          <BrandMark size={16} className="bk-sum__mark" />
          <h2 id={titleId} className="bk-sum__title">
            {tr("Booking summary", "예약 요약")}
          </h2>
          <span className="bk-sum__kind">{plan.round_trip ? tr("Round trip", "왕복") : tr("One way", "편도")}</span>
        </div>

        <div className="bk-sum__section">
          <p className="bk-sum__route">
            <City code={offer.origin} />
            {plan.round_trip ? (
              <ArrowLeftRight size={16} className="bk-sum__route-arrow" aria-label={tr("round trip to", "왕복")} />
            ) : (
              <ArrowRight size={16} className="bk-sum__route-arrow" aria-label={tr("to", "편도")} />
            )}
            <City code={offer.destination} />
          </p>
          <p className="bk-sum__dates">{formatDateRange(offer.depart_date, plan.round_trip ? offer.return_date : null)}</p>
        </div>

        <div className="bk-sum__perf" aria-hidden />

        <div className="bk-sum__section">
          <ul className="bk-sum__flights">
            <SummaryFlight leg={offer.outbound} direction="outbound" />
            {plan.round_trip &&
              (offer.inbound ? (
                <SummaryFlight leg={offer.inbound} direction="inbound" />
              ) : (
                <li className="bk-sum__flight">
                  <span className="bk-sum__dir">{tr("Return", "오는 편")}</span>
                  <span className="bk-sum__fl">
                    <span className="bk-sum__sub">
                      {offer.return_date ? `${formatDate(offer.return_date)} · ` : ""}
                      {tr("chosen on Google's page", "Google 페이지에서 선택")}
                    </span>
                  </span>
                </li>
              ))}
          </ul>
          <dl className="bk-sum__facts">
            <div>
              <dt>{tr("Travelers", "승객")}</dt>
              <dd>{formatAdults(plan.adults)}</dd>
            </div>
            <div>
              <dt>{tr("Cabin", "좌석")}</dt>
              <dd>{CABIN_LABEL[offer.cabin]}</dd>
            </div>
            {plan.return_known || !plan.round_trip ? (
              <div>
                <dt>{tr("Tickets", "항공권")}</dt>
                <dd>{tr(ticketCount === 1 ? "1 booking" : `${ticketCount} bookings`, `${ticketCount}장`)}</dd>
              </div>
            ) : null}
          </dl>
        </div>

        <div className="bk-sum__perf" aria-hidden />

        <div className="bk-sum__section bk-sum__price">
          <span className="bk-sum__label">{tr("Total", "총액")}</span>
          <p className="bk-sum__total tnum">{formatMoney(offer.price_total, offer.currency)}</p>
          <p className="bk-sum__pp tnum">
            {tr(
              `${formatMoney(offer.price_per_person, offer.currency)} per person`,
              `1인 ${formatMoney(offer.price_per_person, offer.currency)}`,
            )}
          </p>
          <p className="bk-sum__checked">
            {tr(`Price checked ${formatUtc(offer.searched_at)} (Seoul time)`, `${formatUtc(offer.searched_at)} 확인한 가격 (서울 시간)`)}
          </p>
          {moved && plan.resolved_total != null && (
            <p className="bk-sum__moved">
              {tr(
                `Google showed ${formatMoney(plan.resolved_total, offer.currency)} when the return flight was found. The seller's page shows the final price.`,
                `오는 편을 찾을 때 Google에 표시된 총액은 ${formatMoney(plan.resolved_total, offer.currency)}이었어요. 최종 금액은 판매처 화면에서 확인하세요.`,
              )}
            </p>
          )}
        </div>

        {plan.booking_url && plan.booking_page !== "none" ? (
          <div className="bk-sum__cta">
            <BookingLink plan={plan} onOpen={onOpen} block />
            <span className="bk-sum__hint">{tr("Opens in a new tab.", "새 탭에서 열려요.")}</span>
          </div>
        ) : (
          <p className="bk-sum__cta bk-sum__hint">
            {tr("No booking link for this fare.", "이 항공권에는 예약 링크가 없어요.")}
          </p>
        )}
      </div>
    </aside>
  );
}
