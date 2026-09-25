import type { ReactNode } from "react";

/**
 * One directory row in the sidebar's directory area: the draft's editable main
 * directory, an open session's fixed one and the secondary list all use this
 * shell, so their geometry never depends on which session is on screen.
 */
export function DirectoryCard({
  icon,
  name,
  detail,
  title,
  trailing,
  expanded,
  disabled = false,
  popup = false,
  onClick,
}: {
  icon: ReactNode;
  /** Short label: project name, "默认工作区" or "次目录". */
  name: string;
  /** One-line, ellipsised detail; the full text lives in the title tooltip. */
  detail: string;
  title: string;
  /** Trailing actions of the row (a menu button, a copy button, …). */
  trailing?: ReactNode;
  expanded?: boolean;
  disabled?: boolean;
  /** Whether the main button unfolds a listbox/detail panel. */
  popup?: boolean;
  onClick?: () => void;
}) {
  return (
    <div className={`dir-card${expanded ? " open" : ""}`}>
      <button
        type="button"
        className="dir-card-main"
        title={title}
        disabled={disabled}
        aria-expanded={popup ? Boolean(expanded) : undefined}
        aria-haspopup={popup ? "listbox" : undefined}
        onClick={onClick}
      >
        <span className="dir-icon" aria-hidden="true">
          {icon}
        </span>
        <span className="dir-copy">
          <strong>{name}</strong>
          <small>{detail}</small>
        </span>
        {popup && (
          <span className="dir-caret" aria-hidden="true">
            ⌄
          </span>
        )}
      </button>
      {trailing && <div className="dir-actions">{trailing}</div>}
    </div>
  );
}
