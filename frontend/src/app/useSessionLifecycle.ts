import { useCallback, useEffect, useRef, useState } from "react";
import {
  createSession,
  deleteSession,
  fetchSession,
  fetchSessions,
  removeProject,
} from "../api";
import type { SessionDetail, SessionSummary, WorkspaceProject } from "../api";
import type { Item, ToolItem } from "../types";
import { artifactsToItems, historyToItems } from "../features/chat/history";
import { DRAFT_KEY } from "../features/chat/useChatStream";
import type { DraftStore } from "../features/composer/useDrafts";

/** Foreground session load phase; null means ready. */
export type SessionLoad = { status: "loading" } | { status: "error"; message: string };

export interface SessionLifecycleInputs {
  // Named functions rather than the hooks' own objects: those objects are
  // rebuilt on every render, so depending on one would rebuild every callback
  // below with it. Each of these is a stable `useCallback` from its hook.
  forgetEntry: (id: string) => void;
  dropEntry: (id: string) => void;
  loadHistory: (key: string, items: Item[]) => void;
  isStreaming: (key: string) => boolean;
  forgetPane: (id: string) => void;
  forgetScroll: (id: string) => void;
  /**
   * The drafts store, read at call time rather than depended on: its `get`
   * closes over the current map, so the store is a new object after every
   * keystroke and depending on it would rebuild every callback below.
   */
  draftsRef: React.RefObject<DraftStore>;
  openTabs: string[];
  setOpenTabs: (update: (prev: string[]) => string[]) => void;
  registerTab: (id: string) => void;
  rememberedTab: string | null;
  sessions: SessionSummary[];
  archived: SessionSummary[];
  setArchived: (update: (prev: SessionSummary[]) => SessionSummary[]) => void;
  sessionById: Map<string, SessionSummary>;
  setProjects: (projects: WorkspaceProject[]) => void;
  refreshSessions: () => void;
  refreshArchived: () => void;
  permissionMode: string;
  resetPermission: (mode: string) => void;
  clearPermissionPending: () => void;
  closeCmdMenu: () => void;
  dismissMention: () => void;
  toast: (kind: "ok" | "err", text: string) => void;
  /** The view token: a reply from a superseded view is discarded. */
  viewTokenRef: React.RefObject<number>;
  currentId: string | null;
  setCurrentId: (id: string | null) => void;
  sessionLoad: SessionLoad | null;
  setSessionLoad: (load: SessionLoad | null) => void;
  busy: boolean;
  setSidebarOpen: (open: boolean) => void;
  setSecondary: (roots: string[]) => void;
  chosenRoot: string | null;
  chooseRoot: (root: string | null) => void;
  setArtifactRecords: (update: (prev: Record<string, ToolItem[]>) => Record<string, ToolItem[]>) => void;
}

/**
 * Opening, closing and removing conversations.
 *
 * The three ways a conversation can go away — a closed blank tab, a deleted
 * session, a removed project — end the same way, and that tail lives here once:
 * the tab goes, the draft goes, every per-conversation cache is released, and
 * both lists are re-read. What differs per path is only what it does *before*
 * that (which ids, whether the server has confirmed) and *after* it (where the
 * view lands).
 */
