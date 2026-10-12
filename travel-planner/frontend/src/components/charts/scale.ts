/** Tiny scale helpers for the hand-rolled SVG charts. Pure: no DOM, no i18n (tests/scale.test.ts). */

export interface NiceDomain {
  lo: number;
  hi: number;
  ticks: number[];
}

/** Round a [min, max] range out to clean tick steps (1/2/5 × 10^n). */
export function niceDomain(min: number, max: number, count = 4): NiceDomain {
  let a = min;
  let b = max;
  if (!Number.isFinite(a) || !Number.isFinite(b)) return { lo: 0, hi: 1, ticks: [0, 1] };
  if (a === b) {
    const pad = Math.abs(a) * 0.05 || 1;
    a -= pad;
    b += pad;
  }
  const raw = (b - a) / count;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const norm = raw / mag;
  const step = (norm < 1.5 ? 1 : norm < 3 ? 2 : norm < 7 ? 5 : 10) * mag;
  const lo = Math.floor(a / step) * step;
  const hi = Math.ceil(b / step) * step;
  const ticks: number[] = [];
  for (let v = lo; v <= hi + step / 2; v += step) ticks.push(Number(v.toPrecision(12)));
  return { lo, hi, ticks };
}

// ----------------------------------------------------------------------------- relative time axis
// The x axis ends at "now" and spans a fixed window, so the frame (ticks, size) is the same
// every time the page opens; only the points move left as they age.

const HOUR = 3_600_000;
const DAY = 24 * HOUR;

export type TimeWindow = "24h" | "7d" | "30d";

export const TIME_WINDOWS: readonly TimeWindow[] = ["24h", "7d", "30d"];

export const WINDOW_MS: Readonly<Record<TimeWindow, number>> = {
  "24h": DAY,
  "7d": 7 * DAY,
  "30d": 30 * DAY,
};

/** A fixed tick of a window: "7 days ago" is { ago: 7 days, amount: 7, unit: "day" }. */
export interface RelativeTick {
  /** Milliseconds before now (0 for "now"). */
  ago: number;
  /** The number in the label (0 for "now"). */
  amount: number;
  unit: "hour" | "day" | "now";
}

const WINDOW_TICKS: Readonly<Record<TimeWindow, readonly (readonly [number, "hour" | "day"])[]>> = {
  "24h": [
    [24, "hour"],
    [18, "hour"],
    [12, "hour"],
    [6, "hour"],
  ],
  "7d": [
    [7, "day"],
    [5, "day"],
    [3, "day"],
    [1, "day"],
  ],
  "30d": [
    [30, "day"],
    [21, "day"],
    [14, "day"],
    [7, "day"],
  ],
};

/** The window's ticks, oldest first, ending with "now". */
export function relativeTicks(window: TimeWindow): RelativeTick[] {
  return [
    ...WINDOW_TICKS[window].map(([amount, unit]) => ({ ago: amount * (unit === "day" ? DAY : HOUR), amount, unit })),
    { ago: 0, amount: 0, unit: "now" as const },
  ];
}

/** The next longer window (24h -> 7d -> 30d), or null for the longest. */
export function longerWindow(window: TimeWindow): TimeWindow | null {
  const i = TIME_WINDOWS.indexOf(window);
  return i >= 0 && i < TIME_WINDOWS.length - 1 ? TIME_WINDOWS[i + 1] : null;
}

/** Points slightly after `now` (clock skew between server and browser) still count as now. */
export const FUTURE_SKEW_MS = 10 * 60_000;

/** Whether the instant `t` falls inside the window that ends at `now`. */
export function inWindow(t: number, now: number, windowMs: number): boolean {
  return Number.isFinite(t) && t >= now - windowMs && t <= now + FUTURE_SKEW_MS;
}

/**
 * x position of the instant `t` on a window ending at `now`: `now - windowMs` maps to `r0`,
 * `now` to `r1`; instants outside the window are clamped to the edges.
 */
export function relativeX(t: number, now: number, windowMs: number, r0: number, r1: number): number {
  const f = windowMs > 0 ? (t - (now - windowMs)) / windowMs : 1;
  return r0 + Math.min(1, Math.max(0, f)) * (r1 - r0);
}

