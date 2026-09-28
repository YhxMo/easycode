import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { CSSProperties } from "react";
import type {
  ModelsInfo,
  SessionDetail,
  SessionSummary,
  WorkspacesInfo,
  WorkspaceProject,
} from "../api";
import {
  archiveProjectChats,
  createSession,
  createWorktree,
  deleteSession,
  fetchArchivedSessions,
  fetchModels,
  fetchSession,
  fetchSessions,
  pinSession,
  fetchWorkspaces,
  pinProject,
  removeProject,
  revealInFinder,
  saveProject,
  setSessionArchived,
  setSessionPermission,
  submitApproval,
} from "../api";
import { DRAFT_KEY, useChatStream } from "../features/chat/useChatStream";
import { useComposer } from "../features/composer/useComposer";
import { MENTION_LIST_ID, MENTION_OPTION_PREFIX, MentionMenu } from "../features/composer/MentionMenu";
import { ExtensionsSettings, type ExtensionTarget } from "../features/extensions/ExtensionsSettings";
import { ModelPicker } from "../features/models/ModelPicker";
import { currentTurn } from "../features/chat/chatStream";
import { artifactsToItems, historyToItems } from "../features/chat/history";
import type { ApprovalState, Item, ToolItem } from "../types";
import { COMMAND_LIST_ID, CommandMenu } from "../features/composer/CommandMenu";
import {
  activeCommandId,
  commandNameIn,
} from "../features/composer/commands";
import { Modal } from "../components/Modal";
import { ChatMessages } from "../features/chat/ChatMessages";
import { Sidebar } from "../features/sidebar/Sidebar";
import { ComposerBar } from "../features/composer/ComposerBar";
import { RightPane } from "../features/pane/RightPane";
import { PaneBody } from "../features/pane/PaneBody";
import { usePane } from "../features/pane/usePane";
import { EmptyState } from "../features/chat/EmptyState";
import { LoadingState } from "../components/primitives/LoadingState";
import { TabBar, type OpenTab } from "../features/sidebar/TabBar";
import { SelectionActions } from "../features/chat/SelectionActions";
import { groupSessions } from "../features/sidebar/sessionGroups";
import { usePersistedFlags } from "../lib/usePersistedFlags";
import { useDrafts, type Draft } from "../features/composer/useDrafts";
import { useStickToBottom } from "../lib/useStickToBottom";
import type { StickToBottom } from "../lib/useStickToBottom";
import type { ProjectAction } from "../features/sidebar/ProjectMenu";
import { basename, DEFAULT_PROJECT } from "../lib/paths";
import { SecondaryEditor } from "../features/sidebar/SecondaryEditor";
import {
  CONTINUE_PROMPT,
  CURRENT_TAB_KEY,
  EMPTY_MODELS,
  NARROW_CHAT_PX,
  PANE_SECTIONS,
  readStoredCurrentTab,
  readStoredTabs,
} from "./constants";
import { useToast } from "./useToast";

/** Foreground session load phase; null means ready. */
type SessionLoad = { status: "loading" } | { status: "error"; message: string };

/** One user message, as the transcript holds it. */
type UserItem = Extract<Item, { kind: "user" }>;

/**
 * The `/` command a send from this draft carries, if it still carries one.
 *
 * The selection lives in the draft, beside the text it belongs to, and only
 * counts while that text is still the command: typing over it clears the id
 * rather than leaving one that would expand a prompt the user replaced.
 */
function commandIdFor(draft: Draft): string | null {
  return activeCommandId(draft.command, draft.text);
}

