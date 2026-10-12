import { test } from "node:test";
import assert from "node:assert/strict";
import { CABIN_LABEL, formatAdults, formatDuration, formatMoney, formatStops } from "../src/lib/format";
import { getLang, labels, tr } from "../src/lib/i18n";
import { inLang, makeTrip } from "./fixtures";
import { tripFacts } from "../src/lib/trip";

test("Korean is the default UI language", () => {
  assert.equal(getLang(), "ko");
  assert.equal(tr("Trips", "내 여행"), "내 여행");
  assert.equal(inLang("en", () => tr("Trips", "내 여행")), "Trips");
});

test("label maps follow the current language", () => {
  const map = labels({ a: ["Apple", "사과"] });
  assert.equal(map.a, "사과");
  assert.equal(inLang("en", () => map.a), "Apple");
  assert.equal(CABIN_LABEL.economy, "일반석");
  assert.equal(inLang("en", () => CABIN_LABEL.economy), "Economy");
});

test("KRW is written with 원 in Korean and ₩ in English", () => {
  assert.equal(formatMoney(295394, "KRW"), "295,394원");
  assert.equal(inLang("en", () => formatMoney(295394, "KRW")), "₩295,394");
  assert.equal(formatMoney(null, "KRW"), "—");
});

test("stops, adults and durations in both languages", () => {
  assert.equal(formatStops(0), "직항");
  assert.equal(formatStops(2), "경유 2회");
  assert.equal(inLang("en", () => formatStops(0)), "Direct");
  assert.equal(inLang("en", () => formatStops(1)), "1 stop");
  assert.equal(formatAdults(2), "성인 2명");
  assert.equal(inLang("en", () => formatAdults(1)), "1 adult");
  assert.equal(formatDuration(145), "2시간 25분");
  assert.equal(inLang("en", () => formatDuration(145)), "2h 25m");
});

test("trip facts line", () => {
  const trip = makeTrip({ adults: 2, cabin: "economy", direct_only: true });
  assert.match(tripFacts(trip), /성인 2명 · 일반석 · 직항만$/);
  assert.match(inLang("en", () => tripFacts(trip)), /2 adults · Economy · Direct only$/);
});