// ----------------------------------------------------------------------------- tick labels

/**
 * Rough rendered width of a short label in px (no DOM): Hangul/CJK about one em, digits and
 * Latin about 0.6 em, spaces 0.3 em. Good enough to keep axis labels from colliding.
 */
export function estimateTextWidth(text: string, fontPx: number): number {
  let em = 0;
  for (const ch of text) {
    const code = ch.codePointAt(0) ?? 0;
    if (code >= 0x1100) em += 0.95;
    else if (ch === " ") em += 0.3;
    else if (/[0-9]/.test(ch)) em += 0.6;
    else em += 0.56;
  }
  return em * fontPx;
}

/** [left, right] extent of a label of `width` px drawn at `x` with an SVG text-anchor. */
export function labelBox(x: number, width: number, anchor: "start" | "middle" | "end"): [number, number] {
  if (anchor === "start") return [x, x + width];
  if (anchor === "end") return [x - width, x];
  return [x - width / 2, x + width / 2];
}

/**
 * Which axis labels to draw so none overlap: the first and last always, a middle one only
 * when it keeps `gap` px clear of the last kept label and of the final one.
 */
export function fitLabels(boxes: readonly (readonly [number, number])[], gap = 8): boolean[] {
  const n = boxes.length;
  const keep = boxes.map(() => false);
  if (n === 0) return keep;
  keep[0] = true;
  keep[n - 1] = true;
  let prevRight = boxes[0][1];
  const lastLeft = n > 1 ? boxes[n - 1][0] : Infinity;
  for (let i = 1; i < n - 1; i++) {
    const [left, right] = boxes[i];
    if (left >= prevRight + gap && right <= lastLeft - gap) {
      keep[i] = true;
      prevRight = right;
    }
  }
  return keep;
}

/** Linear map from a domain to a range. */
export function linear(d0: number, d1: number, r0: number, r1: number): (v: number) => number {
  const span = d1 - d0 || 1;
  return (v) => r0 + ((v - d0) / span) * (r1 - r0);
}

/** SVG path through points, breaking the line at null values. */
export function linePath(points: ({ x: number; y: number } | null)[]): string {
  let d = "";
  let pen = false;
  for (const p of points) {
    if (!p) {
      pen = false;
      continue;
    }
    d += `${pen ? "L" : "M"}${p.x.toFixed(1)},${p.y.toFixed(1)}`;
    pen = true;
  }
  return d;
}

/**
 * Closed SVG area path under a line down to `baseY`, one shape per run of non-null points
 * (single points get no area).
 */
export function areaPath(points: ({ x: number; y: number } | null)[], baseY: number): string {
  let d = "";
  let run: { x: number; y: number }[] = [];
  const flush = () => {
    if (run.length > 1) {
      const first = run[0];
      const last = run[run.length - 1];
      d += `M${first.x.toFixed(1)},${baseY.toFixed(1)}`;
      for (const p of run) d += `L${p.x.toFixed(1)},${p.y.toFixed(1)}`;
      d += `L${last.x.toFixed(1)},${baseY.toFixed(1)}Z`;
    }
    run = [];
  };
  for (const p of points) {
    if (p) run.push(p);
    else flush();
  }
  flush();
  return d;
}

/** Index of the x position nearest to `px` (-1 for an empty list); ties go to the earlier one. */
export function nearestIndex(xs: readonly number[], px: number): number {
  let best = -1;
  let bestDist = Infinity;
  for (let i = 0; i < xs.length; i++) {
    const d = Math.abs(xs[i] - px);
    if (d < bestDist) {
      bestDist = d;
      best = i;
    }
  }
  return best;
}

/**
 * Pointer x in SVG user units: `clientX` relative to the svg's own box (not the hit target's,
 * which may start elsewhere), scaled when CSS renders the svg at another size than its viewBox.
 */
export function svgX(clientX: number, svgLeft: number, svgRenderedWidth: number, viewBoxWidth: number): number {
  const scale = svgRenderedWidth > 0 ? viewBoxWidth / svgRenderedWidth : 1;
  return (clientX - svgLeft) * scale;
}