export default function App() {
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  // Read-only view of the server list; never written to on its own.
  const sessionById = useMemo(() => new Map(sessions.map((s) => [s.id, s])), [sessions]);
  const [currentId, setCurrentId] = useState<string | null>(null);
  // Unsent composer text, one record per conversation: switching sessions must
  // never carry what was typed here into another conversation's field.
  const drafts = useDrafts();
  const [models, setModels] = useState<ModelsInfo>(EMPTY_MODELS);
  const [workspaces, setWorkspaces] = useState<WorkspacesInfo>({ projects: [] });
  // False until the workspace list has been answered once, so the directory
  // card shows a loading state instead of a wrong project name.
  const [workspacesLoaded, setWorkspacesLoaded] = useState(false);

  const [chosenRoot, setChosenRoot] = useState<string | null>(null);
  // One pair of states serves both the new-session draft and the open session:
  // a session event updates them in place, and openSession resets them.
  const [secondary, setSecondary] = useState<string[]>([]);
  const [permission, setPermission] = useState<string>("ask");
  // Foreground session load state: while loading (or after a failed load) the
  // composer, permission picker and secondary editor are disabled, so no
  // request can be sent against a target whose state is not confirmed yet.
  const [sessionLoad, setSessionLoad] = useState<SessionLoad | null>(null);
  // A permission change is server-confirmed: while the request is in flight the
  // view keeps the old value and sending is blocked, so a turn can never write
  // an unconfirmed mode back to the server.
  const [permissionPending, setPermissionPending] = useState(false);
  // Full host access is a security boundary, so selecting it from the menu
  // opens an explicit risk confirmation before the server-side mode changes.
  const [allowAllConfirm, setAllowAllConfirm] = useState(false);
  /** Bumped whenever the extensions dialog installs or saves something: the `/`
   *  menu reads its sources again, so a new skill is selectable without a reload. */
  const [extensionsRevision, setExtensionsRevision] = useState(0);

  // on mobile (<=760px) the sidebar is hidden; `sidebarOpen` drives the
  // drawer overlay so core session/project/model navigation stays reachable.
  const [sidebarOpen, setSidebarOpen] = useState(false);
  // Open session tabs: `openTabs` is the single list of opened conversations,
  // in the order they were opened. Closing one closes only the view: the
  // conversation stays in the sidebar and a running turn keeps streaming into
  // its own slot.
  const [openTabs, setOpenTabs] = useState<string[]>(() => readStoredTabs());
  // File-tool records per conversation, as fetched with the session. They are
  // what the pane reads once a refresh (or a compaction) has taken the tool
  // results out of the message history.
  const [artifactRecords, setArtifactRecords] = useState<Record<string, ToolItem[]>>({});
  const mainRef = useRef<HTMLDivElement>(null);
  const fieldRef = useRef<HTMLTextAreaElement>(null);
  // Height of the floating composer: the message stream reserves exactly that
  // much room at its bottom, so a grown input never hides the last reply.
  const [composerHeight, setComposerHeight] = useState(0);
  // View version: a response is applied only while it still matches, and the
  // ref is readable at response time by child editors.
  const openSeqRef = useRef(0);
  // Newest session-list request: an out-of-order reply must not prune tabs the
  // list it lost to still has.
  const sessionsSeqRef = useRef(0);
  // Escape closes the mobile drawer. Scoped to when the drawer is open so
  // the desktop layout and the composer's own Escape (command menu) are
  // unaffected.
  useEffect(() => {
    if (!sidebarOpen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setSidebarOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [sidebarOpen]);

  const closeSidebar = useCallback(() => setSidebarOpen(false), []);

  // Read at approval time, so the callback handed to the stream stays stable
  // while the pane hook below is (re)created with the foreground conversation.
  const stickyRef = useRef<StickToBottom | null>(null);
  // The pane hook is declared after the stream (it reads the live items), so a
  // removal that happens earlier reaches its forget() through this.
  const paneRef = useRef<ReturnType<typeof usePane> | null>(null);
  const revealApproval = useCallback(
    () => stickyRef.current?.reveal(".approval-card.pending"),
    [],
  );

  const setProjects = useCallback(
    (projects: WorkspaceProject[]) => setWorkspaces((w) => ({ ...w, projects })),
    [],
  );

  const registerTab = useCallback((id: string) => {
    setOpenTabs((prev) => (prev.includes(id) ? prev : [...prev, id]));
  }, []);

  const refreshSessions = useCallback(() => {
    const seq = ++sessionsSeqRef.current;
    fetchSessions()
      .then((list) => {
        // An older reply that lost the race must not prune a tab the newest
        // list still carries (e.g. one a turn named while the request was out).
        if (seq !== sessionsSeqRef.current) return;
        setSessions(list);
        // Tabs are ids: a conversation the server no longer lists loses its tab
        // here, after a successful load — never on initialization or failure.
        const known = new Set(list.map((s) => s.id));
        setOpenTabs((prev) => {
          const kept = prev.filter((id) => known.has(id));
          return kept.length === prev.length ? prev : kept;
        });
      })
      .catch(() => {});
    fetchWorkspaces()
      .then(setWorkspaces)
      .catch(() => {})
      .finally(() => setWorkspacesLoaded(true));
  }, []);

  // Resolved ahead of useChatStream so send() can stamp the model name onto the
  // turn's assistant messages (reply meta row).
  const currentSession = currentId === null ? undefined : sessionById.get(currentId);
  // The open conversation's own directory; a move (only a blank session can
  // make one) changes what every directory-derived view below reports.
  const currentRoot = currentSession?.root ?? null;
  const currentModelName =
    (currentId ? (models.models[currentSession?.model_alias ?? ""]?.model ?? currentSession?.model_alias) : null) ??
    models.models[models.default]?.model ??
    models.default;
  const sessionBlocked = sessionLoad !== null;
  const sendBlocked = sessionBlocked || permissionPending;

  // The composer edits exactly one conversation's draft: the one on screen.
  const draftKey = currentId ?? DRAFT_KEY;
  const draft = drafts.get(draftKey);
  // The stream reports this text back on an edit; the composer reads the same
  // field for the input element itself.
  const input = draft.text;

  /** Leave edit mode and put the draft the user started with back. */
  const cancelEdit = useCallback(() => {
    drafts.cancelEdit(draftKey);
    fieldRef.current?.focus();
  }, [drafts, draftKey]);

  const {
    send,
    sendEdit,
    stop,
    busy,
    items,
    activity,
    setItems,
    loadHistory,
    dropEntry,
    forgetEntry,
    isStreaming,
  } = useChatStream({
    input,
    currentId,
    chosenRoot,
    secondary,
    permission,
    commandId: commandIdFor(draft),
    currentModelName,
    sendBlocked,
    getViewToken: () => openSeqRef.current,
    refreshSessions,
    clearDraft: drafts.clear,
    moveDraft: drafts.move,
    setCurrentId,
    onApprovalRequired: revealApproval,
    onSessionNamed: registerTab,
  });

  /**
   * Release every per-conversation cache this page holds for a deleted session.
   *
   * Only for a delete the server has confirmed: a closed tab or an archived
   * conversation must keep everything, because reopening it reads these back.
   * Callers own the tab list, the draft and the view, which differ per path.
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
      paneRef.current?.forget(id);
      stickyRef.current?.forget(id);
    },
    [forgetEntry],
  );

  // The message pane follows the live edge only while the reader is at it, and
  // remembers each conversation's place. Declared after the stream because it
  // tracks the foreground conversation's items.
  const sticky = useStickToBottom(mainRef, draftKey, items);
  useEffect(() => {
    stickyRef.current = sticky;
  }, [sticky]);

  const decideApproval = useCallback(
    async (item: Extract<Item, { kind: "approval" }>, approve: boolean, always: boolean) => {
      const mark = (state: ApprovalState) =>
        setItems((prev) =>
          prev.map((it) => (it.kind === "approval" && it.id === item.id ? { ...it, state } : it)),
        );
      mark(approve ? "approved" : "denied");
      try {
        await submitApproval(item.id, approve, always);
      } catch {
        // The approval was already resolved (e.g. turn cancelled, timed out or
        // the user refreshed) — mark it expired instead of reverting to pending.
        mark("expired");
      }
    },
    [setItems],
  );

  const [archived, setArchived] = useState<SessionSummary[]>([]);
  const refreshArchived = useCallback(() => {
    fetchArchivedSessions()
      .then((list) => {
        setArchived(list);
        // An archived conversation has no tab: it left the main list on purpose.
        const gone = new Set(list.map((s) => s.id));
        setOpenTabs((prev) =>
          prev.some((id) => gone.has(id)) ? prev.filter((id) => !gone.has(id)) : prev,
        );
      })
      .catch(() => {});
  }, []);

  useEffect(() => {
    refreshSessions();
    fetchModels().then(setModels).catch(() => {});
  }, [refreshSessions]);

  // Changes whenever the registered projects do, so the menu re-reads them.
  const projectSig = (workspaces.projects ?? []).map((p) => p.root ?? "").join("\u0000");

  // Bound by name: the two callbacks below depend on these, and a member
  // expression would make the rule ask for the whole (per-render) object.
  const composer = useComposer({
    key: draftKey,
    drafts,
    sessionId: currentId,
    sessionRoot: currentRoot,
    draftRoot: chosenRoot,
    secondary,
    projectsSignature: projectSig,
    extensionsRevision,
    send,
    sendEdit,
    cancelEdit,
    sticky,
    fieldRef,
  });
  // Bound by name: two callbacks below depend on these, and through a member
  // expression the dependency rule asks for the whole (per-render) object.
  const { closeCmdMenu, dismissMention } = composer;
  const insertDraftText = composer.setInput;

  useEffect(() => {
    refreshArchived();
  }, [refreshArchived]);

  useEffect(() => {
    try {
      window.localStorage.setItem("easycode:open_tabs", JSON.stringify(openTabs));
    } catch {
      // localStorage 不可用（隐私模式等）——仅内存态，忽略即可
    }
  }, [openTabs]);

  useEffect(() => {
    try {
      window.localStorage.setItem(CURRENT_TAB_KEY, currentId ?? "");
    } catch {
      // localStorage 不可用（隐私模式等）——仅内存态，忽略即可
    }
  }, [currentId]);

  // ---- toast (shared by openSession failure + project actions) ----
  const [toast, showToast] = useToast();

  // ---- sidebar collapse state (sections + project groups, in localStorage) ----
  const [collapsedProjects, toggleProjectCollapsed] = usePersistedFlags("easycode:collapsed_projects");
  const [collapsedSections, toggleSection] = usePersistedFlags("easycode:collapsed_sections");

  const openSession = useCallback(
    async (id: string | null) => {
      // Re-opening the session that is currently streaming must not reload the
      // stale disk snapshot: that would drop this turn's items and its pending
      // approval while the stream keeps appending events.
      if (id !== null && id === currentId && (busy || sessionLoad?.status === "loading")) {
        setSidebarOpen(false);
        return;
      }
      const token = ++openSeqRef.current;
      // Switching views no longer interrupts anything: a background turn keeps
      // running in its own slot and is picked up again by switching back.
      setCurrentId(id);
      setSidebarOpen(false);
      // An unconfirmed permission change belongs to the view that started it.
      setPermissionPending(false);
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
          if (openSeqRef.current !== token) return;
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
          drafts.revalidateEdit(id, (detail.turns ?? []).map((t) => t.id), detail.revision);
          setSecondary(detail.secondary_roots ?? []);
          setPermission(detail.permission_mode ?? "ask");
          // Records describe the whole conversation, not one turn, so they are
          // adopted even while a turn runs: the live items fill in whatever the
          // server had not recorded when this reply was sent.
          setArtifactRecords((prev) => ({
            ...prev,
            [id]: artifactsToItems(detail.artifacts ?? []),
          }));
          setSessionLoad(null);
        } catch (e) {
          if (openSeqRef.current !== token) return;
          // A failed detail fetch must not blank a conversation that is still
          // streaming into the view.
          if (isStreaming(id)) {
            setSessionLoad(null);
            return;
          }
          const message = e instanceof Error ? e.message : String(e);
          setSessionLoad({ status: "error", message });
          showToast("err", `打开会话失败: ${message}`);
        }
      } else {
        dropEntry(DRAFT_KEY);
        setSessionLoad(null);
        setSecondary([]);
        setPermission("ask");
      }
    },
    [
      showToast,
      currentId,
      busy,
      sessionLoad,
      loadHistory,
      dropEntry,
      isStreaming,
      registerTab,
      drafts,
      closeCmdMenu,
      dismissMention,
    ],
  );

  // Reopen the remembered conversation, but only while the server still lists
  // it: a session deleted while this page was closed must not come back as a
  // view of something that is gone. The id is read during the first render —
  // the effect below writes the current (still empty) view back to the same key
  // before this check could run, so reading it later would always find "none".
  const [rememberedTab] = useState(readStoredCurrentTab);
  const restoredTabRef = useRef(false);
  useEffect(() => {
    if (restoredTabRef.current) return;
    restoredTabRef.current = true;
    if (!rememberedTab) return;
    fetchSessions()
      .then((list) => {
        if (list.some((s) => s.id === rememberedTab)) openSession(rememberedTab);
      })
      .catch(() => {});
  }, [rememberedTab, openSession]);

  // Restore the caret a conversation was left at. The field keeps the focus it
  // has: this only puts the insertion point where the user stopped typing.
  // Never while an IME is composing — the selection belongs to it until the
  // candidate lands.

  // The composer is laid out over the stream: publish its height so the stream
  // can keep exactly that much room clear at the bottom.
  const attachComposer = useCallback((el: HTMLDivElement | null) => {
    if (!el || typeof ResizeObserver === "undefined") return;
    const publish = () => setComposerHeight(el.offsetHeight);
    const observer = new ResizeObserver(publish);
    observer.observe(el);
    publish();
    return () => observer.disconnect();
  }, []);

  const projectMeta = useMemo(() => {
    const map = new Map<string | null, WorkspaceProject>();
    for (const p of workspaces.projects ?? []) map.set(p.root ?? null, p);
    return map;
  }, [workspaces]);

  const chooseRoot = useCallback((root: string | null) => {
    ++openSeqRef.current;
    setChosenRoot(root);
  }, []);

  // A conversation's main directory may move only before its first turn: once
  // one has run, its records, previews and tree report all describe a single
  // directory. The start page's draft holds no session, so it just chooses.
  const rootEditable = currentId === null || currentSession?.started === false;

  /** Adopt the workspace a blank conversation moved to, as the server confirmed it. */
  const sessionWorkspaceChanged = useCallback(
    (updated: SessionSummary, projects: WorkspaceProject[]) => {
      setSessions((prev) => prev.map((s) => (s.id === updated.id ? { ...s, ...updated } : s)));
      setSecondary(updated.secondary_roots ?? []);
      setProjects(projects);
    },
    [setProjects],
  );

  /**
   * Create the conversation the user just asked for, before anything is sent:
   * the tab that appears is backed by a real session, so an empty one survives
   * a refresh and closing it can remove it for good.
   */
  const createAndOpen = useCallback(
    async (root: string | null) => {
      setSidebarOpen(false);
      try {
        const created = await createSession(root, permission);
        // Text typed on the start page was meant for the conversation the user
        // just asked for, so it comes along instead of being parked on a page
        // they have left.
        if (currentId === null) drafts.move(DRAFT_KEY, created.id);
        refreshSessions();
        openSession(created.id);
      } catch (e) {
        showToast("err", `新建会话失败: ${e instanceof Error ? e.message : String(e)}`);
      }
    },
    [currentId, drafts, openSession, permission, refreshSessions, showToast],
  );

  const newSession = useCallback(() => {
    // Inherit the project on screen: the open session's own project, or the
    // directory picked on the start page.
    void createAndOpen(currentId ? (currentSession?.root ?? null) : chosenRoot);
  }, [createAndOpen, currentId, currentSession, chosenRoot]);

  // ---- project row actions (new chat / more menu) ----
  const [editTarget, setEditTarget] = useState<{ root: string | null; name: string } | null>(null);
  /** The project whose skills and servers the extensions dialog is editing. */
  const [extensionTarget, setExtensionTarget] = useState<ExtensionTarget | null>(null);
  const [removeTarget, setRemoveTarget] = useState<{ root: string | null; count: number } | null>(null);
  const [showArchived, setShowArchived] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<SessionSummary | null>(null);

  const confirmDeleteSession = useCallback(async () => {
    if (!deleteTarget) return;
    const target = deleteTarget;
    const viewToken = openSeqRef.current;
    const isCurrent = currentId === target.id;
    // The backend owns cancellation: DELETE stops the turn, waits for it to
    // unwind and only then removes the file, so no separate cancel is needed.
    try {
      await deleteSession(target.id);
    } catch (e) {
      // The session still exists: keep it in the list/view and surface why.
      showToast("err", `删除会话失败: ${e instanceof Error ? e.message : String(e)}`);
      setDeleteTarget(null);
      return;
    }
    setArchived((prev) => prev.filter((x) => x.id !== target.id));
    // Only a successful delete drops the tab (and the draft it holds).
    setOpenTabs((prev) => prev.filter((t) => t !== target.id));
    drafts.clear(target.id);
    forgetSession(target.id);
    // Clear the view only when it is still the one the delete targeted: a
    // switch during the delete must not blank the new view.
    if (isCurrent && openSeqRef.current === viewToken) openSession(null);
    setDeleteTarget(null);
    refreshSessions();
    refreshArchived();
  }, [
    deleteTarget,
    currentId,
    drafts,
    forgetSession,
    openSession,
    refreshSessions,
    refreshArchived,
    setDeleteTarget,
    showToast,
  ]);

  const togglePin = useCallback(
    async (target: SessionSummary) => {
      try {
        await pinSession(target.id, !target.pinned);
        // The server owns the flag: re-read the list rather than guessing.
        refreshSessions();
      } catch (e) {
        showToast("err", `置顶失败: ${e instanceof Error ? e.message : String(e)}`);
      }
    },
    [refreshSessions, showToast],
  );

  const restoreArchived = useCallback(
    async (session: SessionSummary) => {
      try {
        await setSessionArchived(session.id, false);
      } catch (e) {
        // A failed restore leaves the entry exactly where it was, with the
        // reason on screen: it must not look like it moved and came back.
        showToast("err", `恢复会话失败: ${e instanceof Error ? e.message : String(e)}`);
        return;
      }
      setArchived((prev) => prev.filter((x) => x.id !== session.id));
      refreshSessions();
    },
    [refreshSessions, setArchived, showToast],
  );

  const newChatInProject = useCallback(
    (root: string | null) => {
      // This row's project is the requested one, whatever is on screen now.
      void createAndOpen(root);
    },
    [createAndOpen],
  );

  /**
   * Open the extensions dialog for one project.
   *
   * The target is fixed here, at the moment of the click: the dialog then
   * describes one project's skills and servers even if the view moves on. A
   * session is carried along only when it really runs in that project — its
   * connection status says nothing about another project's servers.
   */
  const openExtensions = useCallback(
    (initialTab: ExtensionTarget["initialTab"], root: string | null) => {
      // Read from the session list rather than the view's own root: a session
      // whose summary has not loaded yet must not be mistaken for a match.
      const sameProject = currentSession !== undefined && (currentSession.root ?? null) === root;
      setExtensionTarget({
        root,
        sessionId: sameProject && currentId ? currentId : null,
        initialTab,
      });
    },
    [currentId, currentSession],
  );

  const runProjectAction = useCallback(
    async (root: string | null, action: ProjectAction) => {
      try {
        if (action === "edit") {
          const proj = root ? projectMeta.get(root) : projectMeta.get(null);
          setEditTarget({ root, name: proj?.name ?? (root ? basename(root) : DEFAULT_PROJECT) });
        } else if (action === "extensions") {
          // The project whose row was acted on, not whatever is on screen.
          openExtensions("skills", root);
        } else if (action === "pin" || action === "unpin") {
          const r = await pinProject(root, action === "pin");
          setProjects(r.projects);
          refreshSessions();
          showToast("ok", action === "pin" ? "已置顶" : "已取消置顶");
        } else if (action === "reveal") {
          const r = await revealInFinder(root);
          if (!r.ok) showToast("err", r.error ?? "无法打开目录");
        } else if (action === "worktree") {
          if (!root) {
            showToast("err", "默认项目没有目录");
            return;
          }
          const r = await createWorktree(root);
          setProjects(r.projects);
          refreshSessions();
          if (r.warnings?.length) {
            showToast("err", `工作树已创建，但有提示：${r.warnings.join("；")}`);
          } else {
            showToast("ok", `已创建永久工作树 ${r.name ?? ""}`);
          }
        } else if (action === "archive") {
          const r = await archiveProjectChats(root);
          refreshSessions();
          refreshArchived();
          showToast("ok", `已归档 ${r.archived_sessions} 条聊天`);
        } else if (action === "remove") {
          setRemoveTarget({ root, count: sessions.filter((s) => (s.root ?? null) === root).length });
        }
      } catch (e) {
        showToast("err", e instanceof Error ? e.message : String(e));
      }
    },
    [
      openExtensions,
      projectMeta,
      refreshSessions,
      refreshArchived,
      sessions,
      setEditTarget,
      setProjects,
      setRemoveTarget,
      showToast,
    ],
  );

  const confirmRemoveProject = useCallback(async () => {
    if (!removeTarget) return;
    const removedRoot = removeTarget.root;
    const viewToken = openSeqRef.current;
    // Sessions of this project, captured now: a failed removal leaves every
    // tab where it was.
    const removed = sessions.filter((s) => (s.root ?? null) === removedRoot).map((s) => s.id);
    // Archived conversations of that project are removed as well, so their
    // caches have to go too — the sidebar just was not showing them.
    const removedAll = [
      ...new Set([...removed, ...archived.filter((s) => (s.root ?? null) === removedRoot).map((s) => s.id)]),
    ];
    try {
      const r = await removeProject(removedRoot);
      setProjects(r.projects);
      setOpenTabs((prev) => prev.filter((id) => !removedAll.includes(id)));
      for (const id of removedAll) {
        drafts.clear(id);
        forgetSession(id);
      }
      refreshSessions();
      refreshArchived();
      if (openSeqRef.current === viewToken) {
        if (currentId !== null && (currentSession?.root ?? null) === removedRoot) openSession(null);
        else if (currentId === null && removedRoot === chosenRoot) chooseRoot(null);
      }
      showToast("ok", `已移除项目（删除 ${r.deleted_sessions} 条会话）`);
    } catch (e) {
      showToast("err", e instanceof Error ? e.message : String(e));
    } finally {
      setRemoveTarget(null);
    }
  }, [
    removeTarget,
    chooseRoot,
    drafts,
    forgetSession,
    sessions,
    archived,
    refreshSessions,
    refreshArchived,
    currentSession,
    currentId,
    chosenRoot,
    openSession,
    setProjects,
    setRemoveTarget,
    showToast,
  ]);

  // Sidebar sections: directories, pinned conversations, projects, recents.
  const groups = useMemo(
    () => groupSessions(sessions, workspaces.projects ?? []),
    [sessions, workspaces.projects],
  );

  // Tabs are ids; the titles come from the session list so a rename shows up.
  // Every opened conversation is registered exactly once, by opening it or by
  // the moment the backend named a draft; the draft keeps its own tab so the
  // strip always names what is on screen.
  const sessionTabs: OpenTab[] = openTabs.map((id) => ({
    id,
    title: sessionById.get(id)?.title ?? "会话",
  }));
  const tabs: OpenTab[] =
    currentId === null ? [{ id: DRAFT_KEY, title: "新会话", draft: true }, ...sessionTabs] : sessionTabs;

  // Selecting the tab that is already open changes nothing: the draft tab in
  // particular must never be mistaken for a stored session.
  const selectTab = useCallback(
    (id: string | null) => {
      if (id === currentId) return;
      openSession(id);
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
          showToast("err", `关闭会话失败: ${e instanceof Error ? e.message : String(e)}`);
          return;
        }
        drafts.clear(id);
        forgetSession(id);
        refreshSessions();
      }
      setOpenTabs(remaining);
      // Dropping an idle conversation makes reopening it reload from disk; a
      // running turn keeps its slot (and its draft) so its output is not lost.
      dropEntry(id);
      if (id !== currentId) return;
      const neighbour = remaining[index] ?? remaining[index - 1] ?? null;
      openSession(neighbour);
    },
    [
      openTabs,
      currentId,
      sessionById,
      drafts,
      dropEntry,
      forgetSession,
      isStreaming,
      openSession,
      refreshSessions,
      showToast,
    ],
  );

  // The project a new conversation from the sidebar joins: the one on screen,
  // so the button's hint names what the user will actually get. A project's own
  // name comes first — the directory it points at is what the path says.
  const nextSessionRoot = currentId ? currentRoot : chosenRoot;
  /** What to call a directory: its configured project name, else the folder's. */
  const projectLabel = useCallback(
    (root: string | null) =>
      projectMeta.get(root)?.name ||
      (root ? basename(root) : workspaces.default ? basename(workspaces.default) : DEFAULT_PROJECT),
    [projectMeta, workspaces.default],
  );
  const newSessionLabel = projectLabel(nextSessionRoot);
  // What the empty stage can promise: the directory this draft would run in.
  const draftContext = projectLabel(chosenRoot);
  const exploration = useMemo(() => {
    let reads = 0;
    let searches = 0;
    for (const item of items) {
      if (item.kind !== "tool") continue;
      if (/^(read_file|glob)$/.test(item.name)) reads += 1;
      if (item.name === "grep") searches += 1;
    }
    return { reads, searches };
  }, [items]);
  const lastItem = items[items.length - 1];
  const activeTool = lastItem?.kind === "tool" && !lastItem.done;
  // Any turn on this page: a model switch rebinds every session, so a
  // background turn blocks it just as the foreground one does.
  const anyBusy = useMemo(() => Object.values(activity).some((a) => a.busy), [activity]);
  const turn = useMemo(() => currentTurn(items), [items]);
  // Scoped to the current turn (items after the last user message): restored
  // history approvals must not read as work waiting on the user now.
  const pendingApprovals = useMemo(
    () =>
      turn.filter(
        (it): it is Extract<Item, { kind: "approval" }> =>
          it.kind === "approval" && it.state === "pending",
      ),
    [turn],
  );
  const pendingApproval = pendingApprovals.length > 0;
  // Turns are counted by user messages: a choice made in one turn must not
  // carry over to the next.
  const turnNo = useMemo(() => items.filter((it) => it.kind === "user").length, [items]);
  // The pane covers the stream once the chat area gets narrow. Measured from
  // the element the CSS container query measures, so the two agree; a narrow
  // pane must not open on its own, since it would hide the reply it describes.
  const chatRef = useRef<HTMLElement>(null);
  const paneToggleRef = useRef<HTMLButtonElement>(null);
  const [chatNarrow, setChatNarrow] = useState(false);
  useEffect(() => {
    const el = chatRef.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const measure = () => setChatNarrow(el.clientWidth < NARROW_CHAT_PX);
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  // The pane reads the whole conversation, not just the turn on screen: its
  // records outlive the message history, and the calls this view holds but the
  // server has not recorded yet are appended to them.
  const pane = usePane({
    key: draftKey,
    sessionId: currentId,
    root: currentRoot,
    busy,
    records: currentId ? (artifactRecords[currentId] ?? []) : [],
    items,
    turnItems: turn,
    turnNo,
    fullAccess: permission === "allow-all",
    narrow: chatNarrow,
    toggleRef: paneToggleRef,
  });

  useEffect(() => {
    paneRef.current = pane;
  });

  // The centred first-run stage replaces the stream until a conversation has
  // something to show; a load in progress is never masked by it.
  const isEmptyStage = items.length === 0 && sessionLoad === null;

  const changePermission = useCallback(
    async (mode: string, confirmFullAccess = false) => {
      // One permission request at a time: the picker is disabled while pending,
      // so a second request can never race the first (no request token needed).
      if (sendBlocked) return;
      const id = currentId;
      // The draft's mode is a local choice for the next send: update at once.
      if (id === null) {
        setPermission(mode);
        return;
      }
      const viewToken = openSeqRef.current;
      // The view keeps the confirmed mode until the server answers; a failed
      // request therefore leaves the old value in place with no rollback.
      setPermissionPending(true);
      try {
        const r = await setSessionPermission(id, mode, confirmFullAccess);
        if (openSeqRef.current !== viewToken) return;
        setPermission(r.permission_mode);
      } catch (e) {
        if (openSeqRef.current !== viewToken) return;
        showToast("err", `修改权限失败: ${e instanceof Error ? e.message : String(e)}`);
      } finally {
        // A response from a superseded view must not release a newer request.
        if (openSeqRef.current === viewToken) setPermissionPending(false);
      }
    },
    [currentId, showToast, sendBlocked],
  );

  const requestPermissionChange = useCallback(
    (mode: string) => {
      // Entering full access is the one change that removes every boundary, so
      // it goes through the risk dialog first; only that dialog's own button
      // states the consent to the server.
      if (mode === "allow-all" && permission !== "allow-all") {
        setAllowAllConfirm(true);
        return;
      }
      void changePermission(mode, mode === "allow-all");
    },
    [changePermission, permission],
  );

  /**
   * The one way a message leaves the composer.
   *
   * Enter and the send button both come through here. An edit rewrites the
   * branch it was opened from, so a second path that called ``send()`` on its
   * own would append a new turn beside the very message the user was editing —
   * which is exactly what the retract arrow promised to replace.
   */
  /**
   * A continuation the user has to agree to: the prompt goes into the composer
   * and nothing is sent. What the interrupted turn already did is still on
   * disk, so the message tells the model to look before it acts.
   */
  const continueUnfinished = useCallback(() => {
    // Never overwrite what the user is writing: the draft is theirs.
    if (!drafts.get(draftKey).text.trim()) insertDraftText(CONTINUE_PROMPT);
    fieldRef.current?.focus();
    sticky.stick();
  }, [drafts, draftKey, insertDraftText, sticky]);

  /**
   * Send one earlier message back to the composer.
   *
   * The revision is read from the server right now rather than kept from the
   * last load: an edit has to name the conversation as it is, and the view's
   * copy can be older than the last turn it streamed. A failed read leaves the
   * draft alone and explains itself — editing a conversation the server has
   * moved past would either be refused or, worse, replace the wrong turn.
   */
  const startEdit = useCallback(
    (item: UserItem) => {
      if (busy || sendBlocked || currentId === null) return;
      const key = draftKey;
      // The command this message was sent with, taken from the message itself:
      // the id the server recorded, and the name its own first token writes.
      // Whatever the composer happens to be holding is not this message's.
      const name = commandNameIn(item.text);
      const command = item.commandId && name ? { id: item.commandId, name } : null;
      fetchSession(currentId)
        .then((detail) => {
          if (detail.revision === undefined) return;
          drafts.startEdit(key, {
            turnId: item.turnId ?? "",
            revision: detail.revision,
            command,
            text: item.text,
          });
          fieldRef.current?.focus();
          sticky.stick();
        })
        .catch((e: unknown) => {
          showToast("err", `无法开始编辑: ${e instanceof Error ? e.message : String(e)}`);
        });
    },
    [busy, currentId, draftKey, drafts, sendBlocked, showToast, sticky],
  );

  // One composer, two placements: the centred first-run card, or docked over
  // the message stream. Only one of them is mounted at a time.
  const renderComposer = (variant: "tall" | "docked") => (
    <ComposerBar
      variant={variant}
      value={composer.input}
      placeholder="向 Easy code 提问，使用 / 运行命令…"
      ariaLabel="给 Easy code 发送消息"
      busy={busy}
      sendBlocked={sendBlocked}
      permission={permission}
      permissionDisabled={busy || sendBlocked}
      onPermission={requestPermissionChange}
      onChange={composer.handleChange}
      onKeyDown={composer.onKeyDown}
      onSelectionChange={composer.setCaret}
      onComposingChange={composer.handleComposingChange}
      onCompositionEnd={(v, nextCaret) => composer.syncComposer(v, nextCaret)}
      fieldRef={fieldRef}
      menuOpen={composer.mentionOpen || composer.cmdOpen}
      menuId={composer.mentionOpen ? MENTION_LIST_ID : COMMAND_LIST_ID}
      activeOptionId={
        composer.mentionOpen
          ? composer.mentionMatches.length
            ? `${MENTION_OPTION_PREFIX}${Math.min(composer.mentionIndex, composer.mentionMatches.length - 1)}`
            : undefined
          : undefined
      }
      onSend={composer.submit}
      editing={
        composer.editingDraft
          ? { text: composer.editingDraft.text, onCancel: cancelEdit }
          : undefined
      }
      onStop={stop}
      hint="Enter 发送 · Shift + Enter 换行"
      model={
        <ModelPicker
          models={models}
          current={models.default}
          sessionAlias={currentId ? (currentSession?.model_alias ?? null) : null}
          switchingBlocked={anyBusy}
          onChange={(next) => {
            setModels(next);
            // Switching rebinds every live session, so the reply meta rows and
            // the sidebar must be re-read rather than assume the old binding.
            refreshSessions();
          }}
          onError={(msg) => showToast("err", msg)}
        />
      }
      voice={composer.voice}
      menu={
        composer.mentionOpen ? (
          <MentionMenu
            state={composer.mentionCurrent}
            query={composer.mentionInfo?.query ?? ""}
            index={composer.mentionIndex}
            onPick={composer.pickMention}
            onClose={composer.dismissMention}
            onRetry={composer.retryMention}
          />
        ) : (
          <CommandMenu
            state={composer.commandState}
            open={composer.cmdOpen}
            query={composer.input}
            index={composer.cmdIndex}
            onPick={composer.pickCommand}
            onRetry={composer.retryCommands}
            onClose={composer.closeCmdMenu}
          />
        )
      }
    />
  );

  return (
    <div className={`app${sidebarOpen ? " sidebar-open" : ""}`}>
      <Sidebar
        currentId={currentId}
        activity={activity}
        archived={archived}
        showArchived={showArchived}
        collapsedSections={collapsedSections}
        collapsedProjects={collapsedProjects}
        busy={busy}
        sessionBlocked={sessionBlocked}
        viewToken={openSeqRef}
        workspaces={workspaces}
        workspacesLoaded={workspacesLoaded}
        groups={groups}
        chosenRoot={chosenRoot}
        currentRoot={currentRoot}
        secondary={secondary}
        rootEditable={rootEditable}
        onNewSession={newSession}
        newSessionLabel={newSessionLabel}
        onOpenSession={openSession}
        onToggleSection={toggleSection}
        onToggleCollapsed={toggleProjectCollapsed}
        onNewChatInProject={newChatInProject}
        onProjectAction={runProjectAction}
        onSetChosenRoot={chooseRoot}
        onSetSecondary={setSecondary}
        onProjects={setProjects}
        onSessionWorkspace={sessionWorkspaceChanged}
        onToggleArchived={() => setShowArchived(!showArchived)}
        onTogglePin={togglePin}
        onDeleteSession={setDeleteTarget}
        onRestoreSession={restoreArchived}
        onOpenExtensions={() => openExtensions("skills", currentId ? currentRoot : chosenRoot)}
        onError={(msg) => showToast("err", msg)}
      />
      {sidebarOpen && (
        <div
          className="sidebar-overlay"
          data-testid="sidebar-overlay"
          onClick={closeSidebar}
          aria-hidden="true"
        />
      )}
      <main className="chat" ref={chatRef}>
        <div className="chat-card">
          <div className="chat-top">
            <TabBar
              tabs={tabs}
              currentId={currentId}
              activity={activity}
              onSelect={selectTab}
              onClose={closeTab}
              onNew={newSession}
            />
            <div className="chat-actions">
              <button
                type="button"
                className="sidebar-toggle"
                aria-label="打开侧栏"
                aria-controls="sidebar"
                aria-expanded={sidebarOpen}
                title="打开侧栏"
                onClick={() => setSidebarOpen(true)}
              >
                <span aria-hidden="true">☰</span>
              </button>
              <span
                className={`connection-state ${busy ? "working" : ""} ${pendingApproval ? "clickable" : ""}`}
                role={pendingApproval ? "button" : undefined}
                title={pendingApproval ? "查看待批准的操作" : undefined}
                onClick={pendingApproval ? revealApproval : undefined}
              >
                <span aria-hidden="true" />
                {busy ? (pendingApproval ? "等待批准" : "正在工作") : "就绪"}
              </span>
              <button
                type="button"
                className={`icon-btn${pane.open ? " on" : ""}`}
                aria-label={pane.open ? "收起面板" : "展开面板"}
                aria-pressed={pane.open}
                title={pane.open ? "收起面板" : "展开面板"}
                ref={paneToggleRef}
                onClick={() => (pane.open ? pane.close() : pane.setOpen(true))}
              >
                <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
                  <rect x="3.5" y="4.5" width="17" height="15" rx="2.5" />
                  <path d="M14.5 4.5v15" />
                </svg>
              </button>
            </div>
          </div>
          <div
            className={`chat-main${isEmptyStage ? "" : " docked"}`}
            ref={mainRef}
            style={
              composerHeight
                ? ({ "--composer-h": `${composerHeight}px` } as CSSProperties)
                : undefined
            }
          >
            {items.length === 0 && sessionLoad?.status === "loading" && (
              <div className="session-loading" role="status">
                正在加载会话…
              </div>
            )}
            {items.length === 0 && sessionLoad?.status === "error" && (
              <div className="session-load-error" role="alert">
                <strong>会话加载失败：{sessionLoad.message}</strong>
                <button type="button" className="retry-btn" onClick={() => openSession(currentId)}>
                  重试
                </button>
              </div>
            )}
            {isEmptyStage && (
              <EmptyState
                context={draftContext}
                onPick={(text) => {
                  insertDraftText(text);
                  fieldRef.current?.focus();
                }}
              >
                {renderComposer("tall")}
              </EmptyState>
            )}
            {!isEmptyStage && (
              <>
                <ChatMessages
                  items={items}
                  busy={busy}
                  currentModelName={currentModelName}
                  onDecide={decideApproval}
                  onContinue={continueUnfinished}
                  onEdit={busy || sendBlocked ? undefined : startEdit}
                  onOpenTasks={() => {
                    pane.chooseSection("tasks");
                    pane.setOpen(true);
                  }}
                />
                {busy && !activeTool && !pendingApproval && (
                  <LoadingState
                    label={exploration.reads || exploration.searches ? "正在探索" : "思考中"}
                  />
                )}
              </>
            )}
          </div>
          {!isEmptyStage && (
            <div className="composer-area" ref={attachComposer}>
              {sticky.unread && (
                <button type="button" className="scroll-latest" onClick={sticky.stick}>
                  <span aria-hidden="true">↓</span> 有新内容 · 回到最新
                </button>
              )}
              {renderComposer("docked")}
            </div>
          )}
          <SelectionActions
            containerRef={mainRef}
            onPick={(quoted, instruction) => {
              insertDraftText(`${instruction}\n\n> ${quoted.replace(/\n/g, "\n> ")}\n`);
              fieldRef.current?.focus();
            }}
          />
        </div>
        <RightPane
          open={pane.open}
          overlay={chatNarrow}
          sections={PANE_SECTIONS}
          active={pane.section}
          onSelect={pane.chooseSection}
          onClose={pane.close}
        >
          <PaneBody pane={pane} draftKey={draftKey} sessionId={currentId} />
        </RightPane>
        {toast && (
          <div className={`app-toast ${toast.kind}`} role="status">
            {toast.text}
          </div>
        )}
        <Modal
          open={allowAllConfirm}
          onClose={() => setAllowAllConfirm(false)}
          title="确认完全访问"
          variant="permission-warning-modal"
          actions={
            <>
              <button type="button" className="modal-cancel" onClick={() => setAllowAllConfirm(false)}>
                取消
              </button>
              <button
                type="button"
                className="danger"
                onClick={() => {
                  setAllowAllConfirm(false);
                  void changePermission("allow-all", true);
                }}
              >
                确认完全访问
              </button>
            </>
          }
        >
          <p className="modal-desc">
            完全访问会关闭文件沙箱和审批，让 Easy code 可以读写宿主机上的文件并执行命令。
            仅在你确认模型和任务可信时使用。
          </p>
        </Modal>
        {extensionTarget && (
          <ExtensionsSettings
            // A new target is a new dialog: its tab, its loaded panels and the
            // project it names all belong to this one opening.
            key={`${extensionTarget.root ?? ""}|${extensionTarget.sessionId ?? ""}|${extensionTarget.initialTab}`}
            {...extensionTarget}
            projectName={projectLabel(extensionTarget.root)}
            projectPath={extensionTarget.root ?? workspaces.default ?? null}
            onClose={() => setExtensionTarget(null)}
            onChanged={() => setExtensionsRevision((n) => n + 1)}
            onToast={showToast}
          />
        )}
        {editTarget && (
          <Modal
            open={Boolean(editTarget)}
            onClose={() => setEditTarget(null)}
            title="编辑项目"
            variant="project-edit-modal"
            actions={
              <>
                <button type="button" className="modal-cancel" onClick={() => setEditTarget(null)}>
                  取消
                </button>
                <button
                  type="button"
                  className="primary"
                  onClick={async () => {
                    try {
                      const r = await saveProject(
                        editTarget.root,
                        projectMeta.get(editTarget.root)?.secondary ?? [],
                        undefined,
                        editTarget.name,
                      );
                      setProjects(r.projects);
                      refreshSessions();
                      showToast("ok", "已保存项目设置");
                    } catch (e) {
                      showToast("err", e instanceof Error ? e.message : String(e));
                    }
                    setEditTarget(null);
                  }}
                >
                  保存
                </button>
              </>
            }
          >
            <label className="modal-field">
              <span>项目名称</span>
              <input
                type="text"
                value={editTarget.name}
                onChange={(e) => setEditTarget({ ...editTarget, name: e.target.value })}
                autoFocus
              />
            </label>
            <SecondaryEditor
              root={editTarget.root}
              secondary={projectMeta.get(editTarget.root)?.secondary ?? []}
              disabled={false}
              viewToken={openSeqRef}
              onSecondary={() => {}}
              onProjects={setProjects}
              onError={(msg) => showToast("err", msg)}
            />
          </Modal>
        )}
        {deleteTarget && (
          <Modal
            open={Boolean(deleteTarget)}
            onClose={() => setDeleteTarget(null)}
            title="删除会话"
            variant="project-remove-modal"
            actions={
              <>
                <button type="button" className="modal-cancel" onClick={() => setDeleteTarget(null)}>
                  取消
                </button>
                <button type="button" className="danger" onClick={confirmDeleteSession}>
                  确认删除
                </button>
              </>
            }
          >
            <p className="modal-desc">
              将永久删除「{deleteTarget.title}」及其全部消息（不可恢复）。
            </p>
          </Modal>
        )}
        {removeTarget && (
          <Modal
            open={Boolean(removeTarget)}
            onClose={() => setRemoveTarget(null)}
            title="移除项目"
            variant="project-remove-modal"
            actions={
              <>
                <button type="button" className="modal-cancel" onClick={() => setRemoveTarget(null)}>
                  取消
                </button>
                <button type="button" className="danger" onClick={confirmRemoveProject}>
                  确认移除
                </button>
              </>
            }
          >
            <p className="modal-desc">
              将删除「{removeTarget.root ? basename(removeTarget.root) : DEFAULT_PROJECT}」的绑定，
              并删除其下 {removeTarget.count} 条会话（不可恢复）。
            </p>
          </Modal>
        )}
      </main>
    </div>
  );
}
