import { useEffect, useRef } from "react";
import type { RefObject } from "react";

/**
 * Close a popover when clicking outside `ref` or pressing Escape.
 *
 * `active` scopes the listeners to the open state so closed popovers never
 * intercept document events. The callback is read through a ref so inline
 * arrow functions do not re-register the listeners on every render.
 */
export function useDismiss(
  ref: RefObject<HTMLElement | null>,
  onDismiss: () => void,
  active = true,
): void {
  const callback = useRef(onDismiss);
  useEffect(() => {
    callback.current = onDismiss;
  }, [onDismiss]);

  useEffect(() => {
    if (!active) return;
    const onDoc = (event: MouseEvent) => {
      if (ref.current && !ref.current.contains(event.target as Node)) callback.current();
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
