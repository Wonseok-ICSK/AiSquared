/**
 * Pure helpers of the booking step page (pages/BookingPrep.tsx): airline names, the ticket
 * details a traveller types into an airline's search form, and the page's one-line summary.
 * Everything reads the current UI language at call time (call from render).
 */

import type { BookingLeg, BookingPlanOut, BookingTicketOut, FlightLegOut } from "../api/types";
import { CABIN_LABEL, airlineName, formatAdults, formatDate, formatDateLong, formatLocalTime, formatMoney } from "./format";
import { tr } from "./i18n";

export { airlineName };

/** Carrier of a stored leg: first segment's code/name, else the flight number prefix. */
export function legCarrier(leg: FlightLegOut): { code: string | null; name: string } {
  const seg = leg.segments.find((s) => s.carrier_code || s.carrier_name);
  const code = seg?.carrier_code ?? (leg.flight_numbers[0] ? leg.flight_numbers[0].slice(0, 2).toUpperCase() : null);
  return { code, name: airlineName(code, seg?.carrier_name ?? leg.airlines[0] ?? null) };
}

/** True when one ticket covers both directions. */
export function isWholeTrip(ticket: Pick<BookingTicketOut, "legs">): boolean {
  return ticket.legs.includes("outbound") && ticket.legs.includes("inbound");
}

/** "가는 편" / "오는 편" / "왕복" for a ticket's legs. */
export function legsLabel(legs: readonly BookingLeg[]): string {
  if (legs.includes("outbound") && legs.includes("inbound")) return tr("Round trip", "왕복");
  return legs.includes("inbound") ? tr("Return", "오는 편") : tr("Outbound", "가는 편");
}

/** Flight numbers of a ticket; a whole-trip ticket adds the return flight's numbers. */
export function ticketFlights(ticket: BookingTicketOut, plan: Pick<BookingPlanOut, "offer">): string[] {
  const numbers = [...ticket.flight_numbers];
  const inbound = plan.offer.inbound;
  if (isWholeTrip(ticket) && inbound) {
    for (const n of inbound.flight_numbers) if (!numbers.includes(n)) numbers.push(n);
  }
  return numbers;
}

/** One row of the "search with these details" list (`copy` is the clipboard wording). */
export interface DetailRow {
  key: "route" | "date" | "return" | "flights" | "travelers" | "cabin";
  label: string;
  value: string;
  copy: string;
}

function dateTime(date: string, time: string | null | undefined, long: boolean): string {
  const day = long ? formatDateLong(date) : formatDate(date);
  const hhmm = time ? formatLocalTime(time) : "";
  return hhmm && hhmm !== "—" ? `${day} ${hhmm}` : day;
}

/** What to type into the airline's own search: route, date(s), flights, travelers, cabin. */
export function ticketDetails(ticket: BookingTicketOut, plan: Pick<BookingPlanOut, "offer" | "adults">): DetailRow[] {
  const whole = isWholeTrip(ticket);
  const route = `${ticket.origin} → ${ticket.destination}`;
  const routeValue = whole ? `${route} (${tr("round trip", "왕복")})` : route;
  const rows: DetailRow[] = [
    { key: "route", label: tr("Route", "노선"), value: routeValue, copy: routeValue },
    {
      key: "date",
      label: whole ? tr("Departs", "가는 날") : tr("Date", "출발일"),
      value: dateTime(ticket.depart_date, ticket.departure_time, false),
      copy: dateTime(ticket.depart_date, ticket.departure_time, true),
    },
  ];
  if (whole && ticket.return_date) {
    const backTime = plan.offer.inbound?.departure_time;
    rows.push({
      key: "return",
      label: tr("Returns", "오는 날"),
      value: dateTime(ticket.return_date, backTime, false),
      copy: dateTime(ticket.return_date, backTime, true),
    });
  }
  const flights = ticketFlights(ticket, plan).join(", ");
  if (flights) rows.push({ key: "flights", label: tr("Flight", "항공편"), value: flights, copy: flights });
  const travelers = formatAdults(plan.adults);
  rows.push({ key: "travelers", label: tr("Travelers", "승객"), value: travelers, copy: travelers });
  const cabin = CABIN_LABEL[plan.offer.cabin];
  if (cabin) rows.push({ key: "cabin", label: tr("Cabin", "좌석"), value: cabin, copy: cabin });
  return rows;
}

/** Plain text of a ticket's details for the clipboard ("노선: ICN → NRT" per line). */
export function detailsCopyText(heading: string, rows: readonly DetailRow[]): string {
  return [heading, ...rows.map((r) => `${r.label}: ${r.copy}`)].join("\n");
}

/** True when Google's total at the return-flight lookup differs from the stored price. */
export function priceMoved(plan: Pick<BookingPlanOut, "resolved_total" | "offer">): boolean {
  return plan.resolved_total != null && Math.abs(plan.resolved_total - plan.offer.price_total) >= 1;
}

/** "2명" / "2 travelers". */
export function travelersShort(n: number): string {
  return tr(n === 1 ? "1 traveler" : `${n} travelers`, `${n}명`);
}

/** The page's summary in the app's voice (one or two plain sentences). */
export function planSummary(plan: BookingPlanOut): string {
  const { offer } = plan;
  const total = formatMoney(offer.price_total, offer.currency);
  const people = travelersShort(plan.adults);
  const priceKo = `총액은 ${people} ${total}이에요.`;
  const priceEn = `${total} in total for ${people}.`;
  const out = legCarrier(offer.outbound);

  if (plan.round_trip && !plan.return_known) {
    const flight = offer.outbound.flight_numbers.join(", ");
    return tr(
      `Outbound is ${out.name} ${flight}; the return flight is chosen on Google's page. ${priceEn}`,
      `가는 편은 ${out.name} ${flight}, 오는 편은 Google 예약 페이지에서 골라요. ${priceKo}`,
    );
  }
  if (plan.tickets.length >= 2) {
    const [first, second] = plan.tickets;
    const a = airlineName(first.carrier_code, first.airline);
    const b = airlineName(second.carrier_code, second.airline);
    return tr(
      `Two tickets, booked one after the other: ${a} out and ${b} back. ${priceEn}`,
      `${a} 가는 편과 ${b} 오는 편, 항공권 2장을 차례로 예약해요. ${priceKo}`,
    );
  }
  const ticket = plan.tickets[0];
  const name = ticket ? airlineName(ticket.carrier_code, ticket.airline) : out.name;
  if (plan.round_trip) {
    return tr(
      `One ${name} round-trip ticket covers both flights. ${priceEn}`,
      `${name} 왕복 항공권 1장으로 두 항공편을 모두 예약해요. ${priceKo}`,
    );
  }
  return tr(`One ${name} one-way ticket. ${priceEn}`, `${name} 편도 항공권 1장을 예약해요. ${priceKo}`);
}
