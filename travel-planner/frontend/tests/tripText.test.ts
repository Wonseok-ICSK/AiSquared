import { test } from "node:test";
import assert from "node:assert/strict";
import type { FlightLegOut, FlightOfferOut } from "../src/api/types";
import {
  historySummary,
  legCarriers,
  legRoute,
  offerLabel,
  sameAirline,
  separateTickets,
  timingText,
  tripSummary,
} from "../src/pages/trip/tripText";
import { inLang, makeTrip } from "./fixtures";

function leg(code: string, airline: string, number: string, from: string, to: string, dep: string, arr: string): FlightLegOut {
  return {
    departure_airport: from,
    arrival_airport: to,
    departure_time: dep,
    arrival_time: arr,
    duration_minutes: 155,
    stops: 0,
    airlines: [airline],
    flight_numbers: [number],
    segments: [
      {
        carrier_code: code,
        carrier_name: airline,
        flight_number: number,
        departure_airport: from,
        arrival_airport: to,
        departure_time: dep,
        arrival_time: arr,
        duration_minutes: 155,
        operating_carrier: null,
        aircraft: null,
      },
    ],
    layovers: [],
  };
}

const PEACH_OUT = leg("MM", "Peach Aviation", "MM642", "ICN", "NRT", "2026-11-20T12:40", "2026-11-20T15:15");
const AIR_SEOUL_BACK = leg("RS", "Air Seoul", "RS260", "NRT", "ICN", "2026-11-27T12:50", "2026-11-27T15:35");

function makeOffer(overrides: Partial<FlightOfferOut> = {}): FlightOfferOut {
  return {
    id: 358,
    trip_id: 1,
    run_id: 4,
    searched_at: "2026-10-12T03:27:42Z",
    provider: "google_flights",
    seller: null,
    offer_key: "ICN-NRT-2026-11-20T12:40-MM642|RT:2026-11-27|ECONOMY",
    origin: "ICN",
    destination: "NRT",
    depart_date: "2026-11-20",
    return_date: "2026-11-27",
    airline: "Peach Aviation",
    flight_number: "MM642",
    departure_airport: "ICN",
    arrival_airport: "NRT",
    departure_time: "2026-11-20T12:40",
    arrival_time: "2026-11-20T15:15",
    duration_minutes: 155,
    stops: 0,
    outbound: PEACH_OUT,
    inbound: AIR_SEOUL_BACK,
    cabin: "economy",
    fare_type: null,
    baggage: null,
    price_total: 627592,
    price_per_person: 313796,
    currency: "KRW",
    previous_price: 311194,
    lowest_price: 311194,
    booking_url: null,
    source_url: null,
    matches_filters: true,
    ...overrides,
  };
}

const NOW = Date.parse("2026-10-12T06:27:42Z"); // 3 h after the last check, 2 h before the next

test("different airlines each way are two tickets; unknown return is null", () => {
  assert.equal(separateTickets(makeOffer()), true);
  assert.equal(separateTickets(makeOffer({ inbound: null })), null);
  const sameAirline = leg("MM", "Peach Aviation", "MM641", "NRT", "ICN", "2026-11-27T10:00", "2026-11-27T12:40");
  assert.equal(separateTickets(makeOffer({ inbound: sameAirline })), false);
});

test("carrier codes fall back to flight-number prefixes", () => {
  const noCodes: FlightLegOut = { ...AIR_SEOUL_BACK, segments: [] };
  assert.deepEqual(legCarriers(noCodes), ["RS"]);
  assert.deepEqual(legCarriers(null), []);
});

test("offer label and return route read like the flight list", () => {
  inLang("ko", () => {
    assert.equal(offerLabel(makeOffer()), "피치항공 MM642");
    assert.equal(legRoute(AIR_SEOUL_BACK), "11월 27일 12:50 NRT → 15:35 ICN");
  });
  const redEye = leg("KE", "Korean Air", "KE5", "NRT", "ICN", "2026-11-27T23:30", "2026-11-28T02:00");
  inLang("en", () => assert.equal(legRoute(redEye), "Nov 27 23:30 NRT → 02:00+1 ICN"));
});

test("summary names the cheapest flight, the total and the distance to the target", () => {
  const trip = makeTrip({
    last_checked_at: "2026-10-12T03:27:42Z",
    next_run_at: "2026-10-12T08:30:02Z",
    latest_min_price: 627592,
    latest_min_price_basis_value: 313796,
  });
  inLang("ko", () => {
    const s = tripSummary(trip, makeOffer(), NOW);
    assert.equal(s.tone, "neutral");
    assert.equal(s.title, "지금 가장 싼 항공편은 피치항공 MM642, 성인 2명 왕복 627,592원이에요.");
    assert.equal(s.text, "1인 기준 목표가 300,000원까지 13,796원 남았어요.");
    assert.equal(s.timing, "3시간 전에 확인했고, 2시간 후에 다시 확인해요.");
  });
  inLang("en", () => {
    const s = tripSummary(trip, makeOffer(), NOW);
    assert.equal(s.title, "The cheapest matching flight is Peach Aviation MM642: ₩627,592 round trip for 2 adults.");
    assert.equal(s.text, "₩13,796 to go until your target of ₩300,000 per person.");
  });
});

test("at or below the target the note turns good", () => {
  const trip = makeTrip({ target_price: 320000, last_checked_at: "2026-10-12T03:27:42Z" });
  inLang("ko", () => {
    const s = tripSummary(trip, makeOffer(), NOW);
    assert.equal(s.tone, "good");
    assert.equal(s.text, "1인 기준 목표가 320,000원보다 6,204원 저렴해요.");
  });
});

