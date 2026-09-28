import { useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

/**
 * The project a conversation belongs to, as the floating hint states it.
 * ``root`` is null for the default workspace, which has no path of its own.
 */
export interface ProjectHint {
  name: string;
  root: string | null;
}

/** How long the pointer rests on a row before its project is named. */
const HOVER_DELAY_MS = 500;

/** Gap between the row and the hint, and the hint and the window edge. */
const GAP = 8;

/**
 * Project hint for one conversation row, shown on hover or keyboard focus.
 *
 * It exists because a pinned conversation is listed away from its project: the
 * row alone no longer says where the conversation runs, and the row's own
 * ``title`` would otherwise have to carry both facts at once.
 *
 * The layer is not interactive (``pointer-events: none``): it must never take
 * the hover that opened it, and it must never take a click meant for the row.
 */
export function SessionProjectTooltip({
  anchor,
  title,
  hint,
}: {
  /** The row's open button: the trigger, and what the hint is placed beside. */
  anchor: HTMLElement | null;
  /** The conversation's full name, which the row itself may be ellipsing. */
  title: string;
  hint: ProjectHint;
}) {
  const id = useId();
  const layerRef = useRef<HTMLDivElement>(null);
  const timerRef = useRef<number | null>(null);
  const [open, setOpen] = useState(false);
  const [pos, setPos] = useState<{ left: number; top: number } | null>(null);

  useEffect(() => {
    if (!anchor) return;
    const stopTimer = () => {
      if (timerRef.current === null) return;
      window.clearTimeout(timerRef.current);
      timerRef.current = null;
    };
    const hide = () => {
      stopTimer();
      setOpen(false);
    };
    const show = () => {
      stopTimer();
      setOpen(true);
    };
    const onEnter = () => {
      stopTimer();
      timerRef.current = window.setTimeout(show, HOVER_DELAY_MS);
    };
    // Leaving, clicking (the row is being opened) and scrolling or resizing
    // (the row moves out from under the layer) all end the hint; so does
    // unmounting, through this cleanup.
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") hide();
    };
    anchor.addEventListener("pointerenter", onEnter);
    anchor.addEventListener("pointerleave", hide);
    anchor.addEventListener("focus", show);
    anchor.addEventListener("blur", hide);
    anchor.addEventListener("click", hide);
    anchor.addEventListener("keydown", onKey);
    window.addEventListener("scroll", hide, true);
    window.addEventListener("resize", hide);
    return () => {
      stopTimer();
      anchor.removeEventListener("pointerenter", onEnter);
      anchor.removeEventListener("pointerleave", hide);
      anchor.removeEventListener("focus", show);
      anchor.removeEventListener("blur", hide);
      anchor.removeEventListener("click", hide);
      anchor.removeEventListener("keydown", onKey);
      window.removeEventListener("scroll", hide, true);
      window.removeEventListener("resize", hide);
    };
  }, [anchor]);

  // The trigger, not the layer, is what a reader focuses: the description is
  // announced from there, while the layer stays presentational.
  useEffect(() => {
    if (!anchor || !open) return;
    anchor.setAttribute("aria-describedby", id);
    return () => anchor.removeAttribute("aria-describedby");
  }, [anchor, open, id]);

  useLayoutEffect(() => {
    if (!open) return;
    const layer = layerRef.current;
    if (!anchor || !layer) return;
    const a = anchor.getBoundingClientRect();
    const t = layer.getBoundingClientRect();
    // Preferred to the right of the row, and flipped to its left when the
    // window leaves no room there; clamped vertically so a row near an edge
    // still shows the whole hint.
    const right = a.right + GAP;
    const left = right + t.width <= window.innerWidth - GAP ? right : a.left - GAP - t.width;
    const top = Math.min(
      Math.max(GAP, a.top),
      Math.max(GAP, window.innerHeight - t.height - GAP),
    );
    setPos({ left: Math.max(GAP, left), top });
  }, [open, anchor, title, hint.name, hint.root]);

  if (!open || !anchor) return null;

  return createPortal(
    <div
      ref={layerRef}
      id={id}
      className="session-hint"
      role="tooltip"
      style={{ left: pos?.left ?? 0, top: pos?.top ?? 0, visibility: pos ? "visible" : "hidden" }}
    >
      <span className="session-hint-title">{title}</span>
      <span className="session-hint-project">{hint.name}</span>
      {hint.root ? <span className="session-hint-root">{hint.root}</span> : null}
    </div>,
    document.body,
  );
}
