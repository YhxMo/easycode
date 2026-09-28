import { useEffect, useRef, useState, type ReactNode } from "react";
import type { ChangeRow } from "../../lib/pane";
import { DiffBody } from "./DiffView";

function Caret({ open }: { open: boolean }) {
  return (
    <span className={`change-caret${open ? " open" : ""}`} aria-hidden="true">
      ▸
    </span>
  );
}

/** One file's diff: already in the row, or fetched when the reader opens it. */
type Loading = Record<string, { status: "loading" } | { status: "error"; message: string }>;

/**
 * The change section's file rows: counts and state on the line, the diff below
 * the one row the reader opens.
 *
 * Counts come before content on purpose — which files moved, and by how much,
 * is what a reader scans for; the diff of one file is what they open next. The
 * working tree's rows are fetched one at a time, when opened, so the section's
 * summary never waits on a diff nobody asked for. A row git cannot describe in
 * lines (binary, deleted) keeps its counts and says why there is nothing to
 * open, instead of offering an empty preview.
 */
export function ChangeList({
  rows,
  loadDiff,
  statsVersion,
}: {
  rows: ChangeRow[];
  /** Fetch one row's diff; absent for sources that already carry theirs. */
  loadDiff?: (row: ChangeRow) => Promise<{ diff: string | null; diff_note: string | null }>;
  /** Changes whenever the rows' counts were re-read from the working tree. */
  statsVersion?: number;
}) {
  const [open, setOpen] = useState<string[]>([]);
  const openRef = useRef<string[]>([]);
  const [loading, setLoading] = useState<Loading>({});
  const [fetched, setFetched] = useState<Record<string, string>>({});
  // One counter per row: a reply that a newer request has already superseded
  // must not land, or the pane would show a diff from an older reading.
  const requestSeq = useRef<Record<string, number>>({});

  const toggle = (key: string) =>
    setOpen((prev) => {
      const next = prev.includes(key) ? prev.filter((k) => k !== key) : [...prev, key];
      // Mirrored for the refresh effect, which must know which rows are open
      // without re-running every time one is toggled.
      openRef.current = next;
      return next;
    });

  const load = (row: ChangeRow, force = false) => {
    if (!loadDiff || !row.gitSource) return;
    if (!force && (fetched[row.key] || loading[row.key])) return;
    const seq = (requestSeq.current[row.key] ?? 0) + 1;
    requestSeq.current[row.key] = seq;
    setLoading((prev) => ({ ...prev, [row.key]: { status: "loading" } }));
    loadDiff(row)
      .then((res) => {
        if (requestSeq.current[row.key] !== seq) return;
        setFetched((prev) => ({ ...prev, [row.key]: res.diff ?? "" }));
        setLoading((prev) => {
          const next = { ...prev };
          delete next[row.key];
          return next;
        });
      })
      .catch((e: unknown) => {
        if (requestSeq.current[row.key] !== seq) return;
        // The row stays open with the reason, instead of an empty body.
        setLoading((prev) => ({
          ...prev,
          [row.key]: {
            status: "error",
            message: e instanceof Error ? e.message : String(e),
          },
        }));
      });
  };

  // New counts describe a new working tree: a diff fetched against the old one
  // would sit under numbers it does not match. Re-request the rows that are
  // open, so what the reader sees comes from one snapshot.
  const firstVersion = useRef(statsVersion);
  useEffect(() => {
    if (firstVersion.current === statsVersion) return;
    setFetched({});
    for (const row of rows) {
      if (row.gitSource && openRef.current.includes(row.key)) load(row, true);
    }
    // `load` closes over the current props; the version is what changes here.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [statsVersion]);

  if (!rows.length) return null;
  return (
    <div className="change-list">
      {rows.map((row) => {
        const expanded = open.includes(row.key);
        const state = loading[row.key];
        const diff = fetched[row.key] || row.diff;
        const canOpen = Boolean(row.gitSource) || Boolean(row.diff);
        const body: ReactNode = (
          <>
            {canOpen && <Caret open={expanded} />}
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
            {canOpen ? (
              <button
                type="button"
                className="change-row"
                title={`${row.label} +${row.added} −${row.removed}`}
                aria-expanded={expanded}
                onClick={() => {
                  if (!expanded) load(row);
                  toggle(row.key);
                }}
              >
                {body}
              </button>
            ) : (
              // Nothing to open: the row states what it knows and why.
              <div className="change-row static" title={row.label}>
                {body}
              </div>
            )}
            {expanded && state?.status === "loading" && (
              <p className="pane-empty">正在读取改动…</p>
            )}
            {expanded && state?.status === "error" && (
              <p className="pane-empty">无法读取改动：{state.message}</p>
            )}
            {expanded && !state && diff && <DiffBody diff={diff} />}
            {!canOpen && row.note && <p className="change-note">{row.note}</p>}          </div>
        );
      })}
    </div>
  );
}