export function useSessionLifecycle(input: SessionLifecycleInputs) {
  const {
    forgetEntry,
    dropEntry,
    loadHistory,
    isStreaming,
    forgetPane,
    forgetScroll,
    draftsRef,
    openTabs,
    setOpenTabs,
    registerTab,
    rememberedTab,
    sessions,
    archived,
    setArchived,
    sessionById,
    setProjects,
    refreshSessions,
    refreshArchived,
    permissionMode,
    resetPermission,
    clearPermissionPending,
    closeCmdMenu,
    dismissMention,
    toast,
    viewTokenRef,
    currentId,
    setCurrentId,
    sessionLoad,
    setSessionLoad,
    busy,
    setSidebarOpen,
    setSecondary,
    chosenRoot,
    chooseRoot,
    setArtifactRecords,
  } = input;

  const [deleteTarget, setDeleteTarget] = useState<SessionSummary | null>(null);
  const [removeTarget, setRemoveTarget] = useState<{ root: string | null; count: number } | null>(
    null,
  );
  const currentSession = currentId === null ? undefined : sessionById.get(currentId);

  /**
   * Release every per-conversation cache this page holds for a deleted session.
   *
   * Only for a removal the server has confirmed: a closed tab or an archived
   * conversation must keep everything, because reopening it reads these back.
   */
  const forgetSession = useCallback(
    (id: string) => {
      forgetEntry(id);
      setArtifactRecords((prev) => {
        if (!(id in prev)) return prev;
        const next = { ...prev };
        delete next[id];
        return next;
      });
      forgetPane(id);
      forgetScroll(id);
    },
    [forgetEntry, setArtifactRecords, forgetPane, forgetScroll],
  );

  const openSession = useCallback(
    async (id: string | null) => {
      // Re-opening the session that is currently streaming must not reload the
      // stale disk snapshot: that would drop this turn's items and its pending
      // approval while the stream keeps appending events.
      if (id !== null && id === currentId && (busy || sessionLoad?.status === "loading")) {
        setSidebarOpen(false);
        return;
      }
      const token = ++viewTokenRef.current;
      // Switching views no longer interrupts anything: a background turn keeps
      // running in its own slot and is picked up again by switching back.
      setCurrentId(id);
      setSidebarOpen(false);
      // An unconfirmed permission change belongs to the view that started it.
      clearPermissionPending();
      // Menus belong to the composer's text, which is about to change: a
      // dismissed state keeps them closed until the user types again.
      closeCmdMenu();
      dismissMention();
      if (id) registerTab(id);
      if (id) {
        setSessionLoad({ status: "loading" });
        try {
          const detail: SessionDetail = await fetchSession(id);
          // Guard against out-of-order responses: only the most recent open
          // request may apply its result.
          if (viewTokenRef.current !== token) return;
          // A live turn owns its conversation's items: the disk snapshot lags
          // behind it and would drop the streamed reply and its pending approval.
          if (!isStreaming(id)) {
            const restored = historyToItems(
              detail.messages,
              detail.approvals,
              detail.turn_failures,
              detail.turns,
            );
            // The task list is session state, not a message: it rides at the end
            // of the stream so a reopened session still shows it.
            if (detail.todos?.length) restored.push({ kind: "todo", todos: detail.todos });
            loadHistory(id, restored);
          }
          // An edit draft is only meaningful while the turn it names is still in
          // the conversation: after a reload it may have been replaced here or in
          // another tab. The text the user typed stays; the target does not
          // silently become a different turn.
          draftsRef.current.revalidateEdit(id, (detail.turns ?? []).map((t) => t.id), detail.revision);
          setSecondary(detail.secondary_roots ?? []);
          resetPermission(detail.permission_mode ?? "ask");
          // Records describe the whole conversation, not one turn, so they are
          // adopted even while a turn runs: the live items fill in whatever the
          // server had not recorded when this reply was sent.
          setArtifactRecords((prev) => ({
            ...prev,
            [id]: artifactsToItems(detail.artifacts ?? []),
          }));
          setSessionLoad(null);
        } catch (e) {
          if (viewTokenRef.current !== token) return;
          // A failed detail fetch must not blank a conversation that is still
          // streaming into the view.
          if (isStreaming(id)) {
            setSessionLoad(null);
            return;
          }
          const message = e instanceof Error ? e.message : String(e);
          setSessionLoad({ status: "error", message });
          toast("err", `打开会话失败: ${message}`);
        }
      } else {
        dropEntry(DRAFT_KEY);
        setSessionLoad(null);
        setSecondary([]);
        resetPermission("ask");
      }
    },
    [
      currentId,
      busy,
      sessionLoad,
      setSidebarOpen,
      setCurrentId,
      viewTokenRef,
      clearPermissionPending,
      closeCmdMenu,
      dismissMention,
      registerTab,
      setSessionLoad,
      loadHistory,
      isStreaming,
      setSecondary,
      setArtifactRecords,
      resetPermission,
      dropEntry,
      draftsRef,
      toast,
    ],
  );

  // Reopen the remembered conversation, but only while the server still lists
  // it: a session deleted while this page was closed must not come back as a
  // view of something that is gone. The id is read during the first render —
  // the effect that persists the current (still empty) view runs before this
  // could, so reading it later would always find "none".
  const restoredTabRef = useRef(false);
  useEffect(() => {
    if (restoredTabRef.current) return;
    restoredTabRef.current = true;
    if (!rememberedTab) return;
    fetchSessions()
      .then((rows) => {
        if (rows.some((s) => s.id === rememberedTab)) void openSession(rememberedTab);
      })
      .catch(() => {});
  }, [rememberedTab, openSession]);

  /**
   * Create the conversation the user just asked for, before anything is sent:
   * the tab that appears is backed by a real session, so an empty one survives
   * a refresh and closing it can remove it for good.
   */
  const createAndOpen = useCallback(
    async (root: string | null) => {
      setSidebarOpen(false);
      try {
        const created = await createSession(root, permissionMode);
        // Text typed on the start page was meant for the conversation the user
        // just asked for, so it comes along instead of being parked on a page
        // they have left.
        if (currentId === null) draftsRef.current.move(DRAFT_KEY, created.id);
        refreshSessions();
        void openSession(created.id);
      } catch (e) {
        toast("err", `新建会话失败: ${e instanceof Error ? e.message : String(e)}`);
      }
    },
    [currentId, openSession, permissionMode, refreshSessions, setSidebarOpen, toast, draftsRef],
  );

  const newSession = useCallback(() => {
    // Inherit the project on screen: the open session's own project, or the
    // directory picked on the start page.
    void createAndOpen(currentId ? (currentSession?.root ?? null) : chosenRoot);
  }, [createAndOpen, currentId, currentSession, chosenRoot]);

  /** Selecting the tab that is already open changes nothing. */
  const selectTab = useCallback(
    (id: string | null) => {
      if (id === currentId) return;
      void openSession(id);
    },
    [currentId, openSession],
  );

  const closeTab = useCallback(
    async (id: string) => {
      const index = openTabs.indexOf(id);
      const remaining = openTabs.filter((t) => t !== id);
      const target = sessionById.get(id);
      // A conversation the server reports as never started is not worth
      // keeping: ask it to remove the session — it re-checks under its own lock,
      // so a turn that began in the meantime keeps it — and only then drop the
      // tab. Anything else keeps its slot, so a turn's output is never lost and
      // the tab can be reopened; an unknown "started" is treated as started,
      // because only an explicit blank is safe to delete.
      if (target && target.started === false && !isStreaming(id)) {
        try {
          await deleteSession(id, true);
        } catch (e) {
          // The session is still there. Keep the tab rather than send the user
          // back to a sidebar row they thought they had closed.
          toast("err", `关闭会话失败: ${e instanceof Error ? e.message : String(e)}`);
          return;
        }
        draftsRef.current.clear(id);
        forgetSession(id);
        refreshSessions();
      }
      setOpenTabs(() => remaining);
      // Dropping an idle conversation makes reopening it reload from disk; a
      // running turn keeps its slot (and its draft) so its output is not lost.
      dropEntry(id);
      if (id !== currentId) return;
      const neighbour = remaining[index] ?? remaining[index - 1] ?? null;
      void openSession(neighbour);
    },
    [
      openTabs,
      sessionById,
      currentId,
      forgetSession,
      isStreaming,
      draftsRef,
      refreshSessions,
      setOpenTabs,
      dropEntry,
      toast,
      openSession,
    ],
  );

  const confirmDeleteSession = useCallback(async () => {
    if (!deleteTarget) return;
    const target = deleteTarget;
    const token = viewTokenRef.current;
    const isCurrent = currentId === target.id;
    // The backend owns cancellation: DELETE stops the turn, waits for it to
    // unwind and only then removes the file, so no separate cancel is needed.
    try {
      await deleteSession(target.id);
    } catch (e) {
      // The session still exists: keep it in the list/view and surface why.
      toast("err", `删除会话失败: ${e instanceof Error ? e.message : String(e)}`);
      setDeleteTarget(null);
      return;
    }
    setArchived((prev) => prev.filter((x) => x.id !== target.id));
    // Only a successful delete drops the tab (and the draft it holds).
    setOpenTabs((prev) => prev.filter((t) => t !== target.id));
    draftsRef.current.clear(target.id);
    forgetSession(target.id);
    // Clear the view only when it is still the one the delete targeted: a
    // switch during the delete must not blank the new view.
    if (isCurrent && viewTokenRef.current === token) void openSession(null);
    setDeleteTarget(null);
    refreshSessions();
    refreshArchived();
  }, [
    deleteTarget,
    currentId,
    viewTokenRef,
    toast,
    setArchived,
    setOpenTabs,
    forgetSession,
    draftsRef,
    refreshSessions,
    refreshArchived,
    openSession,
  ]);

  const confirmRemoveProject = useCallback(async () => {
    if (!removeTarget) return;
    const removedRoot = removeTarget.root;
    const token = viewTokenRef.current;
    // Sessions of this project, captured now: a failed removal leaves every
    // tab where it was.
    const removed = sessions.filter((s) => (s.root ?? null) === removedRoot).map((s) => s.id);
    // Archived conversations of that project are removed as well, so their
    // caches have to go too — the sidebar just was not showing them.
    const removedAll = [
      ...new Set([
        ...removed,
        ...archived.filter((s) => (s.root ?? null) === removedRoot).map((s) => s.id),
      ]),
    ];
    try {
      const r = await removeProject(removedRoot);
      setProjects(r.projects);
      setOpenTabs((prev) => prev.filter((id) => !removedAll.includes(id)));
      for (const id of removedAll) {
        draftsRef.current.clear(id);
        forgetSession(id);
      }
      refreshSessions();
      refreshArchived();
      if (viewTokenRef.current === token) {
        if (currentId !== null && (currentSession?.root ?? null) === removedRoot) {
          void openSession(null);
        } else if (currentId === null && removedRoot === chosenRoot) {
          chooseRoot(null);
        }
      }
      toast("ok", `已移除项目（删除 ${r.deleted_sessions} 条会话）`);
    } catch (e) {
      toast("err", e instanceof Error ? e.message : String(e));
    } finally {
      setRemoveTarget(null);
    }
  }, [
    removeTarget,
    viewTokenRef,
    sessions,
    archived,
    setProjects,
    setOpenTabs,
    forgetSession,
    draftsRef,
    refreshSessions,
    refreshArchived,
    currentId,
    currentSession,
    chosenRoot,
    chooseRoot,
    openSession,
    toast,
  ]);

  return {
    openSession,
    createAndOpen,
    newSession,
    selectTab,
    closeTab,
    confirmDeleteSession,
    confirmRemoveProject,
    deleteTarget,
    setDeleteTarget,
    removeTarget,
    setRemoveTarget,
  };
}
