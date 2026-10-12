/**
 * Price history line chart on a relative time axis. The window ends at "now" and has fixed
 * ticks ("7일 전 … 1일 전 · 지금"), so the frame has the same size and layout whenever the page
 * opens; checks older than the window are not drawn. One clay line (--series-1) of the lowest
 * matching price per check with small points, a dashed target line with a label, the latest
 * price labelled, and a snapping hover/focus tooltip (relative time, Seoul time, price).
 * The runs table under the chart is its table view.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import type { KeyboardEvent, PointerEvent, ReactNode, RefObject } from "react";
import { formatCompactNumber, formatMoney, formatRelative, formatUtc, parseUtc } from "../../lib/format";
import { tr } from "../../lib/i18n";
import { relativeTickLabel } from "./chartText";
import {
  estimateTextWidth,
  fitLabels,
  inWindow,
  labelBox,
  linePath,
  linear,
  nearestIndex,
  niceDomain,
  relativeTicks,
  relativeX,
  svgX,
  WINDOW_MS,
} from "./scale";
import type { TimeWindow } from "./scale";

export interface ChartPoint {
  /** UTC timestamp of the check. */
  at: string;
  /** Lowest matching price of that check (null: nothing matched; the line breaks there). */
  value: number | null;
}

interface PriceChartProps {
  /** Every check, any order; only those inside the window are drawn. */
  points: readonly ChartPoint[];
  /** The time window shown (ends at now). */
  range: TimeWindow;
  /** Epoch ms of "now" (the right edge). */
  now: number;
  currency: string;
  target?: number | null;
  /** Accessible summary of the chart. */
  summary: string;
  /** Shown over the empty frame when no check with a price falls inside the window. */
  empty?: ReactNode;
}

interface Check {
  at: string;
  t: number;
  value: number | null;
}
type PricedCheck = Check & { value: number };

const MARGIN = { top: 30, right: 20, bottom: 36, left: 54 };
/** Narrower value-label column on phones. */
const COMPACT_LEFT = 46;
/** Axis label size in px (keep in sync with .chart__tick). */
const TICK_FONT = 11;
/** Below this width the chart is shorter and English tick labels use "7d ago". */
const COMPACT_WIDTH = 560;
/** Points are drawn on the line while they stay readable; the latest always is. */
const MAX_MARKERS = 60;

