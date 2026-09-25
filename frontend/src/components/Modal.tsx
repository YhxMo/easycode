import { useEffect, useRef, type ReactNode } from "react";

// Every focusable control that should participate in the dialog's Tab cycle.
// Disabled controls are excluded so the trap never lands on an inert element.
const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), textarea:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])';

/**
 * Generic modal dialog.
 *
 * Provides the WAI-ARIA Dialog (Modal) contract that the previous inline
 * `.modal-backdrop` divs lacked:
 *   - `role="dialog"` + `aria-modal="true"` + `aria-label`
 *   - Escape closes the dialog on the window keydown listener
 *   - a simple focus trap: Tab / Shift+Tab cycle between the first and last
 *     focusable controls inside the card
 *   - focus moves to the first focusable control on open
 *   - focus is restored to the element that owned focus before the dialog
 *     opened when it closes
 */
export function Modal({
  open,
  onClose,
  title,
  variant,
  children,
  actions,
}: {
  open: boolean;
  onClose: () => void;
  title: string;
  /** Optional extra class applied to the card (e.g. `project-edit-modal`). */
  variant?: string;
  children?: ReactNode;
  actions?: ReactNode;
}) {
  const cardRef = useRef<HTMLDivElement>(null);
  // Keep the latest onClose in a ref so the keydown effect can be stable (only
  // depends on `open`) without re-attaching on every render.
  const onCloseRef = useRef(onClose);
  useEffect(() => {
    onCloseRef.current = onClose;
  }, [onClose]);
  // Remember the control that opened the dialog so focus can be returned to it.
  const restoreRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    if (!open) return;

    // Record the trigger and move focus into the dialog.
    restoreRef.current = (document.activeElement as HTMLElement | null) ?? null;
    const first = cardRef.current?.querySelector<HTMLElement>(FOCUSABLE);
    first?.focus();

    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        // A popover inside the dialog closes first and claims the key, so one
        // Escape never closes two layers at once.
        if (!e.defaultPrevented) onCloseRef.current();
        return;
      }
      if (e.key !== "Tab") return;
      const container = cardRef.current;
      if (!container) return;
      const focusables = Array.from(container.querySelectorAll<HTMLElement>(FOCUSABLE));
      if (focusables.length === 0) {
        e.preventDefault();
        return;
      }
      const firstEl = focusables[0];
      const lastEl = focusables[focusables.length - 1];
      const active = document.activeElement as HTMLElement | null;
      if (e.shiftKey) {
        // Shift+Tab at the start (or focus escaped the card) wraps to the end.
        if (active === firstEl || !container.contains(active)) {
          e.preventDefault();
          lastEl.focus();
        }
      } else {
        // Tab at the end (or focus escaped the card) wraps to the start.
        if (active === lastEl || !container.contains(active)) {
          e.preventDefault();
          firstEl.focus();
        }
      }
    };

    window.addEventListener("keydown", onKeyDown);
    return () => {
      window.removeEventListener("keydown", onKeyDown);
      // Restore focus to the element that owned it before the dialog opened.
      restoreRef.current?.focus?.();
      restoreRef.current = null;
    };
  }, [open]);

  if (!open) return null;

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div
        ref={cardRef}
        className={`modal ${variant ?? ""}`.trim()}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal-title">{title}</div>
        {children}
        {actions ? <div className="modal-actions">{actions}</div> : null}
      </div>
    </div>
  );
}
