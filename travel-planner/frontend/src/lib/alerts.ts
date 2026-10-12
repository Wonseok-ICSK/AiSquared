/** Pure helpers for alert rules, mirroring services/alerts.py. */

import type { AlertRuleOut, PriceBasis, TripOut } from "../api/types";
import { BASIS_LABEL, formatMoney } from "./format";
import { tr } from "./i18n";

type RuleLike = Pick<AlertRuleOut, "kind" | "threshold" | "price_basis">;

/** Whether a rule compares against the trip's target (below_target without its own threshold). */
export function usesTripTarget(rule: Pick<AlertRuleOut, "kind" | "threshold">): boolean {
  return rule.kind === "below_target" && rule.threshold === null;
}

/**
 * The basis the backend really compares in: a below_target rule without a threshold uses the
 * trip's target in the trip's basis (alerts._target), ignoring the rule's own price_basis.
 */
export function effectiveRuleBasis(rule: RuleLike, trip: Pick<TripOut, "price_basis">): PriceBasis {
  return usesTripTarget(rule) ? trip.price_basis : rule.price_basis;
}

/** One-line description of what a rule does. */
export function describeRule(rule: RuleLike & Pick<AlertRuleOut, "drop_percent">, trip: TripOut): string {
  const basis = BASIS_LABEL[effectiveRuleBasis(rule, trip)];
  if (rule.kind === "below_target") {
    const value = rule.threshold ?? trip.target_price;
    const src = rule.threshold === null ? tr(" (trip target)", " (여행 목표가)") : "";
    return value === null
      ? tr(
          `Price at or below the trip target (${basis}) — no target set yet`,
          `가격이 여행 목표가 이하 (${basis}) — 아직 목표가 미설정`,
        )
      : tr(`Price ≤ ${formatMoney(value, trip.currency)}${src}, ${basis}`, `가격 ≤ ${formatMoney(value, trip.currency)}${src}, ${basis}`);
  }
  if (rule.kind === "price_drop") return tr(
      `Price drops ≥ ${rule.drop_percent ?? "?"}% vs the previous check, ${basis}`,
      `직전 확인보다 ${rule.drop_percent ?? "?"}% 이상 하락, ${basis}`,
    );
  return tr(`A new lowest price is found, ${basis}`, `신규 최저가 발견, ${basis}`);
}
