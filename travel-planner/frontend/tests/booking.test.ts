import { test } from "node:test";
import assert from "node:assert/strict";
import type { BookingPlanOut, BookingTicketOut, FlightLegOut, FlightOfferOut } from "../src/api/types";
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
} from "../src/lib/booking";
import { inLang } from "./fixtures";

function leg(code: string, name: string, number: string, from: string, to: string, depart: string, arrive: string): FlightLegOut {
  return {
    departure_airport: from,
    arrival_airport: to,
    departure_time: depart,
    arrival_time: arrive,
    duration_minutes: 155,
    stops: 0,
    airlines: [name],
    flight_numbers: [number],
    segments: [
      {
        carrier_code: code,
        carrier_name: name,
        flight_number: number,
        departure_airport: from,
        arrival_airport: to,
        departure_time: depart,
        arrival_time: arrive,
        duration_minutes: 155,
        operating_carrier: null,
        aircraft: null,
      },
    ],
    layovers: [],
  };
}

const OUT = leg("MM", "Peach Aviation", "MM642", "ICN", "NRT", "2026-11-20T12:40", "2026-11-20T15:15");
const BACK = leg("RS", "Air Seoul", "RS260", "NRT", "ICN", "2026-11-27T12:50", "2026-11-27T15:35");

function offer(inbound: FlightLegOut | null, returnDate: string | null = "2026-11-27"): FlightOfferOut {
  return {
    id: 358,
    trip_id: 1,
    run_id: 4,
    searched_at: "2026-10-12T03:27:42Z",
    provider: "google_flights",
    seller: null,
    offer_key: "k",
    origin: "ICN",
    destination: "NRT",
    depart_date: "2026-11-20",
    return_date: returnDate,
    airline: "Peach Aviation",
    flight_number: "MM642",
    departure_airport: "ICN",
    arrival_airport: "NRT",
    departure_time: OUT.departure_time,
    arrival_time: OUT.arrival_time,
    duration_minutes: 155,
    stops: 0,
    outbound: OUT,
    inbound,
    cabin: "economy",
    fare_type: null,
    baggage: null,
    price_total: 627592,
    price_per_person: 313796,
    currency: "KRW",
    previous_price: null,
    lowest_price: null,
    booking_url: "https://www.google.com/travel/flights/booking?tfs=x",
    source_url: null,
    matches_filters: true,
  };
}

function ticket(overrides: Partial<BookingTicketOut>): BookingTicketOut {
  return {
    legs: ["outbound"],
    carrier_code: "MM",
    airline: "Peach Aviation",
    flight_numbers: ["MM642"],
    origin: "ICN",
    destination: "NRT",
    depart_date: "2026-11-20",
    return_date: null,
    departure_time: "2026-11-20T12:40",
    arrival_time: "2026-11-20T15:15",
    handoff: "prefilled",
    airline_url: "https://www.flypeach.com/kr/",
    ...overrides,
  };
}

const SEPARATE: BookingPlanOut = {
  offer: offer(BACK),
  booking_url: "https://www.google.com/travel/flights/booking?tfs=x",
  booking_page: "google_booking",
  adults: 2,
  round_trip: true,
  return_known: true,
  separate_tickets: true,
  tickets: [
    ticket({}),
    ticket({
      legs: ["inbound"],
      carrier_code: "RS",
      airline: "Air Seoul",
      flight_numbers: ["RS260"],
      origin: "NRT",
      destination: "ICN",
      depart_date: "2026-11-27",
      departure_time: "2026-11-27T12:50",
      arrival_time: "2026-11-27T15:35",
      handoff: "homepage",
      airline_url: "https://flyairseoul.com/",
    }),
  ],
  resolved_total: 627474,
};

const WHOLE_TICKET = ticket({ legs: ["outbound", "inbound"], return_date: "2026-11-27" });
const WHOLE: BookingPlanOut = {
  ...SEPARATE,
  offer: offer(leg("MM", "Peach Aviation", "MM641", "NRT", "ICN", "2026-11-27T16:00", "2026-11-27T18:40")),
  separate_tickets: false,
  tickets: [WHOLE_TICKET],
  resolved_total: null,
};

