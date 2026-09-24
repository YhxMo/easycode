import type { ReactNode } from "react";

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
  children?: ReactNode;
}

export function RightPane({ open, sections, active, onSelect, onClose, children }: Props) {
  if (!open) return null;
  const index = Math.max(
    0,
    sections.findIndex((s) => s.id === active),
  );
  const step = (delta: number) => {
    const next = sections[(index + delta + sections.length) % sections.length];
    if (next) onSelect(next.id);
  };
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
            aria-label="上一个分区"
            title="上一个分区"
            onClick={() => step(-1)}
          >
            <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
              <path d="M14.5 5.5 8 12l6.5 6.5" />
            </svg>
          </button>
          <button
            type="button"
            className="icon-btn"
            aria-label="下一个分区"
            title="下一个分区"
            onClick={() => step(1)}
          >
            <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
              <path d="M9.5 5.5 16 12l-6.5 6.5" />
            </svg>
          </button>
          <button
            type="button"
            className="icon-btn"
            aria-label="关闭面板"
            title="关闭面板"
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
