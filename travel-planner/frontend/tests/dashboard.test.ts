import { describe, test } from "node:test";
import assert from "node:assert/strict";
import {
  addDays,
  composerHint,
  composerQuery,
  dayPart,
  greeting,
  mostCommonOrigin,
  nextFriday,
  seoulHour,
  suggestions,
  targetLine,
  targetStatus,
  watchSummary,
  weekday,
} from "../src/components/dashboard/home";
import { inLang, makeTrip } from "./fixtures";

describe("greeting by Seoul time", () => {
  test("hours are read in Asia/Seoul, not the machine's zone", () => {
    // 2026-10-12T00:30Z is 09:30 in Seoul.
    assert.equal(seoulHour(Date.UTC(2026, 9, 12, 0, 30)), 9);
    assert.equal(seoulHour(Date.UTC(2026, 9, 12, 15, 0)), 0);
  });

  test("morning, afternoon and evening in both languages", () => {
    assert.equal(dayPart(5), "morning");
    assert.equal(dayPart(11), "morning");
    assert.equal(dayPart(12), "afternoon");
    assert.equal(dayPart(17), "afternoon");
    assert.equal(dayPart(18), "evening");
    assert.equal(dayPart(2), "evening");
    assert.equal(greeting("morning"), "좋은 아침이에요");
    assert.equal(greeting("afternoon"), "좋은 오후예요");
    assert.equal(greeting("evening"), "좋은 저녁이에요");
    assert.equal(inLang("en", () => greeting("afternoon")), "Good afternoon");
  });
});

describe("composer", () => {
  test("query uses the exact names the trip form reads", () => {
    const q = new URLSearchParams(
      composerQuery({ origin: "icn", destination: "TYO", depart: "2026-10-16", returnDate: "2026-10-18", adults: 2 }),
    );
    assert.deepEqual(Object.fromEntries(q), {
      origin: "ICN",
      destination: "TYO",
      depart: "2026-10-16",
      return: "2026-10-18",
      adults: "2",
      trip_type: "round_trip",
    });
  });

  test("an empty return date means one way; blank fields are left out", () => {
    const q = new URLSearchParams(
      composerQuery({ origin: "", destination: "FUK", depart: "2026-10-26", returnDate: "", adults: null }),
    );
    assert.deepEqual(Object.fromEntries(q), { destination: "FUK", depart: "2026-10-26", trip_type: "one_way" });
    assert.equal(composerQuery({ origin: "", destination: "", depart: "", returnDate: "", adults: null }), "");
  });

  test("hint names the trip type and length", () => {
    assert.equal(composerHint({ depart: "2026-11-02", returnDate: "2026-11-05" }), "왕복 · 3박 4일");
    assert.equal(inLang("en", () => composerHint({ depart: "2026-11-02", returnDate: "2026-11-03" })), "Round trip · 1 night");
    assert.match(composerHint({ depart: "2026-11-02", returnDate: "" }), /^편도/);
  });

  test("suggestion dates are relative to today", () => {
    assert.equal(weekday("2026-10-12"), 1, "2026-10-12 is a Monday");
    assert.equal(nextFriday("2026-10-12"), "2026-10-16");
    assert.equal(nextFriday("2026-10-16"), "2026-10-23", "on a Friday, the next weekend");
    assert.equal(nextFriday("2026-10-18"), "2026-10-23");
    assert.equal(addDays("2026-12-30", 3), "2027-01-02");

    const [tokyo, osaka, fukuoka] = suggestions("2026-10-12");
    assert.deepEqual(
      [tokyo.origin, tokyo.destination, tokyo.depart, tokyo.returnDate],
      ["ICN", "TYO", "2026-10-16", "2026-10-18"],
    );
    assert.equal(weekday(tokyo.returnDate), 0, "back on Sunday");
    assert.deepEqual([osaka.destination, osaka.depart, osaka.returnDate], ["OSA", "2026-11-02", "2026-11-05"]);
    assert.equal(fukuoka.destination, "FUK");
    assert.equal(tokyo.label, "도쿄 주말 여행");
    assert.equal(osaka.label, "오사카 3박 4일");
  });

  test("the composer starts from the most common origin", () => {
    assert.equal(mostCommonOrigin([]), null);
    assert.equal(mostCommonOrigin([{ origin: "ICN" }, { origin: "GMP" }, { origin: "GMP" }]), "GMP");
  });
});