test("no check yet, a failed check and no matches each say so plainly", () => {
  inLang("ko", () => {
    const fresh = makeTrip({ last_checked_at: null, latest_min_price: null, last_status: null, next_run_at: "2026-10-12T08:27:42Z" });
    const s = tripSummary(fresh, null, NOW);
    assert.equal(s.title, "아직 가격을 확인하지 않았어요.");
    assert.equal(s.text, "2시간 후에 처음 확인해요. 지금 확인을 누르면 바로 볼 수 있어요.");

    const failed = makeTrip({ latest_min_price: null, last_status: "error", last_checked_at: "2026-10-12T03:27:42Z" });
    const f = tripSummary(failed, null, NOW);
    assert.equal(f.tone, "warn");
    assert.equal(f.title, "마지막 가격 확인에 실패했어요.");

    const empty = makeTrip({ latest_min_price: null, last_checked_at: "2026-10-12T03:27:42Z" });
    assert.equal(tripSummary(empty, null, NOW).title, "아직 조건에 맞는 항공편이 없어요.");
  });
});

test("timing: paused, overdue and old checks", () => {
  inLang("ko", () => {
    const paused = makeTrip({ active: false, last_checked_at: "2026-10-12T03:27:42Z" });
    assert.equal(timingText(paused, NOW, false), "3시간 전에 확인했어요. 자동 확인은 일시 중지돼 있어요.");
    const overdue = makeTrip({ last_checked_at: "2026-10-12T03:27:42Z", next_run_at: "2026-10-12T06:00:00Z" });
    assert.equal(timingText(overdue, NOW, false), "3시간 전에 확인했어요. 곧 다시 확인해요.");
    const old = makeTrip({ last_checked_at: "2026-10-09T08:30:00Z", next_run_at: "2026-10-12T08:30:00Z" });
    assert.equal(timingText(old, NOW, false), "10월 9일 17:30에 확인했고, 2시간 후에 다시 확인해요.");
  });
  inLang("en", () => {
    const trip = makeTrip({ last_checked_at: "2026-10-12T03:27:42Z", next_run_at: "2026-10-12T08:27:42Z" });
    assert.equal(timingText(trip, NOW, false), "Checked 3 hr. ago; next check in 2 hr.");
  });
});

test("the return flight shows its own airline only when it differs from the outbound", () => {
  assert.equal(sameAirline(PEACH_OUT, AIR_SEOUL_BACK), false);
  const peachBack = leg("MM", "Peach Aviation", "MM641", "NRT", "ICN", "2026-11-27T10:00", "2026-11-27T12:40");
  assert.equal(sameAirline(PEACH_OUT, peachBack), true);
  const noCodes: FlightLegOut = { ...peachBack, segments: [], flight_numbers: [], airlines: ["Peach Aviation"] };
  const noCodesOut: FlightLegOut = { ...PEACH_OUT, segments: [], flight_numbers: [], airlines: ["Peach Aviation"] };
  assert.equal(sameAirline(noCodesOut, noCodes), true, "falls back to the airline names");
});

test("price history line: latest price vs target, and the move since the first check", () => {
  const trip = makeTrip();
  inLang("ko", () => {
    const s = historySummary(trip, [295394, 313796], "per_person", "KRW");
    assert.equal(s?.tone, "neutral");
    assert.equal(s?.text, "지금 1인 최저가는 313,796원, 목표가 300,000원까지 13,796원 남았어요.");
    assert.deepEqual(s?.change, { dir: "up", text: "첫 확인보다 6.2% 올랐어요" });

    const under = historySummary(trip, [313796, 289000], "per_person", "KRW");
    assert.equal(under?.tone, "good");
    assert.equal(under?.text, "지금 1인 최저가는 289,000원, 목표가 300,000원보다 11,000원 저렴해요.");
    assert.equal(under?.change?.dir, "down");

    const total = historySummary(makeTrip({ price_basis: "total", target_price: null }), [627592], "total", "KRW");
    assert.equal(total?.text, "지금 최저가는 총액 627,592원이에요. 목표 가격을 정하면 알려드려요.");
    assert.equal(total?.change, null, "one price has nothing to compare with");
  });
  inLang("en", () => {
    const s = historySummary(trip, [295394, null, 313796], "per_person", "KRW");
    assert.equal(s?.text, "Lowest now: ₩313,796 per person, ₩13,796 above your ₩300,000 target.");
    assert.equal(s?.change?.text, "6.2% higher than the first check");
    assert.equal(historySummary(trip, [300000], "per_person", "KRW")?.text, "Lowest now: ₩300,000 per person, exactly your target.");
  });
});

test("price history line when the newest check found nothing, or nothing ever", () => {
  const trip = makeTrip();
  inLang("ko", () => {
    const s = historySummary(trip, [310000, 300000, null], "per_person", "KRW");
    assert.equal(s?.text, "최근 확인에서는 조건에 맞는 항공편이 없었어요. 마지막 최저가는 300,000원이에요.");
    assert.equal(s?.change?.text, "첫 확인보다 3.2% 내렸어요");
    assert.equal(historySummary(trip, [300000, 300000], "per_person", "KRW")?.change?.text, "첫 확인과 같아요");
  });
  assert.equal(historySummary(trip, [null, null], "per_person", "KRW"), null);
  assert.equal(historySummary(trip, [], "per_person", "KRW"), null);
});
