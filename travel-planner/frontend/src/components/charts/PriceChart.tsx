/**
 * Price history line chart: lowest matching price per run (slot 1) plus one line per provider
 * (fixed slots by provider order, so colors follow the entity), a dashed target reference
 * line, a snapping crosshair with one tooltip listing every series, keyboard navigation and
 * a toggleable legend. The runs table below the chart is its table view.
 */

import { useEffect, useId, useMemo, useRef, useState } from "react";
import type { KeyboardEvent, PointerEvent, RefObject } from "react";
import { formatCompactNumber, formatMoney, formatSeoulDay, formatSeoulTime, formatUtc, parseUtc } from "../../lib/format";
import { tr } from "../../lib/i18n";
import { areaPath, linePath, linear, nearestIndex, niceDomain, svgX, timeTicks } from "./scale";

export interface ChartSeries {
  key: string;
  label: string;
  /** CSS color (a --series-N variable). */
  color: string;
  /** One value per x position (null = no data in that run). */
  values: (number | null)[];
  emphasis?: boolean;
}

interface PriceChartProps {
  /** UTC timestamps, one per x position (ascending). */
  times: string[];
  series: ChartSeries[];
  currency: string;
  target?: number | null;
  height?: number;
  /** Accessible summary of the chart. */
  summary: string;
}

const MARGIN = { top: 26, right: 18, bottom: 32, left: 56 };
/** Point markers are drawn on the emphasized line only while they stay readable. */
const MAX_MARKERS = 40;

