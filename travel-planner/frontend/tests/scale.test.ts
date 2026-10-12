import { test } from "node:test";
import assert from "node:assert/strict";
import { relativeTickLabel, windowLabel, windowPhrase } from "../src/components/charts/chartText";
import {
  estimateTextWidth,
  fitLabels,
  FUTURE_SKEW_MS,
  inWindow,
  labelBox,
  longerWindow,
  nearestIndex,
  relativeTicks,
  relativeX,
  svgX,
  TIME_WINDOWS,
  WINDOW_MS,
} from "../src/components/charts/scale";
import { inLang } from "./fixtures";

test("hovering exactly on a point selects that point", () => {
  // twice-daily checks ~22 px apart (as reported: 76.0 -> 97.7)
  const xs = [76.0, 97.7, 119.4, 249.3, 271.0];
  const svgLeft = 300; // svg's left edge in client px
  for (const [i, x] of xs.entries()) {
    assert.equal(nearestIndex(xs, svgX(svgLeft + x, svgLeft, 640, 640)), i);
  }
  // the switch happens at the midpoint (86.85), not 12 px early
  assert.equal(nearestIndex(xs, svgX(svgLeft + 86.5, svgLeft, 640, 640)), 0);
  assert.equal(nearestIndex(xs, svgX(svgLeft + 87.2, svgLeft, 640, 640)), 1);
});

test("svgX scales when the svg is rendered at another size than its viewBox", () => {
  assert.equal(svgX(150, 50, 320, 640), 200);
  assert.equal(svgX(150, 50, 0, 640), 100);
});

test("nearestIndex on an empty list", () => {
  assert.equal(nearestIndex([], 10), -1);
});

// ----------------------------------------------------------------------------- relative time axis

const HOUR = 3_600_000;
const DAY = 24 * HOUR;
const NOW = Date.parse("2026-10-12T05:30:00Z");

test("each window has fixed relative ticks, oldest first, ending with now", () => {
  const show = (w: Parameters<typeof relativeTicks>[0]) => relativeTicks(w).map((t) => `${t.amount}${t.unit}`);
  assert.deepEqual(show("24h"), ["24hour", "18hour", "12hour", "6hour", "0now"]);
  assert.deepEqual(show("7d"), ["7day", "5day", "3day", "1day", "0now"]);
  assert.deepEqual(show("30d"), ["30day", "21day", "14day", "7day", "0now"]);
  assert.equal(relativeTicks("7d")[0].ago, 7 * DAY);
  assert.equal(relativeTicks("24h")[3].ago, 6 * HOUR);
  assert.equal(relativeTicks("30d").at(-1)?.ago, 0);
});

test("ticks land at the same x whenever the chart is drawn", () => {
  // the frame does not depend on when "now" is: 7 days ago is the left edge, now the right
  for (const now of [NOW, NOW + 3 * DAY + 17 * 60_000]) {
    const xs = relativeTicks("7d").map((t) => relativeX(now - t.ago, now, WINDOW_MS["7d"], 50, 750));
    assert.deepEqual(
      xs.map((v) => Math.round(v)),
      [50, 250, 450, 650, 750],
    );
  }
  const day = relativeTicks("24h").map((t) => Math.round(relativeX(NOW - t.ago, NOW, WINDOW_MS["24h"], 0, 240)));
  assert.deepEqual(day, [0, 60, 120, 180, 240]);
  const month = relativeTicks("30d").map((t) => Math.round(relativeX(NOW - t.ago, NOW, WINDOW_MS["30d"], 0, 300)));
  assert.deepEqual(month, [0, 90, 160, 230, 300]);
});

test("a check is placed by its age and moves left as it gets older", () => {
  const checked = NOW - 2 * HOUR;
  const x = (now: number) => relativeX(checked, now, WINDOW_MS["24h"], 0, 240);
  assert.equal(x(NOW), 220);
  assert.equal(x(NOW + 6 * HOUR), 160);
});

test("checks outside the window are left out; positions clamp to the edges", () => {
  const week = WINDOW_MS["7d"];
  assert.equal(inWindow(NOW - 4 * DAY, NOW, week), true);
  assert.equal(inWindow(NOW - 7 * DAY, NOW, week), true, "exactly at the left edge");
  assert.equal(inWindow(NOW - 7 * DAY - 1, NOW, week), false);
  assert.equal(inWindow(NOW - 4 * DAY, NOW, WINDOW_MS["24h"]), false);
  assert.equal(inWindow(NOW + 2 * 60_000, NOW, week), true, "a little clock skew still counts as now");
  assert.equal(inWindow(NOW + FUTURE_SKEW_MS + 1, NOW, week), false);
  assert.equal(inWindow(Number.NaN, NOW, week), false);
  assert.equal(relativeX(NOW - 9 * DAY, NOW, week, 10, 710), 10);
  assert.equal(relativeX(NOW + 60_000, NOW, week, 10, 710), 710);
});

test("the longer window after an empty one", () => {
  assert.equal(longerWindow("24h"), "7d");
  assert.equal(longerWindow("7d"), "30d");
  assert.equal(longerWindow("30d"), null);
  assert.deepEqual(TIME_WINDOWS, ["24h", "7d", "30d"]);
});

test("axis labels that would collide are left out, first and last always stay", () => {
  // phone: "1일 전" centered at 214 runs into "지금" ending at 250
  const boxes: [number, number][] = [
    [0, 33],
    [54, 90],
    [125, 161],
    [196, 232],
    [227, 250],
  ];
  assert.deepEqual(fitLabels(boxes), [true, true, true, false, true]);
  // desktop: everything fits
  assert.deepEqual(fitLabels([[0, 40], [200, 240], [400, 440], [700, 740]]), [true, true, true, true]);
  assert.deepEqual(fitLabels([]), []);
  assert.deepEqual(fitLabels([[0, 10]]), [true]);
  assert.deepEqual(labelBox(100, 40, "middle"), [80, 120]);
  assert.deepEqual(labelBox(100, 40, "end"), [60, 100]);
  assert.deepEqual(labelBox(100, 40, "start"), [100, 140]);
  assert.ok(estimateTextWidth("7일 전", 11) > estimateTextWidth("7d ago", 11) - 20);
  assert.ok(estimateTextWidth("지금", 11) > 18 && estimateTextWidth("지금", 11) < 24);
});

test("tick labels read in the UI language", () => {
  const labels = (w: Parameters<typeof relativeTicks>[0], short = false) =>
    relativeTicks(w).map((t) => relativeTickLabel(t, short));
  inLang("ko", () => {
    assert.deepEqual(labels("24h"), ["24시간 전", "18시간 전", "12시간 전", "6시간 전", "지금"]);
    assert.deepEqual(labels("7d"), ["7일 전", "5일 전", "3일 전", "1일 전", "지금"]);
    assert.deepEqual(labels("30d", true), ["30일 전", "21일 전", "14일 전", "7일 전", "지금"]);
    assert.equal(windowLabel("7d"), "7일");
    assert.equal(windowPhrase("24h"), "지난 24시간");
  });
  inLang("en", () => {
    assert.deepEqual(labels("7d"), ["7 days ago", "5 days ago", "3 days ago", "1 day ago", "now"]);
    assert.deepEqual(labels("24h"), ["24 hours ago", "18 hours ago", "12 hours ago", "6 hours ago", "now"]);
    assert.deepEqual(labels("7d", true), ["7d ago", "5d ago", "3d ago", "1d ago", "now"]);
    assert.equal(windowLabel("30d"), "30 days");
    assert.equal(windowPhrase("7d"), "the last 7 days");
  });
});
