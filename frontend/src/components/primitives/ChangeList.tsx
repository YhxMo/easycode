import { useState, type ReactNode } from "react";
import type { ChangeRow } from "../../lib/pane";
import { DiffBody } from "./DiffView";

function Caret({ open }: { open: boolean }) {
  return (
    <span className={`change-caret${open ? " open" : ""}`} aria-hidden="true">
      ▸
    </span>
  );
}

/**
 * The change section's file rows: counts and state on the line, the diff below
 * the one row the reader opens.
 *
 * Counts come before content on purpose — which files moved, and by how much,
 * is what a reader scans for; the diff of one file is what they open next. A
 * row git cannot describe in lines (binary, deleted) keeps its counts and says
 * why there is nothing to open, instead of offering an empty preview.
 */
export function ChangeList({ rows }: { rows: ChangeRow[] }) {
  const [open, setOpen] = useState<string[]>([]);
  const toggle = (key: string) =>
    setOpen((prev) => (prev.includes(key) ? prev.filter((k) => k !== key) : [...prev, key]));

  if (!rows.length) return null;
  return (
    <div className="change-list">
      {rows.map((row) => {
        const expanded = open.includes(row.key);
        const body: ReactNode = (
          <>
            {row.diff && <Caret open={expanded} />}
            <span className="change-path">{row.label}</span>
            {row.rootLabel && <span className="ctx-root">{row.rootLabel}</span>}
            {row.meta && <span className="change-meta">{row.meta}</span>}
            <span className="change-stats tabular">
              <span className="add">+{row.added}</span>
              <span className="del">−{row.removed}</span>
            </span>
          </>
        );
        return (
          <div key={row.key} className={`change-item${expanded ? " open" : ""}`}>
            {row.diff ? (
              <button
                type="button"
                className="change-row"
                title={`${row.label} +${row.added} −${row.removed}`}
                aria-expanded={expanded}
                onClick={() => toggle(row.key)}
              >
                {body}
              </button>
            ) : (
              // Nothing to open: the row states what it knows and why.
              <div className="change-row static" title={row.label}>
                {body}
              </div>
            )}
            {expanded && row.diff && <DiffBody diff={row.diff} />}
            {!row.diff && row.note && <p className="change-note">{row.note}</p>}
          </div>
        );
      })}
    </div>
  );
}
