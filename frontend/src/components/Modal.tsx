import { useEffect, useRef, type ReactNode, type Ref } from "react";
import { createPortal } from "react-dom";

// Every focusable control that should participate in the dialog's Tab cycle.
// Disabled controls are excluded so the trap never lands on an inert element.
const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), textarea:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])';

/**
 * The dialog's focusable controls, in document order.
 *
 * Controls inside a hidden or inert branch are left out: a tab panel that is
 * kept mounted for its unsaved form must still not be reachable by Tab, or the
 * trap would walk into fields the user cannot see.
 */
function focusableIn(container: HTMLElement): HTMLElement[] {
  return [...container.querySelectorAll<HTMLElement>(FOCUSABLE)].filter(
    (el) => !el.closest("[hidden], [inert]"),
  );
}

/**
 * Generic modal dialog.
 *
 * Rendered through a portal on <body> and laid out as a fixed backdrop with the
 * card centred inside it, so an ancestor's `overflow: hidden` (the composer, the
 * chat card) can never crop it.
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
  footerRef,
}: {
  open: boolean;
  onClose: () => void;
  title: string;
  /** Optional extra class applied to the card (e.g. `project-edit-modal`). */
  variant?: string;
  children?: ReactNode;
  actions?: ReactNode;
  /**
   * The actions row itself, for a child that owns its own buttons.
   *
   * A panel with its own form state must not have that state lifted into the
   * dialog just to render a busy-aware save button, so the row is published as
   * a container the panel can portal into. Passing this without ``actions``
   * still renders the row (empty), which is what the portal fills.
   */
  footerRef?: Ref<HTMLDivElement>;
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
    const card = cardRef.current;
    if (card) focusableIn(card)[0]?.focus();

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
      const focusables = focusableIn(container);
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

  // Portalled to <body>: the composers and cards that open dialogs clip their
  // own overflow, so a dialog rendered in place would be cropped by them.
  return createPortal(
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
        {/* Only the body scrolls: the title and the actions stay reachable. */}
        <div className="modal-body">{children}</div>
        {actions || footerRef ? (
          <div className="modal-actions" ref={footerRef}>
            {actions}
          </div>
        ) : null}
      </div>
    </div>,
    document.body,
  );
}
