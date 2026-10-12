/** Alerts tab: CRUD of alert rules (below target, price drop %, new low) with basis. */

import { useState } from "react";
import type { FormEvent } from "react";
import { BellPlus, BellRing, Pencil, RotateCcw, Trash2 } from "lucide-react";
import { api } from "../../api/endpoints";
import { errorMessage } from "../../api/client";
import type { AlertKind, AlertRuleCreate, AlertRuleOut, PriceBasis, TripOut } from "../../api/types";
import { ALERT_KINDS } from "../../api/types";
import {
  Badge,
  Banner,
  Button,
  Checkbox,
  EmptyState,
  ErrorBanner,
  Field,
  Segmented,
  SkeletonBlock,
  Switch,
} from "../../components/ui";
import { describeRule } from "../../lib/alerts";
import { ALERT_KIND_LABEL, BASIS_LABEL, formatMoney, formatUtc } from "../../lib/format";
import { useAsync } from "../../lib/hooks";
import { labels, tr } from "../../lib/i18n";
import { parseNumber } from "../../lib/validation";

interface RuleDraft {
  kind: AlertKind;
  enabled: boolean;
  threshold: string;
  drop_percent: string;
  price_basis: PriceBasis;
}

const KIND_HELP = labels<AlertKind>({
  below_target: [
    "Notify when the best matching price is at or below a threshold.",
    "조건에 맞는 최저가가 기준 금액 이하가 되면 알려드려요.",
  ],
  price_drop: [
    "Notify when the price falls by at least a percentage since the previous check.",
    "이전 확인보다 가격이 지정한 비율 이상 내려가면 알려드려요.",
  ],
  new_low: ["Notify when a check finds a new lowest price for this trip.", "이 여행의 역대 최저가가 새로 나오면 알려드려요."],
});

function draftFrom(rule: AlertRuleOut | null, trip: TripOut): RuleDraft {
  return {
    kind: rule?.kind ?? "below_target",
    enabled: rule?.enabled ?? true,
    threshold: rule?.threshold !== null && rule?.threshold !== undefined ? String(rule.threshold) : "",
    drop_percent: rule?.drop_percent !== null && rule?.drop_percent !== undefined ? String(rule.drop_percent) : "10",
    price_basis: rule?.price_basis ?? trip.price_basis,
  };
}

function validateDraft(d: RuleDraft): Record<string, string> {
  const errors: Record<string, string> = {};
  if (d.kind === "below_target") {
    const t = parseNumber(d.threshold);
    if (t !== null && (Number.isNaN(t) || t < 0)) errors.threshold = tr("Enter an amount of 0 or more", "0 이상의 금액을 입력하세요");
  }
  if (d.kind === "price_drop") {
    const p = parseNumber(d.drop_percent);
    if (p === null) errors.drop_percent = tr("Required for a price-drop rule", "가격 하락 규칙에는 하락률이 필요해요");
    else if (Number.isNaN(p) || p <= 0 || p > 100) errors.drop_percent = tr("Between 0 and 100 (exclusive of 0)", "0보다 크고 100 이하인 값을 입력하세요");
  }
  return errors;
}

/** Whether the draft is a below-target rule following the trip target (blank threshold). */
function draftUsesTripTarget(d: RuleDraft): boolean {
  return d.kind === "below_target" && d.threshold.trim() === "";
}

function toPayload(d: RuleDraft, trip: TripOut): AlertRuleCreate {
  return {
    kind: d.kind,
    enabled: d.enabled,
    threshold: d.kind === "below_target" ? parseNumber(d.threshold) : null,
    drop_percent: d.kind === "price_drop" ? parseNumber(d.drop_percent) : null,
    // the trip target is compared in the trip's basis (services/alerts.py), so store that one
    price_basis: draftUsesTripTarget(d) ? trip.price_basis : d.price_basis,
  };
}