function useWidth(): [RefObject<HTMLDivElement | null>, number] {
  const ref = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(640);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const ro = new ResizeObserver((entries) => {
      const w = entries[0]?.contentRect.width;
      if (w) setWidth(Math.max(280, Math.floor(w)));
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return [ref, width];
}

/** Multi-series price line chart with hover/focus tooltip. */
export function PriceChart({ times, series, currency, target = null, height = 280, summary }: PriceChartProps) {
  const [wrapRef, width] = useWidth();
  const [hidden, setHidden] = useState<Set<string>>(new Set());
  const [active, setActive] = useState<number | null>(null);
  const gradientId = `chart-fill-${useId().replace(/:/g, "")}`;

  const visible = series.filter((s) => !hidden.has(s.key));
  const innerW = width - MARGIN.left - MARGIN.right;
  const innerH = height - MARGIN.top - MARGIN.bottom;

  const ms = useMemo(() => times.map((t) => parseUtc(t).getTime()), [times]);
  const t0 = ms[0] ?? 0;
  const t1 = ms[ms.length - 1] ?? 1;
  const xt = useMemo(() => {
    if (ms.length <= 1 || t1 === t0) return () => MARGIN.left + innerW / 2;
    return linear(t0, t1, MARGIN.left, MARGIN.left + innerW);
  }, [ms.length, t0, t1, innerW]);
  const x = (i: number) => xt(ms[i] ?? t0);

  const domain = useMemo(() => {
    const vals: number[] = [];
    for (const s of visible) for (const v of s.values) if (v !== null) vals.push(v);
    if (target !== null) vals.push(target);
    if (vals.length === 0) return niceDomain(0, 1);
    return niceDomain(Math.min(...vals), Math.max(...vals), Math.max(3, Math.round(innerH / 60)));
  }, [visible, target, innerH]);
  const y = linear(domain.lo, domain.hi, MARGIN.top + innerH, MARGIN.top);

  const xTicks = timeTicks(t0, t1, Math.max(2, Math.floor(innerW / 90)));

  const onPointerMove = (e: PointerEvent<SVGRectElement>) => {
    const svg = e.currentTarget.ownerSVGElement;
    if (!svg || times.length === 0) return;
    const box = svg.getBoundingClientRect();
    const px = svgX(e.clientX, box.left, box.width, width);
    const best = nearestIndex(times.map((_, i) => x(i)), px);
    if (best >= 0) setActive(best);
  };

  const onKeyDown = (e: KeyboardEvent<SVGSVGElement>) => {
    if (times.length === 0) return;
    if (e.key === "ArrowRight") setActive((i) => Math.min(times.length - 1, (i ?? -1) + 1));
    else if (e.key === "ArrowLeft") setActive((i) => Math.max(0, (i ?? times.length) - 1));
    else if (e.key === "Home") setActive(0);
    else if (e.key === "End") setActive(times.length - 1);
    else if (e.key === "Escape") setActive(null);
    else return;
    e.preventDefault();
  };

  const toggle = (key: string) =>
    setHidden((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else if (series.length - next.size > 1) next.add(key);
      return next;
    });

  // Direct label: latest value of the emphasized series.
  const main = visible.find((s) => s.emphasis) ?? visible[0];
  let endLabel: { x: number; y: number; text: string } | null = null;
  if (main) {
    for (let i = main.values.length - 1; i >= 0; i--) {
      const v = main.values[i];
      if (v !== null) {
        endLabel = { x: x(i), y: y(v), text: formatMoney(v, currency) };
        break;
      }
    }
  }

  const tooltipLeft = active !== null ? x(active) : 0;
  const flip = tooltipLeft > width * 0.6;

  return (
    <figure className="chart" aria-label={summary}>
      {series.length > 1 && (
        <div className="chart__legend" role="group" aria-label={tr("Series (click to show or hide)", "데이터 계열 (클릭해서 표시/숨기기)")}>
          {series.map((s) => (
            <button
              key={s.key}
              type="button"
              className="legend-item"
              aria-pressed={!hidden.has(s.key)}
              onClick={() => toggle(s.key)}
            >
              <span className="legend-key" style={{ background: s.color }} aria-hidden />
              {s.label}
            </button>
          ))}
          {target !== null && (
            <span className="legend-item legend-item--static">
              <span className="legend-key legend-key--dashed" aria-hidden />
              {tr("Target", "목표가")}
            </span>
          )}
        </div>
      )}
      <div className="chart__plot" ref={wrapRef}>
        <svg
          width={width}
          height={height}
          viewBox={`0 0 ${width} ${height}`}
          role="img"
          aria-label={summary}
          tabIndex={0}
          onKeyDown={onKeyDown}
          onBlur={() => setActive(null)}
        >
          <defs>
            <linearGradient id={gradientId} x1="0" x2="0" y1="0" y2="1">
              <stop offset="0%" stopColor="var(--series-1)" stopOpacity={0.16} />
              <stop offset="100%" stopColor="var(--series-1)" stopOpacity={0} />
            </linearGradient>
          </defs>
          {/* grid + y ticks */}
          {domain.ticks.map((t) => (
            <g key={t}>
              <line
                x1={MARGIN.left}
                x2={MARGIN.left + innerW}
                y1={y(t)}
                y2={y(t)}
                stroke="var(--chart-grid)"
                strokeWidth={1}
              />
              <text
                x={MARGIN.left - 8}
                y={y(t)}
                dy="0.32em"
                textAnchor="end"
                className="chart__tick"
              >
                {formatCompactNumber(t)}
              </text>
            </g>
          ))}
          <line
            x1={MARGIN.left}
            x2={MARGIN.left + innerW}
            y1={MARGIN.top + innerH}
            y2={MARGIN.top + innerH}
            stroke="var(--chart-axis)"
            strokeWidth={1}
          />
          {/* x ticks */}
          {xTicks.map((tick) => (
            <g key={tick.t}>
              <line
                x1={xt(tick.t)}
                x2={xt(tick.t)}
                y1={MARGIN.top + innerH}
                y2={MARGIN.top + innerH + 4}
                stroke="var(--chart-axis)"
                strokeWidth={1}
              />
              <text x={xt(tick.t)} y={height - 8} textAnchor="middle" className="chart__tick">
                {tick.kind === "day" ? formatSeoulDay(tick.t) : formatSeoulTime(tick.t)}
              </text>
            </g>
          ))}
          {/* target */}
          {target !== null && (
            <g>
              <line
                x1={MARGIN.left}
                x2={MARGIN.left + innerW}
                y1={y(target)}
                y2={y(target)}
                stroke="var(--text-muted)"
                strokeWidth={1.25}
                strokeDasharray="5 4"
              />
              <text x={MARGIN.left + 6} y={y(target) - 7} className="chart__annot">
                {tr(`Target ${formatMoney(target, currency)}`, `목표가 ${formatMoney(target, currency)}`)}
              </text>
            </g>
          )}
          {/* series (emphasized last so it sits on top) */}
          {[...visible]
            .sort((a, b) => Number(Boolean(a.emphasis)) - Number(Boolean(b.emphasis)))
            .map((s) => {
              const pts = s.values.map((v, i) => (v === null ? null : { x: x(i), y: y(v) }));
              const count = pts.filter(Boolean).length;
              const lone = count === 1 || times.length === 1;
              const markers = lone || (s.emphasis && count <= MAX_MARKERS);
              return (
                <g key={s.key}>
                  {s.emphasis && <path d={areaPath(pts, MARGIN.top + innerH)} fill={`url(#${gradientId})`} stroke="none" />}
                  <path
                    d={linePath(pts)}
                    fill="none"
                    stroke={s.color}
                    strokeWidth={s.emphasis ? 2.25 : 1.75}
                    strokeLinejoin="round"
                    strokeLinecap="round"
                    opacity={s.emphasis ? 1 : 0.85}
                  />
                  {markers &&
                    pts.map(
                      (p, i) =>
                        p && (
                          <circle
                            key={i}
                            cx={p.x}
                            cy={p.y}
                            r={4}
                            fill="var(--surface)"
                            stroke={s.color}
                            strokeWidth={2}
                          />
                        ),
                    )}
                </g>
              );
            })}
          {endLabel && active === null && (
            <text
              x={Math.min(endLabel.x, MARGIN.left + innerW)}
              y={endLabel.y - 10}
              textAnchor="end"
              className="chart__endlabel"
            >
              {endLabel.text}
            </text>
          )}
          {/* crosshair */}
          {active !== null && (
            <g pointerEvents="none">
              <line
                x1={x(active)}
                x2={x(active)}
                y1={MARGIN.top}
                y2={MARGIN.top + innerH}
                stroke="var(--border-strong)"
                strokeWidth={1}
                strokeDasharray="3 3"
              />
              {visible.map((s) => {
                const v = s.values[active];
                return v === null ? null : (
                  <circle
                    key={s.key}
                    cx={x(active)}
                    cy={y(v)}
                    r={4.5}
                    fill={s.color}
                    stroke="var(--surface)"
                    strokeWidth={2}
                  />
                );
              })}
            </g>
          )}
          <rect
            x={MARGIN.left - 12}
            y={MARGIN.top}
            width={innerW + 24}
            height={innerH}
            fill="transparent"
            onPointerMove={onPointerMove}
            onPointerLeave={() => setActive(null)}
            onPointerDown={onPointerMove}
          />
        </svg>
        {active !== null && times[active] && (
          <div
            className="chart__tooltip"
            role="status"
            aria-live="polite"
            style={{
              left: flip ? undefined : tooltipLeft + 12,
              right: flip ? width - tooltipLeft + 12 : undefined,
              top: MARGIN.top,
            }}
          >
            <div className="chart__tooltip-title">{formatUtc(times[active])}</div>
            {visible.map((s) => (
              <div className="chart__tooltip-row" key={s.key}>
                <span className="legend-key" style={{ background: s.color }} aria-hidden />
                <span className="chart__tooltip-value">
                  {s.values[active] === null ? "—" : formatMoney(s.values[active], currency)}
                </span>
                <span className="chart__tooltip-label">{s.label}</span>
              </div>
            ))}
          </div>
        )}
      </div>
    </figure>
  );
}