test("airline names are Korean for known carriers in the Korean UI only", () => {
  assert.equal(airlineName("MM", "Peach Aviation"), "피치항공");
  assert.equal(airlineName("rs", "Air Seoul"), "에어서울");
  assert.equal(inLang("en", () => airlineName("MM", "Peach Aviation")), "Peach Aviation");
  assert.equal(airlineName("TW", "Trinity Airways"), "트리니티항공");
  assert.equal(airlineName("XQ", "SunExpress"), "SunExpress");
  assert.equal(airlineName("XX", null), "XX");
  assert.equal(airlineName(null, null), "항공사");
  assert.deepEqual(legCarrier(BACK), { code: "RS", name: "에어서울" });
});

test("tickets: legs label and flights of a whole-trip ticket", () => {
  assert.equal(legsLabel(["outbound"]), "가는 편");
  assert.equal(legsLabel(["inbound"]), "오는 편");
  assert.equal(inLang("en", () => legsLabel(["outbound", "inbound"])), "Round trip");
  assert.equal(isWholeTrip(WHOLE_TICKET), true);
  assert.equal(isWholeTrip(SEPARATE.tickets[1]), false);
  assert.deepEqual(ticketFlights(WHOLE_TICKET, WHOLE), ["MM642", "MM641"]);
  assert.deepEqual(ticketFlights(SEPARATE.tickets[1], SEPARATE), ["RS260"]);
});

test("ticket details list what to type into the airline's search", () => {
  const rows = ticketDetails(SEPARATE.tickets[1], SEPARATE);
  assert.deepEqual(
    rows.map((r) => [r.key, r.label, r.value]),
    [
      ["route", "노선", "NRT → ICN"],
      ["date", "출발일", "11월 27일 (금) 12:50"],
      ["flights", "항공편", "RS260"],
      ["travelers", "승객", "성인 2명"],
      ["cabin", "좌석", "일반석"],
    ],
  );
  const text = detailsCopyText("에어서울 · 오는 편", rows);
  assert.equal(text.split("\n")[0], "에어서울 · 오는 편");
  assert.match(text, /출발일: 2026년 11월 27일 금요일 12:50/);

  const whole = inLang("en", () => ticketDetails(WHOLE_TICKET, WHOLE));
  assert.deepEqual(
    whole.map((r) => r.key),
    ["route", "date", "return", "flights", "travelers", "cabin"],
  );
  assert.equal(whole[0].value, "ICN → NRT (round trip)");
  assert.equal(whole[2].value, "Fri, Nov 27 16:00");
  assert.equal(whole[3].value, "MM642, MM641");
  assert.equal(whole[4].value, "2 adults");
});

test("a moved price is reported only when Google's total differs", () => {
  assert.equal(priceMoved(SEPARATE), true);
  assert.equal(priceMoved(WHOLE), false);
  assert.equal(priceMoved({ ...SEPARATE, resolved_total: 627592.4 }), false);
});

test("the summary sentence follows the ticket split, in both languages", () => {
  assert.equal(
    planSummary(SEPARATE),
    "피치항공 가는 편과 에어서울 오는 편, 항공권 2장을 차례로 예약해요. 총액은 2명 627,592원이에요.",
  );
  assert.equal(
    inLang("en", () => planSummary(SEPARATE)),
    "Two tickets, booked one after the other: Peach Aviation out and Air Seoul back. ₩627,592 in total for 2 travelers.",
  );
  assert.match(planSummary(WHOLE), /^피치항공 왕복 항공권 1장/);

  const unknown: BookingPlanOut = {
    ...SEPARATE,
    offer: offer(null),
    return_known: false,
    separate_tickets: null,
    tickets: [SEPARATE.tickets[0]],
  };
  assert.match(planSummary(unknown), /^가는 편은 피치항공 MM642, 오는 편은 Google 예약 페이지에서 골라요\./);

  const oneWay: BookingPlanOut = {
    ...unknown,
    offer: offer(null, null),
    round_trip: false,
    return_known: false,
    adults: 1,
  };
  assert.equal(inLang("en", () => planSummary(oneWay)), "One Peach Aviation one-way ticket. ₩627,592 in total for 1 traveler.");
});
