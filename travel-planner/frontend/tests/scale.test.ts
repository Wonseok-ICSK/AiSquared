import { test } from "node:test";
import assert from "node:assert/strict";
import { nearestIndex, svgX } from "../src/components/charts/scale";

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
