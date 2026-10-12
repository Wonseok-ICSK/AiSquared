/** Signed price change vs a previous value (down = good). Icon + text, never color alone. */

import { ArrowDownRight, ArrowUpRight, Minus } from "lucide-react";
import { formatMoneyDelta } from "../lib/format";
import { tr } from "../lib/i18n";

/** "▼ −₩12,000" style delta with an accessible description ("cheaper vs previous check"). */
export function PriceDelta({
  current,
  previous,
  currency,
  context,
}: {
  current: number | null | undefined;
  previous: number | null | undefined;
  currency: string;
  /** Comparison phrase; defaults to "vs previous check" / "직전 확인 대비". */
  context?: string;
}) {
  if (current === null || current === undefined || previous === null || previous === undefined) {
    return null;
  }
  const ctx = context ?? tr("vs previous check", "직전 확인 대비");
  const delta = current - previous;
  const pct = previous !== 0 ? (delta / previous) * 100 : 0;
  const dir = delta < 0 ? "down" : delta > 0 ? "up" : "flat";
  const Icon = dir === "down" ? ArrowDownRight : dir === "up" ? ArrowUpRight : Minus;
  const pctText = dir === "flat" ? "" : ` (${pct > 0 ? "+" : ""}${pct.toFixed(1)}%)`;
  const amount = formatMoneyDelta(delta, currency);
  return (
    <span className={`delta delta--${dir}`} title={tr(`${amount}${pctText} ${ctx}`, `${ctx} ${amount}${pctText}`)}>
      <Icon size={14} aria-hidden />
      <span>
        {amount}
        <span className="sr-only">
          {" "}
          {tr(
            `${dir === "flat" ? "unchanged" : dir === "down" ? "cheaper" : "more expensive"} ${ctx}`,
            `(${ctx} ${dir === "flat" ? "변동 없음" : dir === "down" ? "하락" : "상승"})`,
          )}
        </span>
      </span>
    </span>
  );
}
