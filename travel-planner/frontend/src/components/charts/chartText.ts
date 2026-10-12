/**
 * Words of the relative time axis in the UI language ("7일 전" / "7 days ago", "지금" / "now").
 * Kept apart from ./scale (pure numbers) and free of React so it is unit-tested.
 */

import { tr } from "../../lib/i18n";
import type { RelativeTick, TimeWindow } from "./scale";

/** "7일 전" / "7 days ago", "6시간 전" / "6 hours ago", "지금" / "now"; `short` gives "7d ago". */
export function relativeTickLabel(tick: RelativeTick, short = false): string {
  const n = tick.amount;
  if (tick.unit === "now" || n === 0) return tr("now", "지금");
  if (tick.unit === "hour") {
    return tr(short ? `${n}h ago` : `${n} hour${n === 1 ? "" : "s"} ago`, `${n}시간 전`);
  }
  return tr(short ? `${n}d ago` : `${n} day${n === 1 ? "" : "s"} ago`, `${n}일 전`);
}

/** Segmented-control label of a window: "24시간" / "24 hours". */
export function windowLabel(window: TimeWindow): string {
  if (window === "24h") return tr("24 hours", "24시간");
  if (window === "7d") return tr("7 days", "7일");
  return tr("30 days", "30일");
}

/** The window as a phrase: "지난 7일" / "the last 7 days". */
export function windowPhrase(window: TimeWindow): string {
  if (window === "24h") return tr("the last 24 hours", "지난 24시간");
  if (window === "7d") return tr("the last 7 days", "지난 7일");
  return tr("the last 30 days", "지난 30일");
}