function useWidth(): [RefObject<HTMLDivElement | null>, number] {
  const ref = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(720);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const ro = new ResizeObserver((entries) => {
      const w = entries[0]?.contentRect.width;
      if (w) setWidth(Math.max(260, Math.floor(w)));
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return [ref, width];
}

/** Single-series price line on a fixed relative time window. */
export function PriceChart({ points, range, now, currency, target = null, summary, empty }: PriceChartProps) {
  const [wrapRef, width] = useWidth();
  const [active, setActive] = useState<number | null>(null);
  useEffect(() => setActive(null), [range]);

  const compact = width < COMPACT_WIDTH;
  const height = compact ? 280 : 340;
  const x0 = compact ? COMPACT_LEFT : MARGIN.left;
  const x1 = width - MARGIN.right;
  const yTop = MARGIN.top;
  const yBase = height - MARGIN.bottom;
  const windowMs = WINDOW_MS[range];

  const checks = useMemo(
    (): Check[] =>
      points
        .map((p) => ({ at: p.at, t: parseUtc(p.at).getTime(), value: p.value }))
        .filter((c) => inWindow(c.t, now, windowMs))
        .sort((a, b) => a.t - b.t),
    [points, now, windowMs],
  );
  const priced = useMemo(() => checks.filter((c): c is PricedCheck => c.value !== null), [checks]);

  const domain = useMemo(() => {
    if (priced.length === 0) return null;
    const vals = priced.map((c) => c.value);
    if (target !== null) vals.push(target);
    const lo = Math.min(...vals);
    const hi = Math.max(...vals);
    // A little air so the lowest / highest point does not sit on the frame.
    const pad = (hi - lo) * 0.05;
    return niceDomain(lo - pad, hi + pad, compact ? 3 : 4);
  }, [priced, target, compact]);

  const x = (t: number) => relativeX(t, now, windowMs, x0, x1);
  const y = domain ? linear(domain.lo, domain.hi, yBase, yTop) : () => yBase;
  // Fixed relative ticks; a middle label that would collide (phones) is left out, its tick stays.
  const ticks = relativeTicks(range).map((tick, i, all) => {
    const label = relativeTickLabel(tick, compact);
    const tx = x(now - tick.ago);
    const anchor: "start" | "middle" | "end" = i === 0 ? "start" : i === all.length - 1 ? "end" : "middle";
    return { tick, label, x: tx, anchor, box: labelBox(tx, estimateTextWidth(label, TICK_FONT), anchor) };
  });
  const showLabel = fitLabels(ticks.map((t) => t.box));
  // Without data there is no value scale: keep the frame with plain gridlines.
  const gridYs = domain
    ? domain.ticks.map((v) => ({ y: y(v), label: formatCompactNumber(v) }))
    : [0, 1, 2, 3].map((i) => ({ y: yTop + ((yBase - yTop) * i) / 3, label: "" }));

  const current = active !== null && active < priced.length ? priced[active] : null;
  const xs = priced.map((c) => x(c.t));

  const onPointerMove = (e: PointerEvent<SVGRectElement>) => {
    const svg = e.currentTarget.ownerSVGElement;
    if (!svg || priced.length === 0) return;
    const box = svg.getBoundingClientRect();
    const i = nearestIndex(xs, svgX(e.clientX, box.left, box.width, width));
    if (i >= 0) setActive(i);
  };

  const onKeyDown = (e: KeyboardEvent<SVGSVGElement>) => {
    const n = priced.length;
    if (n === 0) return;
    if (e.key === "ArrowRight") setActive((i) => Math.min(n - 1, (i ?? -1) + 1));
    else if (e.key === "ArrowLeft") setActive((i) => Math.max(0, (i ?? n) - 1));
    else if (e.key === "Home") setActive(0);
    else if (e.key === "End") setActive(n - 1);
    else if (e.key === "Escape") setActive(null);
    else return;
    e.preventDefault();
  };

  // The latest price, labelled on the side its line does not come from.
  const latest = priced[priced.length - 1] ?? null;
  const before = priced[priced.length - 2] ?? null;
  let latestLabel: { x: number; y: number; anchor: "start" | "middle" | "end"; text: string } | null = null;
  if (latest) {
    const lx = x(latest.t);
    const ly = y(latest.value);
    const below = before !== null && before.value > latest.value && ly + 24 < yBase;
    const anchor = lx > x1 - 80 ? "end" : lx < x0 + 80 ? "start" : "middle";
    latestLabel = {
      x: anchor === "end" ? lx + 6 : anchor === "start" ? lx - 6 : lx,
      y: below ? ly + 22 : ly - 13,
      anchor,
      text: formatMoney(latest.value, currency),
    };
  }

  const targetY = target !== null && domain ? y(target) : null;
  // The target label sits at the left edge unless the latest price is labelled there.
  const targetRight = latest !== null && x(latest.t) < x0 + 220;

  const tipX = current ? x(current.t) : 0;
  const tipY = current ? y(current.value) : 0;
  const flip = tipX > width * 0.6;

  return (
    <figure className="chart">
      <div className="chart__plot" ref={wrapRef}>
        <svg
          width={width}
          height={height}
          viewBox={`0 0 ${width} ${height}`}
          role="img"
          aria-label={summary}
          tabIndex={priced.length > 0 ? 0 : undefined}
          onKeyDown={onKeyDown}
          onBlur={() => setActive(null)}
        >
          {/* horizontal grid + value ticks */}
          {gridYs.map((g, i) => (
            <g key={i}>
              <line x1={x0} x2={x1} y1={g.y} y2={g.y} stroke="var(--chart-grid)" strokeWidth={1} />
              {g.label && (
                <text x={x0 - 10} y={g.y} dy="0.32em" textAnchor="end" className="chart__tick">
                  {g.label}
                </text>
              )}
            </g>
          ))}
          <line x1={x0} x2={x1} y1={yBase} y2={yBase} stroke="var(--chart-axis)" strokeWidth={1} />

          {/* fixed relative time ticks */}
          {ticks.map(({ tick, label, x: tx, anchor }, i) => (
            <g key={tick.ago}>
              <line x1={tx} x2={tx} y1={yBase} y2={yBase + 5} stroke="var(--chart-axis)" strokeWidth={1} />
              {showLabel[i] && (
                <text
                  x={tx}
                  y={height - 10}
                  textAnchor={anchor}
                  className={tick.unit === "now" ? "chart__tick chart__tick--now" : "chart__tick"}
                >
                  {label}
                </text>
              )}
            </g>
          ))}

          {/* target line (its label is drawn above the data) */}
          {targetY !== null && (
            <line
              x1={x0}
              x2={x1}
              y1={targetY}
              y2={targetY}
              stroke="var(--text-muted)"
              strokeWidth={1.25}
              strokeDasharray="5 4"
            />
          )}

          {/* the line and its points */}
          <path
            d={linePath(checks.map((c) => (c.value === null ? null : { x: x(c.t), y: y(c.value) })))}
            fill="none"
            stroke="var(--series-1)"
            strokeWidth={2}
            strokeLinejoin="round"
            strokeLinecap="round"
          />
          {priced.map((c, i) => {
            const last = i === priced.length - 1;
            if (!last && priced.length > MAX_MARKERS) return null;
            return (
              <circle
                key={c.at}
                cx={x(c.t)}
                cy={y(c.value)}
                r={last ? 6 : 4.5}
                fill="var(--series-1)"
                stroke="var(--surface)"
                strokeWidth={2}
              />
            );
          })}
          {targetY !== null && target !== null && (
            <text
              x={targetRight ? x1 - 4 : x0 + 6}
              y={targetY - 7}
              textAnchor={targetRight ? "end" : "start"}
              className="chart__annot"
            >
              {tr(`Target ${formatMoney(target, currency)}`, `목표가 ${formatMoney(target, currency)}`)}
            </text>
          )}
          {latestLabel && current === null && (
            <text x={latestLabel.x} y={latestLabel.y} textAnchor={latestLabel.anchor} className="chart__endlabel">
              {latestLabel.text}
            </text>
          )}

          {/* crosshair */}
          {current && (
            <g pointerEvents="none">
              <line
                x1={tipX}
                x2={tipX}
                y1={yTop}
                y2={yBase}
                stroke="var(--border-strong)"
                strokeWidth={1}
                strokeDasharray="3 3"
              />
              <circle cx={tipX} cy={tipY} r={7} fill="var(--series-1)" stroke="var(--surface)" strokeWidth={2} />
            </g>
          )}
          {priced.length > 0 && (
            <rect
              x={x0 - 12}
              y={yTop}
              width={x1 - x0 + 24}
              height={yBase - yTop}
              fill="transparent"
              onPointerMove={onPointerMove}
              onPointerLeave={() => setActive(null)}
              onPointerDown={onPointerMove}
            />
          )}
        </svg>

        {priced.length === 0 && empty && (
          <div className="chart__empty" style={{ left: x0, width: x1 - x0, top: yTop, height: yBase - yTop }}>
            {empty}
          </div>
        )}

        {current && (
          <div
            className="chart__tooltip"
            role="status"
            aria-live="polite"
            style={{
              left: flip ? undefined : tipX + 14,
              right: flip ? width - tipX + 14 : undefined,
              top: Math.max(4, Math.min(tipY - 44, height - 100)),
            }}
          >
            <div className="chart__tooltip-price">{formatMoney(current.value, currency)}</div>
            <div className="chart__tooltip-when">{formatRelative(current.at, now)}</div>
            <div className="chart__tooltip-time">{formatUtc(current.at)}</div>
          </div>
        )}
      </div>
    </figure>
  );
}
