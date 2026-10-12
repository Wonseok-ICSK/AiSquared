import { test } from "node:test";
import assert from "node:assert/strict";
import { describeRule, effectiveRuleBasis } from "../src/lib/alerts";
import { inLang, makeTrip } from "./fixtures";

const trip = makeTrip({ price_basis: "per_person", target_price: 300000 });

test("a below-target rule without threshold uses the trip target's basis (alerts._target)", () => {
  const rule = { kind: "below_target" as const, threshold: null, drop_percent: null, price_basis: "total" as const };
  assert.equal(effectiveRuleBasis(rule, trip), "per_person");
  assert.equal(describeRule(rule, trip), "가격 ≤ 300,000원 (여행 목표가), 1인");
  assert.equal(inLang("en", () => describeRule(rule, trip)), "Price ≤ ₩300,000 (trip target), per person");
});

test("rules with their own threshold, and other kinds, keep their basis", () => {
  const own = { kind: "below_target" as const, threshold: 500000, drop_percent: null, price_basis: "total" as const };
  assert.equal(effectiveRuleBasis(own, trip), "total");
  assert.equal(describeRule(own, trip), "가격 ≤ 500,000원, 총액");
  assert.equal(inLang("en", () => describeRule(own, trip)), "Price ≤ ₩500,000, total");
  const drop = { kind: "price_drop" as const, threshold: null, drop_percent: 10, price_basis: "total" as const };
  assert.equal(effectiveRuleBasis(drop, trip), "total");
  assert.equal(describeRule(drop, trip), "직전 확인보다 10% 이상 하락, 총액");
  assert.equal(inLang("en", () => describeRule(drop, trip)), "Price drops ≥ 10% vs the previous check, total");
});