function RuleForm({
  trip,
  rule,
  onSaved,
  onCancel,
}: {
  trip: TripOut;
  rule: AlertRuleOut | null;
  onSaved: (rule: AlertRuleOut) => void;
  onCancel?: () => void;
}) {
  const [draft, setDraft] = useState<RuleDraft>(() => draftFrom(rule, trip));
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const set = <K extends keyof RuleDraft>(k: K, v: RuleDraft[K]) => setDraft((d) => ({ ...d, [k]: v }));
  const followsTarget = draftUsesTripTarget(draft);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const errs = validateDraft(draft);
    setErrors(errs);
    if (Object.keys(errs).length > 0) return;
    setSaving(true);
    setError(null);
    try {
      const payload = toPayload(draft, trip);
      const saved = rule ? await api.updateAlert(rule.id, payload) : await api.createAlert(trip.id, payload);
      onSaved(saved);
      if (!rule) setDraft(draftFrom(null, trip));
    } catch (err) {
      setError(err);
    } finally {
      setSaving(false);
    }
  };

  return (
    <form className="stack-sm" onSubmit={submit} noValidate aria-label={rule ? tr("Edit alert rule", "알림 규칙 수정") : tr("New alert rule", "새 알림 규칙")}>
      <div className="form-grid">
        <Field label={tr("Alert when", "알림 조건")} className="span-12" hint={KIND_HELP[draft.kind]}>
          {() => (
            <Segmented
              label={tr("Alert kind", "알림 종류")}
              value={draft.kind}
              onChange={(v) => set("kind", v)}
              options={ALERT_KINDS.map((k) => ({ value: k, label: ALERT_KIND_LABEL[k] }))}
            />
          )}
        </Field>
        {draft.kind === "below_target" && (
          <Field
            label={tr("Threshold", "기준 금액")}
            className="span-4"
            error={errors.threshold}
            hint={tr(
              `Blank = trip target (${formatMoney(trip.target_price, trip.currency)})`,
              `비워 두면 여행 목표 가격(${formatMoney(trip.target_price, trip.currency)})을 사용해요`,
            )}
          >
            {({ id, describedBy, invalid }) => (
              <div className="input-group">
                <span className="addon">{trip.currency}</span>
                <input
                  id={id}
                  className="input input--num"
                  inputMode="decimal"
                  value={draft.threshold}
                  placeholder={trip.target_price !== null ? String(trip.target_price) : ""}
                  aria-describedby={describedBy}
                  aria-invalid={invalid || undefined}
                  onChange={(e) => set("threshold", e.target.value)}
                />
              </div>
            )}
          </Field>
        )}
        {draft.kind === "price_drop" && (
          <Field label={tr("Drop of at least", "최소 하락률")} className="span-4" error={errors.drop_percent}>
            {({ id, describedBy, invalid }) => (
              <div className="input-group">
                <input
                  id={id}
                  className="input input--num"
                  inputMode="decimal"
                  value={draft.drop_percent}
                  aria-describedby={describedBy}
                  aria-invalid={invalid || undefined}
                  onChange={(e) => set("drop_percent", e.target.value)}
                />
                <span className="addon">%</span>
              </div>
            )}
          </Field>
        )}
        <Field
          label={tr("Compare prices", "가격 비교 기준")}
          className="span-4"
          hint={
            followsTarget
              ? tr(
                  `Uses the trip target's basis (${BASIS_LABEL[trip.price_basis]}). Enter a threshold to choose another.`,
                  `여행 목표 가격의 기준(${BASIS_LABEL[trip.price_basis]})을 따라요. 다른 기준을 쓰려면 기준 금액을 입력하세요.`,
                )
              : undefined
          }
        >
          {() => (
            <Segmented
              label={tr("Price basis", "가격 기준")}
              value={followsTarget ? trip.price_basis : draft.price_basis}
              onChange={(v) => set("price_basis", v)}
              disabled={followsTarget}
              options={[
                { value: "per_person", label: tr("Per person", "1인") },
                { value: "total", label: tr("Total", "총액") },
              ]}
            />
          )}
        </Field>
        <div className="span-4 field" style={{ justifyContent: "flex-end", paddingBottom: 8 }}>
          <Checkbox checked={draft.enabled} onChange={(v) => set("enabled", v)} label={tr("Enabled", "사용")} />
        </div>
      </div>
      <ErrorBanner error={error} title={tr("Could not save the rule", "규칙을 저장하지 못했어요")} />
      <div className="row" style={{ justifyContent: "flex-end" }}>
        {onCancel && (
          <Button variant="ghost" onClick={onCancel}>
            {tr("Cancel", "취소")}
          </Button>
        )}
        <Button type="submit" variant="primary" busy={saving} icon={rule ? undefined : <BellPlus size={16} aria-hidden />}>
          {rule ? tr("Save rule", "규칙 저장") : tr("Add rule", "규칙 추가")}
        </Button>
      </div>
    </form>
  );
}

