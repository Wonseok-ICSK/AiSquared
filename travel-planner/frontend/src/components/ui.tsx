/** Small presentational building blocks shared by every page. */

import { useId } from "react";
import type { ButtonHTMLAttributes, KeyboardEvent, ReactElement, ReactNode } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  CircleDashed,
  Info,
  Loader2,
  RefreshCw,
  XCircle,
} from "lucide-react";
import { errorMessage } from "../api/client";
import { labels, tr } from "../lib/i18n";

// ----------------------------------------------------------------------------- buttons

type ButtonVariant = "default" | "primary" | "ghost" | "danger";

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: "md" | "sm";
  icon?: ReactNode;
  busy?: boolean;
  iconOnly?: boolean;
}

/** Button with variants, optional leading icon and a busy spinner. */
export function Button({
  variant = "default",
  size = "md",
  icon,
  busy = false,
  iconOnly = false,
  className = "",
  children,
  disabled,
  type = "button",
  ...rest
}: ButtonProps) {
  const classes = [
    "btn",
    variant !== "default" ? `btn--${variant}` : "",
    size === "sm" ? "btn--sm" : "",
    iconOnly ? "btn--icon" : "",
    className,
  ]
    .filter(Boolean)
    .join(" ");
  return (
    <button
      type={type}
      className={classes}
      disabled={disabled || busy}
      aria-busy={busy || undefined}
      {...rest}
    >
      {busy ? <Loader2 size={size === "sm" ? 14 : 16} className="spin" aria-hidden /> : icon}
      {children}
    </button>
  );
}

/** Inline spinner with an accessible label. */
export function Spinner({ size = 16, label }: { size?: number; label?: string }) {
  return (
    <span role="status" className="spinner" aria-label={label ?? tr("Loading", "불러오는 중")}>
      <Loader2 size={size} aria-hidden />
    </span>
  );
}

// ----------------------------------------------------------------------------- badges & status

type Tone = "neutral" | "good" | "warn" | "bad" | "info" | "outline";

/** Small pill label. */
export function Badge({
  tone = "neutral",
  icon,
  children,
  title,
}: {
  tone?: Tone;
  icon?: ReactNode;
  children: ReactNode;
  title?: string;
}) {
  const cls = tone === "neutral" ? "badge" : `badge badge--${tone}`;
  return (
    <span className={cls} title={title}>
      {icon}
      {children}
    </span>
  );
}

export type ChipStatus =
  | "ok"
  | "partial"
  | "error"
  | "running"
  | "pending"
  | "done"
  | "none"
  | "skipped";

const STATUS_META: Record<ChipStatus, { tone: Tone; icon: ReactElement }> = {
  ok: { tone: "good", icon: <CheckCircle2 size={12} aria-hidden /> },
  done: { tone: "good", icon: <CheckCircle2 size={12} aria-hidden /> },
  partial: { tone: "warn", icon: <AlertTriangle size={12} aria-hidden /> },
  error: { tone: "bad", icon: <XCircle size={12} aria-hidden /> },
  running: { tone: "info", icon: <Loader2 size={12} className="spinner" aria-hidden /> },
  pending: { tone: "info", icon: <CircleDashed size={12} aria-hidden /> },
  none: { tone: "outline", icon: <CircleDashed size={12} aria-hidden /> },
  skipped: { tone: "neutral", icon: <CircleDashed size={12} aria-hidden /> },
};

const STATUS_LABEL = labels<ChipStatus>({
  ok: ["OK", "성공"],
  done: ["Ready", "완료"],
  partial: ["Partial", "일부 성공"],
  error: ["Error", "오류"],
  running: ["Running", "검색 중"],
  pending: ["Queued", "대기 중"],
  none: ["Not checked", "확인 전"],
  skipped: ["Skipped", "건너뜀"],
});

/** Status chip with icon + label (color never carries meaning alone). */
export function StatusChip({
  status,
  label,
  title,
}: {
  status: ChipStatus;
  label?: string;
  title?: string;
}) {
  const meta = STATUS_META[status];
  return (
    <Badge tone={meta.tone} icon={meta.icon} title={title}>
      {label ?? STATUS_LABEL[status]}
    </Badge>
  );
}

// ----------------------------------------------------------------------------- banners

type BannerTone = "error" | "warn" | "info" | "good";

const BANNER_ICON: Record<BannerTone, ReactElement> = {
  error: <XCircle size={18} aria-hidden />,
  warn: <AlertTriangle size={18} aria-hidden />,
  info: <Info size={18} aria-hidden />,
  good: <CheckCircle2 size={18} aria-hidden />,
};

/** Inline message box. Errors use role="alert"; others are polite live regions. */
export function Banner({
  tone = "info",
  title,
  children,
  action,
}: {
  tone?: BannerTone;
  title?: ReactNode;
  children?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className={`banner banner--${tone}`} role={tone === "error" ? "alert" : "status"}>
      <span className="banner__icon">{BANNER_ICON[tone]}</span>
      <div className="banner__body">
        {title && <span className="banner__title">{title}</span>}
        {children && <span>{children}</span>}
      </div>
      {action}
    </div>
  );
}

/** Error banner showing the backend `detail` message, with optional retry. */
export function ErrorBanner({
  error,
  title,
  onRetry,
}: {
  error: unknown;
  title?: string;
  onRetry?: () => void;
}) {
  if (!error) return null;
  return (
    <Banner
      tone="error"
      title={title ?? tr("Something went wrong", "문제가 발생했어요")}
      action={
        onRetry && (
          <Button size="sm" icon={<RefreshCw size={14} aria-hidden />} onClick={onRetry}>
            {tr("Retry", "다시 시도")}
          </Button>
        )
      }
    >
      {errorMessage(error)}
    </Banner>
  );
}

