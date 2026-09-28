import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { fetchGitChanges, fetchGitFileDiff } from "../../api";
import type { GitChanges } from "../../api";
import type { Item, ToolItem } from "../../types";
import {
  fileRows,
  paneData,
  rowStats,
  sessionChangeRows,
  sessionFiles,
  sessionToolItems,
  workingTreeRows,
  type ChangeRow,
} from "./pane";

/** One conversation's pane choices. `turn` is the turn they were made in, so
 * they expire with that turn and the next one can pick its own section again.
 * The pane switch itself is window-level: only the section and the open preview
 * describe a single conversation. */
export interface PaneState {
  turn: number;
  section: string | null;
  preview: string | null;
  /**
   * Whether the change section shows its files and diffs. Per conversation and
   * not per turn: it describes how the reader is looking at this conversation's
   * work, which outlives the turn that produced it. Collapsed until opened.
   */
  changesOpen?: boolean;
}

export interface PaneInputs {
  /** The conversation's draft key: its pane choices are stored under it. */
  key: string;
  sessionId: string | null;
  /** The session's primary root; a change here re-reads the working tree. */
  root: string | null | undefined;
  busy: boolean;
  /** The file tools this conversation ran, as the server recorded them. */
  records: ToolItem[];
  items: Item[];
  /** The turn on screen, for the "produced in this view" test. */
  turnItems: Item[];
  turnNo: number;
  fullAccess: boolean;
  /** The chat area is narrow, so the pane is a drawer over the stream. */
  narrow: boolean;
  /** The reference to focus when a drawer closes. */
  toggleRef: React.RefObject<HTMLButtonElement | null>;
}

/**
 * The right-hand pane: which section is showing, which file is open, and the
 * working tree those sections report. A turn's first artifact opens it by
 * itself; a turn that ran here is what makes that a real signal.
 */
