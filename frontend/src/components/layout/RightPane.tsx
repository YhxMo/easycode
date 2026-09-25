import { useEffect, useRef, type ReactNode } from "react";

export interface PaneSection {
  id: string;
  label: string;
}

interface Props {
  open: boolean;
  sections: PaneSection[];
  active: string;
  onSelect: (id: string) => void;
  onClose: () => void;
  /** True when the pane covers the conversation instead of sitting beside it. */
  overlay?: boolean;
  children?: ReactNode;
}

export function RightPane({
  open,
  sections,
  active,
  onSelect,
  onClose,
  overlay = false,
  children,
}: Props) {
  const closeRef = useRef<HTMLButtonElement>(null);
  // A drawer hides the conversation behind it, so the keyboard has to follow:
  // opening it puts focus on its own first control.
  useEffect(() => {
    if (open && overlay) closeRef.current?.focus();
  }, [open, overlay]);

  if (!open) return null;
  return (
    <aside className="right-pane" aria-label="任务面板">
      <header className="pane-head">
        <div className="pane-tabs" role="tablist" aria-label="面板分区">
          {sections.map((s) => (
            <button
              key={s.id}
              type="button"
              role="tab"
              aria-selected={s.id === active}
              className={`pane-tab${s.id === active ? " active" : ""}`}
              onClick={() => onSelect(s.id)}
            >
              {s.label}
            </button>
          ))}
        </div>
        <div className="pane-actions">
          <button
            type="button"
            className="icon-btn"
            aria-label="关闭面板"
            title="关闭面板"
            ref={closeRef}
            onClick={onClose}
          >
            <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
              <path d="M6 6l12 12M18 6L6 18" />
            </svg>
          </button>
        </div>
      </header>
      <div className="pane-body">{children}</div>
    </aside>
  );
}