// ----------------------------------------------------------------------------- empty & skeleton

/** Friendly empty state with an icon, title, text and optional call-to-action. */
export function EmptyState({
  icon,
  title,
  children,
  action,
  compact = false,
}: {
  icon: ReactNode;
  title: string;
  children?: ReactNode;
  action?: ReactNode;
  compact?: boolean;
}) {
  return (
    <div className={compact ? "empty empty--compact" : "empty"}>
      <span className="empty__icon" aria-hidden>
        {icon}
      </span>
      <span className="empty__title">{title}</span>
      {children && <span className="small muted">{children}</span>}
      {action}
    </div>
  );
}

/** Shimmering placeholder block. */
export function Skeleton({
  width = "100%",
  height = 14,
  radius,
}: {
  width?: number | string;
  height?: number | string;
  radius?: number;
}) {
  return <span className="skel" style={{ width, height, borderRadius: radius }} aria-hidden />;
}

/** A few skeleton lines inside an aria-busy region. */
export function SkeletonBlock({ lines = 3, label }: { lines?: number; label?: string }) {
  return (
    <div className="stack-sm" aria-busy="true" aria-label={label ?? tr("Loading", "불러오는 중")} role="status">
      {Array.from({ length: lines }, (_, i) => (
        <Skeleton key={i} width={`${90 - i * 12}%`} height={14} />
      ))}
    </div>
  );
}

// ----------------------------------------------------------------------------- form fields

/** Label + control + hint + error, wired with ids for accessibility. */
export function Field({
  label,
  hint,
  error,
  required,
  className = "",
  children,
}: {
  label: ReactNode;
  hint?: ReactNode;
  error?: string | null;
  required?: boolean;
  className?: string;
  children: (ids: { id: string; describedBy: string | undefined; invalid: boolean }) => ReactNode;
}) {
  const id = useId();
  const hintId = `${id}-hint`;
  const errorId = `${id}-err`;
  const describedBy = [hint ? hintId : "", error ? errorId : ""].filter(Boolean).join(" ") || undefined;
  return (
    <div className={`field ${className}`}>
      <label className="field__label" htmlFor={id}>
        {label}
        {required && (
          <span className="req" aria-hidden>
            *
          </span>
        )}
      </label>
      {children({ id, describedBy, invalid: Boolean(error) })}
      {hint && (
        <span className="field__hint" id={hintId}>
          {hint}
        </span>
      )}
      {error && (
        <span className="field__error" id={errorId}>
          {error}
        </span>
      )}
    </div>
  );
}

/** Accessible on/off switch (a styled checkbox with role="switch"). */
export function Switch({
  checked,
  onChange,
  label,
  disabled,
  hideLabel = false,
  describedBy,
}: {
  checked: boolean;
  onChange: (value: boolean) => void;
  label: string;
  disabled?: boolean;
  hideLabel?: boolean;
  /** id of a hint explaining the switch (e.g. why it is disabled). */
  describedBy?: string;
}) {
  return (
    <label className="switch">
      <input
        type="checkbox"
        role="switch"
        checked={checked}
        disabled={disabled}
        aria-checked={checked}
        aria-describedby={describedBy}
        onChange={(e) => onChange(e.target.checked)}
      />
      <span className={hideLabel ? "sr-only" : "switch__label"}>{label}</span>
    </label>
  );
}

/** Checkbox with label and optional hint. */
export function Checkbox({
  checked,
  onChange,
  label,
  hint,
  disabled,
}: {
  checked: boolean;
  onChange: (value: boolean) => void;
  label: ReactNode;
  hint?: ReactNode;
  disabled?: boolean;
}) {
  return (
    <label className="check">
      <input
        type="checkbox"
        checked={checked}
        disabled={disabled}
        onChange={(e) => onChange(e.target.checked)}
      />
      <span className="check__text">
        <span>{label}</span>
        {hint && <span className="check__hint">{hint}</span>}
      </span>
    </label>
  );
}

/** Single-choice segmented control (radio group semantics). */
export function Segmented<T extends string | number>({
  value,
  options,
  onChange,
  label,
  block = false,
  disabled = false,
}: {
  value: T;
  options: readonly { value: T; label: ReactNode }[];
  onChange: (value: T) => void;
  label: string;
  block?: boolean;
  disabled?: boolean;
}) {
  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    if (disabled) return;
    const index = options.findIndex((o) => o.value === value);
    const delta = e.key === "ArrowRight" || e.key === "ArrowDown" ? 1 : e.key === "ArrowLeft" || e.key === "ArrowUp" ? -1 : 0;
    if (!delta) return;
    e.preventDefault();
    const next = (index + delta + options.length) % options.length;
    onChange(options[next].value);
    (e.currentTarget.children[next] as HTMLElement | undefined)?.focus();
  };
  return (
    <div
      className={block ? "segmented segmented--block" : "segmented"}
      role="radiogroup"
      aria-label={label}
      aria-disabled={disabled || undefined}
      onKeyDown={onKeyDown}
    >
      {options.map((opt) => (
        <button
          key={String(opt.value)}
          type="button"
          role="radio"
          aria-checked={opt.value === value}
          tabIndex={opt.value === value ? 0 : -1}
          disabled={disabled}
          onClick={() => onChange(opt.value)}
        >
          {opt.label}
        </button>
      ))}
    </div>
  );
}
