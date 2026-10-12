/** Tiny scale helpers for the hand-rolled SVG charts. */

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

const HOUR = 3_600_000;
const DAY = 24 * HOUR;
const SEOUL_OFFSET = 9 * HOUR; // Asia/Seoul has no DST

export interface TimeTick {
  t: number;
  kind: "day" | "hour";
}

/**
 * Tick instants for a time axis: Seoul midnights (every N days) for multi-day spans,
 * otherwise round Seoul hours (every 1/3/6/12 h). At most `maxTicks`.
 */
export function timeTicks(t0: number, t1: number, maxTicks: number): TimeTick[] {
  const span = t1 - t0;
  if (span <= 0) return [{ t: t0, kind: "hour" }];
  if (span >= 1.5 * DAY) {
    const first = Math.ceil((t0 + SEOUL_OFFSET) / DAY) * DAY - SEOUL_OFFSET;
    const days = Math.floor((t1 - first) / DAY) + 1;
    const step = Math.max(1, Math.ceil(days / Math.max(1, maxTicks)));
    const ticks: TimeTick[] = [];
    for (let t = first; t <= t1; t += step * DAY) ticks.push({ t, kind: "day" });
    return ticks;
  }
  const stepH = [1, 3, 6, 12].find((h) => span / (h * HOUR) <= maxTicks) ?? 24;
  const step = stepH * HOUR;
  const ticks: TimeTick[] = [];
  for (let t = Math.ceil((t0 + SEOUL_OFFSET) / step) * step - SEOUL_OFFSET; t <= t1; t += step) ticks.push({ t, kind: "hour" });
  return ticks;
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