describe("target price", () => {
  test("status says whether the fare reached the target and by how much", () => {
    assert.deepEqual(targetStatus(400000, 300000), { reached: false, diff: -100000 });
    assert.deepEqual(targetStatus(278000, 300000), { reached: true, diff: 22000 });
    assert.equal(targetStatus(null, 300000), null);
    assert.equal(targetStatus(278000, null), null);
  });

  test("one plain line of text, no progress bar", () => {
    assert.deepEqual(targetLine(313796, 300000, "KRW"), { text: "목표가 300,000원 · 13,796원 남음", reached: false });
    assert.deepEqual(targetLine(278000, 300000, "KRW"), { text: "목표가 300,000원보다 22,000원 낮아요", reached: true });
    assert.deepEqual(targetLine(300000, 300000, "KRW"), { text: "목표가 300,000원에 닿았어요", reached: true });
    assert.deepEqual(targetLine(null, 300000, "KRW"), { text: "목표가 300,000원", reached: false });
    assert.equal(targetLine(278000, null, "KRW"), null);
    assert.equal(inLang("en", () => targetLine(313796, 300000, "KRW")?.text), "Target ₩300,000 · ₩13,796 to go");
    assert.equal(inLang("en", () => targetLine(278000, 300000, "KRW")?.text), "₩22,000 below your ₩300,000 target");
  });
});

describe("watched-trips summary", () => {
  test("names the trip below target and by how much", () => {
    const summary = watchSummary([
      makeTrip({ id: 1, name: "도쿄 여행" }),
      makeTrip({ id: 2, name: "오사카", target_price: 250000, latest_min_price_basis_value: 280000 }),
    ]);
    assert.equal(summary?.tone, "good");
    assert.equal(summary?.text, "여행 2개를 지켜보고 있어요. ‘도쿄 여행’ 가격이 목표가보다 1인 기준 22,000원 낮아요.");
  });

  test("otherwise the trip closest to its target", () => {
    const trips = [
      makeTrip({ id: 1, name: "도쿄", target_price: 300000, latest_min_price_basis_value: 313796 }),
      makeTrip({ id: 2, name: "오사카", target_price: 200000, latest_min_price_basis_value: 280000 }),
    ];
    assert.equal(
      watchSummary(trips)?.text,
      "여행 2개를 지켜보고 있어요. 목표가에 가장 가까운 여행은 ‘도쿄’, 1인 기준 13,796원 남았어요.",
    );
    assert.equal(
      inLang("en", () => watchSummary(trips)?.text),
      "I'm watching 2 trips. Closest to its target is “도쿄”, ₩13,796 per person away.",
    );
  });

  test("mentions paused trips and failed checks", () => {
    const summary = watchSummary([
      makeTrip({ id: 1, target_price: 200000 }),
      makeTrip({ id: 2, last_status: "error", latest_min_price_basis_value: null, latest_min_price: null }),
      makeTrip({ id: 3, active: false }),
    ]);
    assert.equal(summary?.tone, "warn");
    assert.match(summary?.text ?? "", /^여행 3개 중 2개를 지켜보고 있고, 1개는 마지막 확인에 실패했어요\./);
  });

  test("a single trip, unchecked trips and all paused", () => {
    const fresh = makeTrip({ name: "제주", last_checked_at: null, latest_min_price: null, latest_min_price_basis_value: null });
    assert.equal(watchSummary([fresh])?.text, "‘제주’ 가격을 지켜보고 있어요. 첫 가격 확인이 끝나면 여기서 정리해 드릴게요.");
    assert.match(watchSummary([makeTrip({ active: false }), makeTrip({ id: 2, active: false })])?.text ?? "", /모두/);
    assert.equal(watchSummary([]), null);
  });
});
