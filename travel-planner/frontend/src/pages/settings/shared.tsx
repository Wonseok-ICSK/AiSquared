/** Helpers shared by the settings sections: save state, secret inputs, test results. */

import { useState } from "react";
import type { ReactNode } from "react";
import { CheckCircle2, KeyRound, Send, XCircle } from "lucide-react";
import { api } from "../../api/endpoints";
import { errorMessage } from "../../api/client";
import type {
  NotificationChannel,
  RuntimeSettingsIn,
  RuntimeSettingsOut,
  SecretState,
} from "../../api/types";
import { Badge, Button } from "../../components/ui";
import { useApp } from "../../lib/appContext";
import { getLang, tr } from "../../lib/i18n";

/** PUT /api/settings with busy/error/saved state. */
export function useSettingsSaver(onSaved: (s: RuntimeSettingsOut) => void): {
  save: (body: RuntimeSettingsIn) => Promise<boolean>;
  busy: boolean;
  error: string | null;
  saved: boolean;
} {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const save = async (body: RuntimeSettingsIn) => {
    setBusy(true);
    setError(null);
    setSaved(false);
    try {
      onSaved(await api.putSettings(body));
      setSaved(true);
      window.setTimeout(() => setSaved(false), 2500);
      return true;
    } catch (err) {
      setError(errorMessage(err));
      return false;
    } finally {
      setBusy(false);
    }
  };
  return { save, busy, error, saved };
}

/** "Saved" / error text next to a save button (polite live region). */
export function SaveStatus({ saved, error }: { saved: boolean; error: string | null }) {
  return (
    <span className="save-status" aria-live="polite">
      {saved && (
        <span className="ok-text">
          <CheckCircle2 size={14} aria-hidden /> {tr("Saved", "저장했어요")}
        </span>
      )}
      {error && (
        <span className="field__error" role="alert">
          {error}
        </span>
      )}
    </span>
  );
}

/** Badge describing whether a secret is set and where it comes from. */
export function SecretBadge({ state }: { state: SecretState }) {
  if (!state.is_set) return <Badge tone="outline">{tr("Not set", "미설정")}</Badge>;
  return (
    <Badge tone="good" icon={<KeyRound size={11} aria-hidden />}>
      {state.source === "env" ? tr("Set · from .env", "설정됨 · .env 파일") : tr("Set · saved here", "설정됨 · 여기에 저장")}
    </Badge>
  );
}

/**
 * Write-only secret input: the current value is never shown. Typing a value stages it in
 * `value`; `onClear` removes a UI-stored value ("" on PUT).
 */
export function SecretInput({
  id,
  label,
  state,
  value,
  onChange,
  onClear,
  clearing,
  describedBy,
  placeholder,
}: {
  id: string;
  label: string;
  state: SecretState;
  value: string;
  onChange: (v: string) => void;
  onClear?: () => void;
  clearing?: boolean;
  describedBy?: string;
  placeholder?: string;
}) {
  return (
    <div className="secret">
      <div className="row-between">
        <label className="field__label" htmlFor={id}>
          {label}
        </label>
        <SecretBadge state={state} />
      </div>
      <div className="row" style={{ flexWrap: "nowrap" }}>
        <input
          id={id}
          type="password"
          className="input mono"
          autoComplete="new-password"
          spellCheck={false}
          value={value}
          aria-describedby={describedBy}
          placeholder={
            placeholder ??
            (state.is_set
              ? tr("•••••••• (enter a new value to replace)", "•••••••• (바꾸려면 새 값을 입력하세요)")
              : tr("Not set", "미설정"))
          }
          onChange={(e) => onChange(e.target.value)}
        />
        {onClear && state.is_set && state.source === "ui" && (
          <Button size="sm" variant="danger" busy={clearing} onClick={onClear}>
            {tr("Clear", "지우기")}
          </Button>
        )}
      </div>
      {state.is_set && state.source === "env" && (
        <span className="field__hint">
          {tr(
            "Comes from the .env file; a value saved here overrides it.",
            ".env 파일에서 가져온 값이에요. 여기에 저장하면 그 값을 대신 사용해요.",
          )}
        </span>
      )}
    </div>
  );
}

/** Text for a test result ("skipped: <why>" / "failed: <reason>" from the dispatcher), translated in Korean. */
function testResultText(result: string): string {
  if (getLang() !== "ko") return result;
  const reason = (text: string) =>
    text === "disabled" ? "사용 안 함" : text === "not configured" ? "설정 안 됨" : text;
  if (result.startsWith("skipped:")) return `건너뜀: ${reason(result.slice(8).trim())}`;
  if (result.startsWith("failed:")) return `실패: ${result.slice(7).trim()}`;
  return result;
}

/** "Send test" button for a notification channel, with the result inline. */
export function TestSendButton({ channel, disabledReason }: { channel: NotificationChannel; disabledReason?: string }) {
  const { refreshUnread } = useApp();
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const run = async () => {
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      const r = await api.testNotification(channel);
      setResult(r.result);
      refreshUnread(); // a test may create an in-app notification
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };
  const ok = result === "sent";
  return (
    <span className="row" style={{ gap: 8 }}>
      <Button size="sm" icon={<Send size={14} aria-hidden />} busy={busy} onClick={() => void run()} title={disabledReason}>
        {tr("Send test", "테스트 보내기")}
      </Button>
      <span aria-live="polite" className="small">
        {result && (
          <span className={ok ? "ok-text" : result.startsWith("failed") ? "err-text" : "subtle"}>
            {ok ? <CheckCircle2 size={14} aria-hidden /> : result.startsWith("failed") ? <XCircle size={14} aria-hidden /> : null}{" "}
            {ok ? tr("Sent", "보냈어요") : testResultText(result)}
          </span>
        )}
        {error && <span className="err-text">{error}</span>}
      </span>
    </span>
  );
}

/** Card wrapper used by every settings section. */
export function SettingsCard({
  id,
  icon,
  title,
  desc,
  actions,
  children,
  footer,
}: {
  id?: string;
  icon?: ReactNode;
  title: string;
  desc?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  footer?: ReactNode;
}) {
  return (
    <section className="card settings-card" id={id} aria-labelledby={id ? `${id}-title` : undefined}>
      <div className="card__head settings-card__head">
        <div className="settings-card__heading">
          {icon && (
            <span className="settings-card__icon" aria-hidden>
              {icon}
            </span>
          )}
          <div className="stack-xs">
            <h2 id={id ? `${id}-title` : undefined} className="settings-card__title">
              {title}
            </h2>
            {desc && <span className="small subtle">{desc}</span>}
          </div>
        </div>
        {actions}
      </div>
      <div className="card__body stack">{children}</div>
      {footer && <div className="card__foot">{footer}</div>}
    </section>
  );
}
