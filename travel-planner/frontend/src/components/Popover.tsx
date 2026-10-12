/**
 * Anchored popover rendered in a portal (so scrolling table wrappers cannot clip it), placed
 * below the trigger or above it when there is no room; a bottom sheet on small screens.
 * Closes on Escape, outside click, or scroll of the page.
 */

import { useCallback, useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import type { CSSProperties, ReactNode } from "react";
import { createPortal } from "react-dom";

interface PopoverProps {
  /** Renders the trigger; spread `triggerProps` on a button. */
  trigger: (triggerProps: {
    onClick: () => void;
    "aria-expanded": boolean;
    "aria-controls": string;
    ref: (el: HTMLElement | null) => void;
  }) => ReactNode;
  children: (close: () => void) => ReactNode;
  label: string;
  width?: number;
}

const GAP = 6;
const MOBILE_QUERY = "(max-width: 720px)";

/** A trigger plus a non-modal dialog positioned next to it. */
export function Popover({ trigger, children, label, width = 420 }: PopoverProps) {
  const [open, setOpen] = useState(false);
  const [style, setStyle] = useState<CSSProperties>({});
  const triggerRef = useRef<HTMLElement | null>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const id = useId();

  const close = useCallback(() => {
    setOpen(false);
    triggerRef.current?.focus();
  }, []);

  const place = useCallback(() => {
    const anchor = triggerRef.current;
    const panel = panelRef.current;
    if (!anchor || !panel) return;
    if (window.matchMedia(MOBILE_QUERY).matches) {
      setStyle({});
      return;
    }
    const rect = anchor.getBoundingClientRect();
    const w = Math.min(width, window.innerWidth - 32);
    const h = panel.offsetHeight;
    const left = Math.max(16, Math.min(rect.right - w, window.innerWidth - w - 16));
    const below = rect.bottom + GAP;
    const top = below + h > window.innerHeight - 8 && rect.top - GAP - h > 8 ? rect.top - GAP - h : below;
    setStyle({ top, left, width: w });
  }, [width]);

  useLayoutEffect(() => {
    if (open) place();
  }, [open, place]);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      const target = e.target as Node;
      if (panelRef.current?.contains(target) || triggerRef.current?.contains(target)) return;
      setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") close();
    };
    const onScroll = (e: Event) => {
      if (panelRef.current && e.target instanceof Node && panelRef.current.contains(e.target)) return;
      setOpen(false);
    };
    const observer = new ResizeObserver(place);
    if (panelRef.current) observer.observe(panelRef.current);
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    window.addEventListener("scroll", onScroll, true);
    window.addEventListener("resize", place);
    panelRef.current?.focus();
    return () => {
      observer.disconnect();
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
      window.removeEventListener("scroll", onScroll, true);
      window.removeEventListener("resize", place);
    };
  }, [open, close, place]);

  return (
    <>
      {trigger({
        onClick: () => setOpen((o) => !o),
        "aria-expanded": open,
        "aria-controls": id,
        ref: (el) => {
          triggerRef.current = el;
        },
      })}
      {open &&
        createPortal(
          <div className="popover" id={id} role="dialog" aria-label={label} tabIndex={-1} ref={panelRef} style={style}>
            {children(close)}
          </div>,
          document.body,
        )}
    </>
  );
}
