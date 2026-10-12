import { test } from "node:test";
import assert from "node:assert/strict";
import type { PriceHistoryOut } from "../src/api/types";
import { historyValues, priceTrend } from "../src/lib/history";

test("the newest run decides the current price even when it found nothing", () => {
  const trend = priceTrend([284000, 278000, null]);
  assert.equal(trend.latest, null);
  assert.equal(trend.previous, null, "no delta for an old price");
  assert.equal(trend.lastFound, 278000);
  assert.equal(trend.lastFoundRunsAgo, 1);
});

test("delta compares the newest price with the nearest earlier price", () => {
  assert.deepEqual(priceTrend([284000, 278000]), {
    latest: 278000,
    previous: 284000,
    previousIsAdjacent: true,
    lastFound: null,
    lastFoundRunsAgo: null,
  });
  const gap = priceTrend([284000, null, null, 271000]);
  assert.equal(gap.previous, 284000);
  assert.equal(gap.previousIsAdjacent, false);
});

test("edge cases: empty, single point, all null", () => {
  assert.equal(priceTrend([]).latest, null);
  assert.equal(priceTrend([300000]).previous, null);
  const none = priceTrend([null, null]);
  assert.equal(none.lastFound, null);
  assert.equal(none.lastFoundRunsAgo, null);
});

test("historyValues picks the basis", () => {
  const h: PriceHistoryOut = {
    trip_id: 1,
    currency: "KRW",
    price_basis: "per_person",
    points: [
      { run_id: 1, checked_at: "2026-10-05T08:30:00Z", min_total: 600000, min_per_person: 300000, by_provider: {} },
      { run_id: 2, checked_at: "2026-10-05T17:30:00Z", min_total: null, min_per_person: null, by_provider: {} },
    ],
  };
  assert.deepEqual(historyValues(h, "per_person"), [300000, null]);
  assert.deepEqual(historyValues(h, "total"), [600000, null]);
  assert.deepEqual(historyValues(undefined, "total"), []);
});
