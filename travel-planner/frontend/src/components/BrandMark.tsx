/** The app's mark — a sun rising over the horizon — and the "assistant note" built on it:
 * a short first-hand summary set beside the mark, the way an assistant would say it. */

import type { ReactNode } from "react";

/** Sunrise mark in the accent color (decorative unless `title` is given). */
export function BrandMark({ size = 24, title, className }: { size?: number; title?: string; className?: string }) {
  return (
    <svg
      className={`brand-mark${className ? ` ${className}` : ""}`}
      width={size}
      height={size}
      viewBox="0 0 32 32"
      role={title ? "img" : undefined}
      aria-label={title}
      aria-hidden={title ? undefined : true}
      focusable="false"
    >
      <path d="M9 24a7 7 0 0 1 14 0z" fill="currentColor" />
      <g stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" fill="none">
        <path d="M16 14.2v-3.4" />
        <path d="M22.9 17.1l2.4-2.4" />
        <path d="M9.1 17.1l-2.4-2.4" />
        <path d="M25.7 21.4l3.1-.8" />
        <path d="M6.3 21.4l-3.1-.8" />
        <path d="M4 26.6h24" />
      </g>
    </svg>
  );
}

/**
 * A summary in the app's own voice: the mark, then one or two plain sentences.
 * `tone` tints the mark (good news, a caution, or neutral).
 */
export function AssistantNote({
  children,
  title,
  tone = "neutral",
  action,
  className,
}: {
  children: ReactNode;
  title?: ReactNode;
  tone?: "neutral" | "good" | "warn";
  action?: ReactNode;
  className?: string;
}) {
  return (
    <div className={`assistant-note assistant-note--${tone}${className ? ` ${className}` : ""}`}>
      <BrandMark size={22} className="assistant-note__mark" />
      <div className="assistant-note__body">
        {title && <p className="assistant-note__title">{title}</p>}
        <div className="assistant-note__text">{children}</div>
      </div>
      {action && <div className="assistant-note__action">{action}</div>}
    </div>
  );
}
