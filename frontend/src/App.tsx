import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { KeyboardEvent as ReactKeyboardEvent } from "react";
import type {
  CommandInfo,
  FileEntry,
  ModelsInfo,
  SessionDetail,
  SessionSummary,
  WorkspacesInfo,
  WorkspaceProject,
} from "./api";
import {
  archiveProjectChats,
  createWorktree,
  deleteSession,
  fetchArchivedSessions,
  fetchCommands,
  fetchModels,
  fetchFiles,
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
} from "./api";
import { DRAFT_KEY, useChatStream } from "./useChatStream";
import { MentionMenu } from "./MentionMenu";
import { ModelPicker } from "./ModelPicker";
import { currentTurn } from "./chatStream";
import { historyToItems } from "./lib/history";
import type { ApprovalState, Item } from "./types";
import { CommandMenu } from "./CommandMenu";
import { filterCommands, clampCommandIndex, moveCommandCursor } from "./lib/commands";
import { Modal } from "./components/Modal";
import { ChatMessages } from "./components/ChatMessages";
import { Sidebar } from "./components/Sidebar";
import { ComposerBar } from "./components/layout/ComposerBar";
import { RightPane, type PaneSection } from "./components/layout/RightPane";
import { EmptyState } from "./components/primitives/EmptyState";
import { LoadingState } from "./components/primitives/LoadingState";
import { TabBar, type OpenTab } from "./components/layout/TabBar";
import { SelectionActions } from "./components/primitives/SelectionActions";
import { ContextCards } from "./components/primitives/ContextCards";
import { DiffView } from "./components/primitives/DiffView";
import { FilePreview } from "./components/primitives/FilePreview";
import { paneData } from "./lib/pane";
import { TaskRows } from "./components/primitives/TaskRows";
import { groupSessions } from "./lib/sessionGroups";
import { applyMention, mentionToken } from "./lib/mention";
import { useVoiceInput } from "./lib/useVoiceInput";
import { usePersistedFlags } from "./lib/usePersistedFlags";
import type { ProjectAction } from "./ProjectMenu";
import { basename, DEFAULT_PROJECT } from "./lib/paths";
import { SecondaryEditor } from "./SecondaryEditor";

const EMPTY_MODELS: ModelsInfo = { default: "", models: {}, providers: {}, limits: {} };

/** Right-hand pane sections; phase 4 fills their bodies. */
const PANE_SECTIONS: PaneSection[] = [
  { id: "tasks", label: "任务" },
  { id: "context", label: "上下文" },
  { id: "changes", label: "变更" },
  { id: "file", label: "文件" },
];

/** Foreground session load phase; null means ready. */
type SessionLoad = { status: "loading" } | { status: "error"; message: string };

