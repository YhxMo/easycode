import { useCallback, useEffect, useState } from "react";

/**
 * The sidebar drawer on narrow windows.
 *
 * Below 760px the sidebar is off-canvas, so it needs its own open state, an
 * overlay that closes it, and Escape to leave it. The Escape listener is scoped
 * to while it is open: the desktop layout and the composer's own Escape must not
 * be affected by a drawer nobody opened.
 */
export function useSidebarDrawer() {
  const [open, setOpen] = useState(false);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);

  return { open, setOpen, close: useCallback(() => setOpen(false), []) };
}
