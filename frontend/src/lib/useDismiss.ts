import { useEffect, useRef } from "react";
import type { RefObject } from "react";

/**
 * Close a popover when clicking outside `ref` or pressing Escape.
 *
 * `ref` may be a list: a popover rendered through a portal is not a DOM
 * descendant of its trigger, so both subtrees have to be named to stay
 * "inside".
 *
 * `active` scopes the listeners to the open state so closed popovers never
 * intercept document events. The callback is read through a ref so inline
 * arrow functions do not re-register the listeners on every render.
 */
export function useDismiss(
  ref: RefObject<HTMLElement | null> | ReadonlyArray<RefObject<HTMLElement | null>>,
  onDismiss: () => void,
  active = true,
): void {
  const callback = useRef(onDismiss);
  useEffect(() => {
    callback.current = onDismiss;
  }, [onDismiss]);

  useEffect(() => {
    if (!active) return;
    const refs = Array.isArray(ref) ? ref : [ref];
    const onDoc = (event: MouseEvent) => {
      const target = event.target as Node;
      // With nothing attached there is no inside to test against, so a click is
      // not evidence of anything and the popover stays up.
      if (refs.every((r) => !r.current)) return;
      if (refs.some((r) => r.current?.contains(target))) return;
      callback.current();
    };
    // Capture phase, so a popover inside a dialog sees Escape before the
    // dialog's own (bubble) listener: the innermost thing closes first.
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      callback.current();
    };
    document.addEventListener("mousedown", onDoc);
    window.addEventListener("keydown", onKey, true);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      window.removeEventListener("keydown", onKey, true);
    };
  }, [ref, active]);
}