function RuleCard({
  rule,
  trip,
  onChange,
  onDelete,
}: {
  rule: AlertRuleOut;
  trip: TripOut;
  onChange: (r: AlertRuleOut) => void;
  onDelete: (id: number) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const patch = async (body: { enabled?: boolean; armed?: boolean }) => {
    setBusy(true);
    setError(null);
    try {
      onChange(await api.updateAlert(rule.id, body));
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const remove = async () => {
    if (!window.confirm(tr(`Delete the “${ALERT_KIND_LABEL[rule.kind]}” rule?`, `“${ALERT_KIND_LABEL[rule.kind]}” 규칙을 삭제할까요?`))) return;
    setBusy(true);
    try {
      await api.deleteAlert(rule.id);
      onDelete(rule.id);
    } catch (err) {
      setError(errorMessage(err));
      setBusy(false);
    }
  };

  return (
    <li className={`rule${rule.enabled ? "" : " is-paused"}`}>
      <div className="rule__row">
        <div className="rule__summary">
          <span className="rule__icon" aria-hidden>
            <BellRing size={16} />
          </span>
          <div className="stack-xs grow">
            <span className="rule__title">
              <span className="strong">{ALERT_KIND_LABEL[rule.kind]}</span>
              {!rule.enabled && <Badge tone="outline">{tr("Off", "꺼짐")}</Badge>}
              {rule.kind === "below_target" &&
                (rule.armed ? (
                  <Badge
                    tone="info"
                    title={tr(
                      "Will notify the next time the price is at or below the threshold",
                      "다음에 가격이 기준 금액 이하가 되면 알려드려요",
                    )}
                  >
                    {tr("Armed", "감시 중")}
                  </Badge>
                ) : (
                  <Badge
                    tone="outline"
                    title={tr(
                      "Already notified; re-arms when the price goes back above the threshold",
                      "이미 알림을 보냈어요. 가격이 기준 금액 위로 다시 올라가면 재설정돼요",
                    )}
                  >
                    {tr("Fired · waiting to re-arm", "알림 발송됨 · 재설정 대기")}
                  </Badge>
                ))}
            </span>
            <span className="small muted">{describeRule(rule, trip)}</span>
            <span className="xsmall subtle">
              {rule.last_triggered_at
                ? tr(
                    `Last triggered ${formatUtc(rule.last_triggered_at)}${
                      rule.last_triggered_price !== null ? ` at ${formatMoney(rule.last_triggered_price, trip.currency)}` : ""
                    }`,
                    `마지막 알림 ${formatUtc(rule.last_triggered_at)}${
                      rule.last_triggered_price !== null ? ` · ${formatMoney(rule.last_triggered_price, trip.currency)}` : ""
                    }`,
                  )
                : tr("Never triggered", "아직 알림 없음")}
            </span>
          </div>
        </div>
        <div className="rule__controls">
          <Switch checked={rule.enabled} disabled={busy} onChange={(v) => void patch({ enabled: v })} label={tr("Enabled", "사용")} hideLabel />
          {rule.kind === "below_target" && !rule.armed && (
            <Button size="sm" variant="ghost" icon={<RotateCcw size={14} aria-hidden />} disabled={busy} onClick={() => void patch({ armed: true })}>
              {tr("Re-arm", "재설정")}
            </Button>
          )}
          <Button
            size="sm"
            variant="ghost"
            iconOnly
            aria-label={tr("Edit rule", "규칙 수정")}
            title={tr("Edit rule", "규칙 수정")}
            aria-expanded={editing}
            icon={<Pencil size={14} aria-hidden />}
            onClick={() => setEditing((v) => !v)}
          />
          <Button
            size="sm"
            variant="ghost"
            className="detail-head__delete"
            iconOnly
            aria-label={tr("Delete rule", "규칙 삭제")}
            title={tr("Delete rule", "규칙 삭제")}
            icon={<Trash2 size={14} aria-hidden />}
            disabled={busy}
            onClick={() => void remove()}
          />
        </div>
      </div>
      {error && (
        <span className="field__error rule__error" role="alert">
          {error}
        </span>
      )}
      {editing && (
        <div className="rule__edit">
          <RuleForm
            trip={trip}
            rule={rule}
            onCancel={() => setEditing(false)}
            onSaved={(r) => {
              onChange(r);
              setEditing(false);
            }}
          />
        </div>
      )}
    </li>
  );
}

/** Alerts tab. */
export function AlertsTab({ trip }: { trip: TripOut }) {
  const rules = useAsync(() => api.listAlerts(trip.id), [trip.id]);
  const list = rules.data ?? [];
  const replace = (r: AlertRuleOut) => rules.setData((prev) => (prev ?? []).map((x) => (x.id === r.id ? r : x)));

  const active = list.filter((r) => r.enabled).length;

  return (
    <div className="stack">
      <ErrorBanner error={rules.error} title={tr("Could not load alert rules", "알림 규칙을 불러오지 못했어요")} onRetry={() => void rules.reload()} />
      {trip.target_price === null && (
        <Banner tone="info">
          {tr(
            "This trip has no target price. “Below target” rules need a threshold or a trip target.",
            "이 여행에는 목표 가격이 없어요. “목표가 이하” 규칙을 쓰려면 기준 금액이나 여행 목표 가격이 필요해요.",
          )}
        </Banner>
      )}
      <section className="tab-section" aria-labelledby="alert-rules-title">
        <div className="tab-section__head">
          <h2 id="alert-rules-title">{tr("Alert rules", "알림 규칙")}</h2>
          {rules.data && list.length > 0 && (
            <span className="tab-section__note">
              {tr(`${active} of ${list.length} on`, `${list.length}개 중 ${active}개 사용 중`)}
            </span>
          )}
        </div>
        {rules.loading && !rules.data && (
          <div className="card card__body">
            <SkeletonBlock lines={4} label={tr("Loading alert rules", "알림 규칙 불러오는 중")} />
          </div>
        )}
        {rules.data && list.length === 0 && (
          <div className="card">
            <EmptyState compact icon={<BellPlus size={24} />} title={tr("No alert rules", "알림 규칙이 없어요")}>
              {tr("Add a rule below to get notified about price changes.", "아래에서 규칙을 추가하면 가격이 바뀔 때 알려드려요.")}
            </EmptyState>
          </div>
        )}
        {list.length > 0 && (
          <div className="card">
            <ul className="rule-list">
              {list.map((rule) => (
                <RuleCard
                  key={rule.id}
                  rule={rule}
                  trip={trip}
                  onChange={replace}
                  onDelete={(id) => rules.setData((prev) => (prev ?? []).filter((x) => x.id !== id))}
                />
              ))}
            </ul>
          </div>
        )}
      </section>
      <section className="card rule-new">
        <div className="card__head">
          <h3>{tr("Add an alert rule", "알림 규칙 추가")}</h3>
        </div>
        <div className="card__body">
          <RuleForm trip={trip} rule={null} onSaved={(r) => rules.setData((prev) => [...(prev ?? []), r])} />
        </div>
      </section>
      <p className="rule-foot">
        {tr(
          "Alerts are delivered to every enabled notification channel (Settings → Notification channels).",
          "알림은 사용 중인 모든 알림 채널로 전송돼요 (설정 → 알림 채널).",
        )}
      </p>
    </div>
  );
}
