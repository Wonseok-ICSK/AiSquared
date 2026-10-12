/** Compact trend line of run minima (soft accent line + area, accent end dot, optional target). */

import { useId } from "react";
import { tr } from "../../lib/i18n";
import { areaPath, linePath, linear } from "./scale";

interface SparklineProps {
  values: number[];
  target?: number | null;
  /** False when the newest check found nothing: the end dot is drawn hollow (not "now"). */
  endIsCurrent?: boolean;
  width?: number;
  height?: number;
  label: string;
}

/** Sparkline sized in px; renders nothing meaningful for < 2 values (just a dot). */
export function Sparkline({ values, target = null, endIsCurrent = true, width = 132, height = 40, label }: SparklineProps) {
  const pad = 5;
  const gradientId = `spark-fill-${useId().replace(/:/g, "")}`;
  if (values.length === 0) {
    return (
      <svg width={width} height={height} role="img" aria-label={tr(`${label}: no data yet`, `${label}: 아직 데이터 없음`)}>
        <line
          x1={pad}
          x2={width - pad}
          y1={height / 2}
          y2={height / 2}
          stroke="var(--chart-grid)"
          strokeWidth={1}
        />
      </svg>
    );
  }
  let lo = Math.min(...values);
  let hi = Math.max(...values);
  const span = hi - lo || Math.abs(hi) * 0.1 || 1;
  const showTarget = target !== null && target >= lo - span && target <= hi + span;
  if (showTarget && target !== null) {
    lo = Math.min(lo, target);
    hi = Math.max(hi, target);
  }
  if (lo === hi) {
    lo -= span / 2;
    hi += span / 2;
  }
  const x = linear(0, Math.max(1, values.length - 1), pad, width - pad);
  const y = linear(lo, hi, height - pad, pad);
  const pts = values.map((v, i) => ({ x: values.length === 1 ? width / 2 : x(i), y: y(v) }));
  const last = pts[pts.length - 1];
  return (
    <svg
      className="sparkline"
      width={width}
      height={height}
      viewBox={`0 0 ${width} ${height}`}
      role="img"
      aria-label={label}
    >
      {showTarget && target !== null && (
        <line
          x1={0}
          x2={width}
          y1={y(target)}
          y2={y(target)}
          stroke="var(--text-muted)"
          strokeWidth={1}
          strokeDasharray="3 3"
          opacity={0.7}
        />
      )}
      {pts.length > 1 && (
        <>
          <defs>
            <linearGradient id={gradientId} x1="0" x2="0" y1="0" y2="1">
              <stop offset="0%" stopColor="var(--series-1)" stopOpacity={0.14} />
              <stop offset="100%" stopColor="var(--series-1)" stopOpacity={0} />
            </linearGradient>
          </defs>
          <path d={areaPath(pts, height)} fill={`url(#${gradientId})`} stroke="none" />
          <path
            d={linePath(pts)}
            fill="none"
            stroke="var(--series-1)"
            strokeOpacity={0.55}
            strokeWidth={1.75}
            strokeLinejoin="round"
            strokeLinecap="round"
          />
        </>
      )}
      {endIsCurrent ? (
        <circle cx={last.x} cy={last.y} r={4} fill="var(--series-1)" stroke="var(--surface)" strokeWidth={2} />
      ) : (
        <circle cx={last.x} cy={last.y} r={3.5} fill="var(--surface)" stroke="var(--series-muted)" strokeWidth={2} />
      )}
    </svg>
  );
}