export default function App() {
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [currentId, setCurrentId] = useState<string | null>(null);
  const [input, setInput] = useState("");
  const [models, setModels] = useState<ModelsInfo>(EMPTY_MODELS);
  const [workspaces, setWorkspaces] = useState<WorkspacesInfo>({ projects: [] });
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
  const [commandResult, setCommandResult] = useState<{ scope: string; commands: CommandInfo[] }>({
    scope: "",
    commands: [],
  });
  const [cmdOpen, setCmdOpen] = useState(false);
  const [cmdIndex, setCmdIndex] = useState(0);
  // `@` file references: the token under the caret, the listing fetched for it,
  // and whether the user dismissed the menu for this token.
  const [caret, setCaret] = useState(0);
  const [mentionOff, setMentionOff] = useState(false);
  const [mentionIndex, setMentionIndex] = useState(0);
  const [mentionResult, setMentionResult] = useState<{
    scope: string;
    query: string;
    files: FileEntry[];
    total: number;
  }>({ scope: "", query: "", files: [], total: 0 });
  // on mobile (<=760px) the sidebar is hidden; `sidebarOpen` drives the
  // drawer overlay so core session/project/model navigation stays reachable.
  const [sidebarOpen, setSidebarOpen] = useState(false);
  // Open session tabs. Closing one closes only the view: the conversation stays
  // in the sidebar and a running turn keeps streaming into its own slot.
  const [openTabs, setOpenTabs] = useState<string[]>(() => {
    try {
      const saved = localStorage.getItem("easycode:open_tabs");
      return saved ? JSON.parse(saved) : [];
    } catch {
      return [];
    }
  });
  // The pane follows the turn: it opens itself once a turn has produced context
  // or changes, and a choice the user makes (toggle or ✕) holds for that turn
  // only, so the next turn can open it again.
  const [paneChoice, setPaneChoice] = useState<{ turn: number; open: boolean } | null>(null);
  const [sectionChoice, setSectionChoice] = useState<{ turn: number; section: string } | null>(null);
  const [preview, setPreview] = useState<string | null>(null);
  const mainRef = useRef<HTMLDivElement>(null);
  const fieldRef = useRef<HTMLTextAreaElement>(null);
  // View version: a response is applied only while it still matches, and the
  // ref is readable at response time by child editors.
  const openSeqRef = useRef(0);

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

  /** A pending approval lives in the stream: bring the newest one into view. */
  const revealApproval = useCallback(() => {
    const pane = mainRef.current;
    pane?.scrollTo({ top: pane.scrollHeight, behavior: "smooth" });
  }, []);

  const setProjects = useCallback(
    (projects: WorkspaceProject[]) => setWorkspaces((w) => ({ ...w, projects })),
    [],
  );

  const refreshSessions = useCallback(() => {
    fetchSessions().then(setSessions).catch(() => {});
    fetchWorkspaces().then(setWorkspaces).catch(() => {});
  }, []);

  // Resolved ahead of useChatStream so send() can stamp the model name onto the
  // turn's assistant messages (reply meta row).
  const currentSession = sessions.find((s) => s.id === currentId);
  const currentModelName =
    (currentId ? (models.models[currentSession?.model_alias ?? ""]?.model ?? currentSession?.model_alias) : null) ??
    models.models[models.default]?.model ??
    models.default;
  const sessionBlocked = sessionLoad !== null;
  const sendBlocked = sessionBlocked || permissionPending;

  const {
    send,
    stop,
    busy,
    items,
    activity,
    setItems,
    loadHistory,
    dropEntry,
    isStreaming,
  } = useChatStream({
    input,
    currentId,
    chosenRoot,
    secondary,
    permission,
    currentModelName,
    sendBlocked,
    getViewToken: () => openSeqRef.current,
    refreshSessions,
    setInput,
    setCurrentId,
    onApprovalRequired: revealApproval,
  });

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

  // Command discovery follows the exact foreground scope: an open session's
  // own roots/skills, or the draft's root + secondary list. A result carries
  // the scope it was fetched for, so a stale menu disappears the moment the
  // scope changes and a late reply can never replace the current one.
  const commandScope = JSON.stringify([currentId, chosenRoot, secondary]);
  const commands = useMemo(
    () => (commandResult.scope === commandScope ? commandResult.commands : []),
    [commandResult, commandScope],
  );
  useEffect(() => {
    const scope = JSON.stringify([currentId, chosenRoot, secondary]);
    const draft = currentId === null ? { root: chosenRoot, secondary } : undefined;
    let live = true;
    fetchCommands(currentId, draft)
      .then((r) => live && setCommandResult({ scope, commands: r.commands }))
      .catch(() => live && setCommandResult({ scope, commands: [] }));
    return () => {
      live = false;
    };
  }, [currentId, chosenRoot, secondary]);

  // `@` references: fetch the listing for the token under the caret, debounced
  // so typing does not fire a request per keystroke. A reply is used only while
  // it still matches the scope and the query it was fetched for.
  const mentionInfo = useMemo(() => mentionToken(input, caret), [input, caret]);
  const mentionScope = JSON.stringify([currentId, chosenRoot, secondary]);
  const mentionQuery = mentionInfo?.query ?? null;
  useEffect(() => {
    if (mentionQuery === null) return;
    const draft = currentId === null ? { root: chosenRoot, secondary } : undefined;
    let live = true;
    const timer = window.setTimeout(() => {
      const settle = (files: FileEntry[], total: number) =>
        live && setMentionResult({ scope: mentionScope, query: mentionQuery, files, total });
      fetchFiles(currentId, draft, mentionQuery)
        .then((r) => settle(r.files, r.total))
        .catch(() => settle([], 0));
    }, 140);
    return () => {
      live = false;
      window.clearTimeout(timer);
    };
  }, [mentionQuery, mentionScope, currentId, chosenRoot, secondary]);

  const mentionOpen = Boolean(mentionInfo) && !mentionOff && !cmdOpen;
  const mentionMatches = useMemo(
    () =>
      mentionInfo &&
      mentionResult.scope === mentionScope &&
      mentionResult.query === mentionInfo.query
        ? mentionResult.files
        : [],
    [mentionInfo, mentionResult, mentionScope],
  );

  const pickMention = useCallback(
    (file: FileEntry) => {
      const token = mentionInfo;
      if (!token) return;
      const next = applyMention(input, token, file.path);
      setInput(next.value);
      setCaret(next.caret);
      setMentionOff(true);
      window.requestAnimationFrame(() => {
        fieldRef.current?.setSelectionRange(next.caret, next.caret);
      });
    },
    [input, mentionInfo],
  );

  /** Dictated text appends to whatever is already composed. */
  const voice = useVoiceInput((text) => setInput((prev) => (prev ? `${prev} ${text}` : text)));

  useEffect(() => {
    refreshArchived();
  }, [refreshArchived]);

  useEffect(() => {
    try {
      localStorage.setItem("easycode:open_tabs", JSON.stringify(openTabs));
    } catch {
      // localStorage 不可用（隐私模式等）——仅内存态，忽略即可
    }
  }, [openTabs]);

  useEffect(() => {
    // Scroll only the message pane. scrollIntoView can walk up to ancestor
    // overflow containers (e.g. .app with overflow:hidden), scrolling the
    // whole UI out of view.
    const pane = mainRef.current;
    pane?.scrollTo({ top: pane.scrollHeight, behavior: "smooth" });
  }, [items]);

  // ---- toast (shared by openSession failure + project actions) ----
  const [toast, setToast] = useState<{ kind: "ok" | "err"; text: string } | null>(null);
  const toastTimer = useRef<number | null>(null);

  // ---- sidebar collapse state (sections + project groups, in localStorage) ----
  const [collapsedProjects, toggleProjectCollapsed] = usePersistedFlags("easycode:collapsed_projects");
  const [collapsedSections, toggleSection] = usePersistedFlags("easycode:collapsed_sections");

  const showToast = useCallback((kind: "ok" | "err", text: string) => {
    setToast({ kind, text });
    if (toastTimer.current) window.clearTimeout(toastTimer.current);
    toastTimer.current = window.setTimeout(() => setToast(null), 3500);
  }, []);

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
            const restored = historyToItems(detail.messages, detail.approvals, detail.user_times);
            // The task list is session state, not a message: it rides at the end
            // of the stream so a reopened session still shows it.
            if (detail.todos?.length) restored.push({ kind: "todo", todos: detail.todos });
            loadHistory(id, restored);
          }
          setSecondary(detail.secondary_roots ?? []);
          setPermission(detail.permission_mode ?? "ask");
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
    [showToast, currentId, busy, sessionLoad, loadHistory, dropEntry, isStreaming],
  );

  const projectMeta = useMemo(() => {
    const map = new Map<string | null, WorkspaceProject>();
    for (const p of workspaces.projects ?? []) map.set(p.root ?? null, p);
    return map;
  }, [workspaces]);

  const chooseRoot = useCallback((root: string | null) => {
    ++openSeqRef.current;
    setChosenRoot(root);
  }, []);

  const newSession = useCallback(() => {
    openSession(null);
    setChosenRoot(null);
    setSidebarOpen(false);
    refreshSessions();
  }, [openSession, refreshSessions]);

  // ---- project row actions (new chat / more menu) ----
  const [editTarget, setEditTarget] = useState<{ root: string | null; name: string } | null>(null);
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
    // Clear the view only when it is still the one the delete targeted: a
    // switch during the delete must not blank the new view.
    if (isCurrent && openSeqRef.current === viewToken) openSession(null);
    setDeleteTarget(null);
    refreshSessions();
    refreshArchived();
  }, [
    deleteTarget,
    currentId,
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
      await setSessionArchived(session.id, false).catch(() => {});
      setArchived((prev) => prev.filter((x) => x.id !== session.id));
      refreshSessions();
    },
    [refreshSessions, setArchived],
  );

  const newChatInProject = useCallback(
    (root: string | null) => {
      const proj = root ? projectMeta.get(root) : null;
      openSession(null);
      setChosenRoot(root);
      setSecondary(proj?.secondary ?? []);
      setSidebarOpen(false);
      refreshSessions();
    },
    [openSession, projectMeta, refreshSessions],
  );

  const runProjectAction = useCallback(
    async (root: string | null, action: ProjectAction) => {
      try {
        if (action === "edit") {
          const proj = root ? projectMeta.get(root) : projectMeta.get(null);
          setEditTarget({ root, name: proj?.name ?? (root ? basename(root) : DEFAULT_PROJECT) });
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
    try {
      const r = await removeProject(removedRoot);
      setProjects(r.projects);
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
  // The foreground conversation always has a tab — including the instant it is
  // created — and the draft gets one, so the strip always names what is on
  // screen.
  const tabIds = currentId && !openTabs.includes(currentId) ? [...openTabs, currentId] : openTabs;
  const sessionTabs: OpenTab[] = tabIds.map((id) => ({
    id,
    title: sessions.find((s) => s.id === id)?.title ?? "会话",
  }));
  const tabs: OpenTab[] =
    currentId === null ? [{ id: DRAFT_KEY, title: "新会话", draft: true }, ...sessionTabs] : sessionTabs;

  const closeTab = useCallback(
    (id: string) => {
      const index = openTabs.indexOf(id);
      const remaining = openTabs.filter((t) => t !== id);
      setOpenTabs(remaining);
      // Dropping an idle conversation makes reopening it reload from disk; a
      // running turn keeps its slot so its output is not lost.
      dropEntry(id);
      if (id !== currentId) return;
      const neighbour = remaining[index] ?? remaining[index - 1] ?? null;
      openSession(neighbour);
    },
    [openTabs, currentId, dropEntry, openSession],
  );

  const currentRoot = currentSession?.root ?? null;
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
  // Scoped to the current turn (items after the last user message): restored
  // history approvals must not read as work waiting on the user now.
  const pendingApprovals = useMemo(
    () =>
      currentTurn(items).filter(
        (it): it is Extract<Item, { kind: "approval" }> =>
          it.kind === "approval" && it.state === "pending",
      ),
    [items],
  );
  const pendingApproval = pendingApprovals.length > 0;
  const turn = useMemo(() => currentTurn(items), [items]);
  // Turns are counted by user messages: a choice made in one turn must not
  // carry over to the next.
  const turnNo = useMemo(() => items.filter((it) => it.kind === "user").length, [items]);
  const pane = useMemo(() => paneData(turn), [turn]);
  const todos = useMemo(() => {
    for (let i = items.length - 1; i >= 0; i -= 1) {
      const item = items[i];
      if (item.kind === "todo") return item.todos;
    }
    return [];
  }, [items]);
  // Only turns produced in this view drive the pane: a restored session's
  // history must not pop it open on load.
  const liveTurn =
    busy || turn.some((it) => it.kind === "assistant" && typeof it.durationMs === "number");
  const hasArtifacts = pane.context.length > 0 || pane.changes.length > 0 || todos.length > 0;
  const paneOpen = paneChoice?.turn === turnNo ? paneChoice.open : liveTurn && hasArtifacts;
  const paneSection =
    sectionChoice?.turn === turnNo
      ? sectionChoice.section
      : pane.changes.length
        ? "changes"
        : todos.length
          ? "tasks"
          : "context";
  const setPane = useCallback(
    (open: boolean) => setPaneChoice({ turn: turnNo, open }),
    [turnNo],
  );
  const chooseSection = useCallback(
    (section: string) => setSectionChoice({ turn: turnNo, section }),
    [turnNo],
  );
  // The centred first-run stage replaces the stream until a conversation has
  // something to show; a load in progress is never masked by it.
  const isEmptyStage = items.length === 0 && sessionLoad === null;

  const changePermission = useCallback(
    async (mode: string) => {
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
        const r = await setSessionPermission(id, mode);
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

  // Composer keys: command-menu navigation first, then Enter to send.
  const onComposerKeyDown = useCallback(
    (e: ReactKeyboardEvent<HTMLTextAreaElement>) => {
      // IME composition (Chinese/Japanese input): Enter confirms the candidate
      // text and must never send or pick commands.
      if (e.nativeEvent.isComposing || e.keyCode === 229) return;
      if (e.key === "Escape") {
        // the mention menu is the innermost context: dismiss it first
        if (mentionOpen) setMentionOff(true);
        else setCmdOpen(false);
        return;
      }
      if (mentionOpen) {
        if (e.key === "ArrowDown") {
          e.preventDefault();
          setMentionIndex((i) => Math.min(i + 1, Math.max(0, mentionMatches.length - 1)));
          return;
        }
        if (e.key === "ArrowUp") {
          e.preventDefault();
          setMentionIndex((i) => Math.max(0, i - 1));
          return;
        }
        if ((e.key === "Enter" || e.key === "Tab") && mentionMatches.length > 0) {
          e.preventDefault();
          const file = mentionMatches[Math.min(mentionIndex, mentionMatches.length - 1)];
          if (file) pickMention(file);
          return;
        }
      }
      if (cmdOpen) {
        const filtered = filterCommands(commands, input);
        if (e.key === "ArrowDown") {
          e.preventDefault();
          setCmdIndex((i) => moveCommandCursor(filtered, i, +1));
          return;
        }
        if (e.key === "ArrowUp") {
          e.preventDefault();
          setCmdIndex((i) => moveCommandCursor(filtered, i, -1));
          return;
        }
        if (e.key === "Enter" && filtered.length > 0) {
          e.preventDefault();
          const pick = filtered[clampCommandIndex(filtered, cmdIndex)];
          if (pick) {
            setInput(`/${pick.name} `);
            setCmdOpen(false);
            setCmdIndex(0);
          }
          return;
        }
      }
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        send();
      }
    },
    [cmdOpen, commands, input, cmdIndex, send, mentionOpen, mentionMatches, mentionIndex, pickMention],
  );

  // One composer, two placements: the centred first-run card, or docked over
  // the message stream. Only one of them is mounted at a time.
  const composer = (variant: "tall" | "docked") => (
    <ComposerBar
      variant={variant}
      value={input}
      placeholder="向 Easy code 提问，使用 / 运行命令…"
      ariaLabel="给 Easy code 发送消息"
      busy={busy}
      sendBlocked={sendBlocked}
      permission={permission}
      permissionDisabled={busy || sendBlocked}
      onPermission={changePermission}
      onChange={(v, nextCaret) => {
        setInput(v);
        setCaret(nextCaret);
        setCmdOpen(v.startsWith("/"));
        setCmdIndex(0);
        setMentionIndex(0);
        setMentionOff(false);
      }}
      onKeyDown={onComposerKeyDown}
      onSelectionChange={setCaret}
      fieldRef={fieldRef}
      onSend={send}
      onStop={stop}
      hint={variant === "docked" ? "Enter 发送 · Shift + Enter 换行" : undefined}
      model={
        <ModelPicker
          models={models}
          current={models.default}
          onChange={setModels}
          onError={(msg) => showToast("err", msg)}
        />
      }
      voice={voice}
      menu={
        mentionOpen ? (
          <MentionMenu
            files={mentionMatches}
            open
            query={mentionInfo?.query ?? ""}
            index={mentionIndex}
            total={mentionResult.total}
            onPick={pickMention}
            onClose={() => setMentionOff(true)}
          />
        ) : (
          <CommandMenu
            commands={commands}
            open={cmdOpen}
            query={input}
            index={cmdIndex}
            onPick={(c) => {
              setInput(`/${c.name} `);
              setCmdOpen(false);
              setCmdIndex(0);
            }}
            onClose={() => setCmdOpen(false)}
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
        groups={groups}
        chosenRoot={chosenRoot}
        currentRoot={currentRoot}
        secondary={secondary}
        onNewSession={newSession}
        onOpenSession={openSession}
        onToggleSection={toggleSection}
        onToggleCollapsed={toggleProjectCollapsed}
        onNewChatInProject={newChatInProject}
        onProjectAction={runProjectAction}
        onSetChosenRoot={chooseRoot}
        onSetSecondary={setSecondary}
        onProjects={setProjects}
        onToggleArchived={() => setShowArchived(!showArchived)}
        onTogglePin={togglePin}
        onDeleteSession={setDeleteTarget}
        onRestoreSession={restoreArchived}
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
      <main className="chat">
        <div className="chat-card">
          <div className="chat-top">
            <TabBar
              tabs={tabs}
              currentId={currentId}
              activity={activity}
              onSelect={openSession}
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
                {busy ? (pendingApproval ? "等待批准" : "正在工作") : "已连接"}
              </span>
              <button
                type="button"
                className={`icon-btn${paneOpen ? " on" : ""}`}
                aria-label={paneOpen ? "收起面板" : "展开面板"}
                aria-pressed={paneOpen}
                title={paneOpen ? "收起面板" : "展开面板"}
                onClick={() => setPane(!paneOpen)}
              >
                <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
                  <rect x="3.5" y="4.5" width="17" height="15" rx="2.5" />
                  <path d="M14.5 4.5v15" />
                </svg>
              </button>
            </div>
          </div>
          <div className={`chat-main${isEmptyStage ? "" : " docked"}`} ref={mainRef}>
            {items.length === 0 && sessionLoad?.status === "loading" && (
              <div className="session-loading" role="status">
                正在加载会话…
              </div>
            )}
            {items.length === 0 && sessionLoad?.status === "error" && (
              <div className="session-load-error" role="alert">
                <strong>会话加载失败：{sessionLoad.message}</strong>
                <span>请再次点击左侧会话重试。</span>
              </div>
            )}
            {isEmptyStage && (
              <EmptyState onPick={setInput}>{composer("tall")}</EmptyState>
            )}
            {!isEmptyStage && (
              <>
                <ChatMessages
                  items={items}
                  busy={busy}
                  currentModelName={currentModelName}
                  onDecide={decideApproval}
                  onOpenTasks={() => {
                    chooseSection("tasks");
                    setPane(true);
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
          {!isEmptyStage && <div className="composer-area">{composer("docked")}</div>}
          <SelectionActions
            containerRef={mainRef}
            onPick={(quoted, instruction) => {
              setInput(`${instruction}\n\n> ${quoted.replace(/\n/g, "\n> ")}\n`);
              fieldRef.current?.focus();
            }}
          />
        </div>
        <RightPane
          open={paneOpen}
          sections={PANE_SECTIONS}
          active={paneSection}
          onSelect={chooseSection}
          onClose={() => setPane(false)}
        >
          {paneSection === "tasks" && <TaskRows todos={todos} />}
          {paneSection === "context" && (
            <ContextCards
              cards={pane.context}
              onOpen={(source) => {
                setPreview(source);
                chooseSection("file");
              }}
            />
          )}
          {paneSection === "changes" &&
            (pane.changes.length ? (
              pane.changes.map((change) => (
                <DiffView key={change.id} path={change.path} diff={change.diff} />
              ))
            ) : (
              <p className="pane-empty">这一轮还没有修改文件。</p>
            ))}
          {paneSection === "file" &&
            (preview && currentId ? (
              <FilePreview
                key={preview}
                sessionId={currentId}
                path={preview}
                onBack={() => setPreview(null)}
              />
            ) : (
              <p className="pane-empty">从「上下文」里打开一个文件查看内容。</p>
            ))}
        </RightPane>
        {toast && (
          <div className={`app-toast ${toast.kind}`} role="status">
            {toast.text}
          </div>
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