export function usePane({
  key,
  sessionId,
  root,
  busy,
  records,
  items,
  turnItems,
  turnNo,
  fullAccess,
  narrow,
  toggleRef,
}: PaneInputs) {
  const [open, setOpen] = useState(false);
  // Turn whose auto-open the user refused by closing the pane: new artifacts of
  // the same turn must not reopen it.
  const mutedRef = useRef<string | null>(null);
  const [state, setState] = useState<Record<string, PaneState>>({});
  // The working tree as it is right now, for the change and file sections.
  // Tagged with the conversation it was read for.
  const [gitChanges, setGitChanges] = useState<{ session: string; data: GitChanges } | null>(null);
  // Bumped with every stats snapshot, so the open rows re-fetch their diffs
  // against the same reading their counts came from.
  const [gitStatsVersion, setGitStatsVersion] = useState(0);

  // The pane reads the whole conversation: its records outlive the message
  // history, and the calls this view holds but the server has not recorded yet
  // are appended to them.
  const pane = useMemo(
    () => paneData(sessionToolItems(records, items), { fullAccess }),
    [records, items, fullAccess],
  );
  // Newest first: this list is the conversation, and the card a turn just
  // produced is the one worth seeing without scrolling.
  const contextCards = useMemo(() => [...pane.context].reverse(), [pane]);
  const changeCards = useMemo(() => [...pane.changes].reverse(), [pane]);
  // The task list is session state: the newest one stays on screen even after
  // its turn ends. Only a list produced by *this* turn may open the pane by
  // itself — an older one must not pop it open the instant a turn starts.
  const todos = useMemo(() => {
    for (let i = items.length - 1; i >= 0; i -= 1) {
      const item = items[i];
      if (item.kind === "todo") return item.todos;
    }
    return [];
  }, [items]);
  const turnTodos = useMemo(() => turnItems.some((it) => it.kind === "todo"), [turnItems]);
  // Only turns produced in this view drive the pane: a restored session's
  // history must not pop it open on load.
  const liveTurn =
    busy ||
    turnItems.some((it) => it.kind === "assistant" && typeof it.durationMs === "number");
  // The pane keeps the newest list on screen after its turn ends, so a list
  // written earlier reads as the current progress unless it is labelled. Only a
  // turn that actually ran here counts — a restored list never describes now.
  const listIsStale = todos.length > 0 && !(turnTodos && liveTurn);

  // A turn producing its first artifact opens the pane by itself. The trigger is
  // the artifact count *growing* while one turn is on screen, which is what
  // keeps switching conversations from opening it: a switch changes the turn,
  // and a restored conversation's history is not something this view produced.
  const artifactCount = pane.context.length + pane.changes.length + (turnTodos ? 1 : 0);
  const paneTurnKey = `${key}#${turnNo}`;
  const seenArtifactsRef = useRef({ key: paneTurnKey, count: artifactCount });
  useEffect(() => {
    const seen = seenArtifactsRef.current;
    seenArtifactsRef.current = { key: paneTurnKey, count: artifactCount };
    if (seen.key !== paneTurnKey || artifactCount <= seen.count) return;
    // Only work that ran in this view counts: a session's restored history
    // carries artifacts of its own, and they must not pop the pane open.
    if (!liveTurn) return;
    // A drawer would cover the reply it describes, and a pane the user closed
    // in this turn stays closed until the next one.
    if (narrow || mutedRef.current === paneTurnKey) return;
    setOpen(true);
  }, [paneTurnKey, artifactCount, narrow, liveTurn]);

  const self = state[key];
  const section =
    self?.turn === turnNo && self.section
      ? self.section
      : pane.changes.length
        ? "changes"
        : todos.length
          ? "tasks"
          : "context";
  const preview = self?.preview ?? null;
  // The change section opens as a summary line and stays as the reader left it
  // for this conversation.
  const changesOpen = self?.changesOpen ?? false;
  const patch = useCallback(
    (patch: Partial<PaneState>) =>
      setState((prev) => {
        const self = prev[key] ?? { turn: 0, section: null, preview: null };
        return { ...prev, [key]: { ...self, ...patch } };
      }),
    [key],
  );
  const chooseSection = useCallback(
    (next: string) => patch({ turn: turnNo, section: next }),
    [patch, turnNo],
  );
  const setPreview = useCallback((path: string | null) => patch({ preview: path }), [patch]);
  /** Open one file's preview, from whichever section offered it. */
  const openFile = useCallback(
    (target: string) => {
      setPreview(target);
      chooseSection("file");
    },
    [setPreview, chooseSection],
  );

  const toggleChanges = useCallback(
    () => patch({ changesOpen: !changesOpen }),
    [patch, changesOpen],
  );

  const close = useCallback(() => {
    setOpen(false);
    // Closing during a turn is a decision about that turn: it must survive the
    // artifacts still arriving, and expire with the turn.
    mutedRef.current = paneTurnKey;
    // A drawer that vanishes hands the keyboard back to what opened it.
    if (narrow) toggleRef.current?.focus();
  }, [narrow, paneTurnKey, toggleRef]);

  // Escape closes the drawer, the way it closes a modal. The composer keeps its
  // own Escape (its menus use it), so events coming from there are left alone.
  useEffect(() => {
    if (!narrow || !open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      if ((e.target as HTMLElement | null)?.closest(".composer")) return;
      close();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [narrow, open, close]);

  // The working tree as it is now, for the sections that report it. Read when
  // those sections come into view and once more when a turn ends, which is when
  // the files it touched have finished changing.
  const git = gitChanges && gitChanges.session === sessionId ? gitChanges.data : null;
  const wantsGit = open && (section === "changes" || section === "file");
  useEffect(() => {
    if (!sessionId || !wantsGit) return;
    let live = true;
    // The stats request never carries per-file diffs: the section shows counts,
    // and the diff of a file is fetched when the reader opens that row.
    fetchGitChanges(sessionId, false)
      .then((data) => {
        if (!live) return;
        setGitChanges({ session: sessionId, data });
        setGitStatsVersion((prev) => prev + 1);
      })
      .catch((e: unknown) => {
        if (!live) return;
        setGitChanges({
          session: sessionId,
          data: {
            repos: [],
            files: [],
            truncated: false,
            error: e instanceof Error ? e.message : String(e),
          },
        });
      });
    return () => {
      live = false;
    };
  }, [sessionId, wantsGit, busy, root]);

  /**
   * One working-tree row's diff, fetched when the reader opens it. The list
   * keeps the answers itself: it is what knows which reply is still current.
   */
  const loadGitDiff = useCallback(
    async (row: ChangeRow) => {
      const source = row.gitSource;
      if (!sessionId || !source) return { diff: null, diff_note: null };
      return fetchGitFileDiff(sessionId, source.repo, source.path);
    },
    [sessionId],
  );

  const fileEntries = useMemo(() => sessionFiles(pane.context, pane.changes), [pane]);
  const paneFiles = useMemo(() => fileRows(fileEntries, git?.files ?? []), [fileEntries, git]);
  // The change section lists the two sources as rows of counts: the files this
  // conversation changed, and the repository's uncommitted state right now.
  const changeRows = useMemo(() => sessionChangeRows(changeCards), [changeCards]);
  const treeRows = useMemo(() => workingTreeRows(git?.files ?? []), [git]);
  // What the collapsed line reports is the working tree — "changes on this
  // branch" is its answer, not the session's. A directory outside Git has no
  // tree to report, and this conversation's own record must not vanish with it.
  const treeKnown = Boolean(git?.repos.length);
  const changeStats = treeKnown
    ? { added: git?.added ?? 0, removed: git?.removed ?? 0 }
    : rowStats(changeRows);
  const changeFiles = treeKnown ? (git?.files.length ?? 0) : changeRows.length;

  /** Drop everything held for a conversation that no longer exists. */
  const forget = useCallback((id: string) => {
    setState((prev) => {
      if (!(id in prev)) return prev;
      const next = { ...prev };
      delete next[id];
      return next;
    });
  }, []);

  return {
    open,
    setOpen,
    close,
    section,
    chooseSection,
    preview,
    setPreview,
    openFile,
    changesOpen,
    toggleChanges,
    git,
    gitStatsVersion,
    loadGitDiff,
    todos,
    listIsStale,
    contextCards,
    paneFiles,
    changeRows,
    treeRows,
    treeKnown,
    changeStats,
    changeFiles,
    forget,
  };
}
