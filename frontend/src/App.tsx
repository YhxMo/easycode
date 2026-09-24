import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type {
  CommandInfo,
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
  fetchSession,
  fetchSessions,
  fetchWorkspaces,
  pinProject,
  removeProject,
  revealInFinder,
  saveProject,
  setSessionArchived,
  setSessionPermission,
  submitApproval,
} from "./api";
import { useChatStream } from "./useChatStream";
import { currentTurn } from "./chatStream";
import { historyToItems } from "./lib/history";
import type { ApprovalState, Item } from "./types";
import { ApprovalSheet } from "./ApprovalSheet";
import { CommandMenu } from "./CommandMenu";
import { filterCommands, clampCommandIndex, moveCommandCursor } from "./lib/commands";
import { Modal } from "./components/Modal";
import { ChatMessages } from "./components/ChatMessages";
import { Sidebar } from "./components/Sidebar";
import { PermissionPicker } from "./PermissionPicker";
import type { ProjectAction } from "./ProjectMenu";
import { basename, DEFAULT_PROJECT } from "./lib/paths";
import { SecondaryEditor } from "./SecondaryEditor";

const EMPTY_MODELS: ModelsInfo = { default: "", models: {}, providers: {}, limits: {} };

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
  const [overlayOpen, setOverlayOpen] = useState(true);
  const [commandResult, setCommandResult] = useState<{ scope: string; commands: CommandInfo[] }>({
    scope: "",
    commands: [],
  });
  const [cmdOpen, setCmdOpen] = useState(false);
  const [cmdIndex, setCmdIndex] = useState(0);
  // on mobile (<=760px) the sidebar is hidden; `sidebarOpen` drives the
  // drawer overlay so core session/project/model navigation stays reachable.
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const mainRef = useRef<HTMLDivElement>(null);
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

  const { send, stop, busy, items, setItems, detach } = useChatStream({
    input,
    currentId,
    chosenRoot,
    secondary,
    permission,
    currentModelName,
    sendBlocked,
    refreshSessions,
    setInput,
    setCurrentId,
    onApprovalRequired: () => setOverlayOpen(true),
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
    fetchArchivedSessions().then(setArchived).catch(() => {});
  }, [setArchived]);

  useEffect(() => {
    refreshSessions();
    fetchModels().then(setModels).catch(() => {});
  }, [refreshSessions]);

  // Command discovery follows the exact foreground scope: an open session's
  // own roots/skills, or the draft's root + secondary list. A result carries
  // the scope it was fetched for, so a stale menu disappears the moment the
  // scope changes and a late reply can never replace the current one.
  const commandScope = JSON.stringify([currentId, chosenRoot, secondary]);
  const commands = commandResult.scope === commandScope ? commandResult.commands : [];
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

  useEffect(() => {
    refreshArchived();
  }, [refreshArchived]);

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

  // ---- project collapse state (persisted in localStorage) ----
  const [collapsedProjects, setCollapsedProjects] = useState<Record<string, boolean>>(() => {
    try {
      const saved = localStorage.getItem("easycode:collapsed_projects");
      return saved ? JSON.parse(saved) : {};
    } catch {
      return {};
    }
  });

  const toggleProjectCollapsed = useCallback((rootKey: string) => {
    setCollapsedProjects((prev) => {
      const next = { ...prev, [rootKey]: !prev[rootKey] };
      try {
        localStorage.setItem("easycode:collapsed_projects", JSON.stringify(next));
      } catch {
        // localStorage 不可用（隐私模式等）——仅内存态，忽略即可
      }
      return next;
    });
  }, []);

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
      // Switching sessions abandons the previous stream (and releases its busy
      // state) so its events cannot leak into the new view.
      detach();
      setCurrentId(id);
      setItems([]);
      setOverlayOpen(true);
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
          setItems(historyToItems(detail.messages, detail.approvals, detail.user_times));
          setSecondary(detail.secondary_roots ?? []);
          setPermission(detail.permission_mode ?? "ask");
          setSessionLoad(null);
        } catch (e) {
          if (openSeqRef.current !== token) return;
          const message = e instanceof Error ? e.message : String(e);
          setSessionLoad({ status: "error", message });
          showToast("err", `打开会话失败: ${message}`);
        }
      } else {
        setSessionLoad(null);
        setSecondary([]);
        setPermission("ask");
      }
    },
    [showToast, currentId, busy, sessionLoad, detach, setItems],
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

  // sessions grouped by project root (null = default project); pinned projects first
  const groups = useMemo(() => {
    const map = new Map<string | null, SessionSummary[]>();
    for (const s of sessions) {
      const key = s.root ?? null;
      const list = map.get(key);
      if (list) list.push(s);
      else map.set(key, [s]);
    }
    return Array.from(map.entries()).sort((a, b) => {
      const pinnedA = projectMeta.get(a[0]!)?.pinned ?? false;
      const pinnedB = projectMeta.get(b[0]!)?.pinned ?? false;
      if (pinnedA !== pinnedB) return pinnedA ? -1 : 1;
      return (
        Math.max(...b[1].map((s) => Date.parse(s.created_at))) -
        Math.max(...a[1].map((s) => Date.parse(s.created_at)))
      );
    });
  }, [sessions, projectMeta]);

  const currentRoot = currentSession?.root ?? null;
  const projectName = currentRoot ? basename(currentRoot) : DEFAULT_PROJECT;
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
  // Approval counters are scoped to the current turn (items after the last
  // user message): restored history approvals must not inflate "n/m".
  const { pendingApprovals, approvalPosition, approvalTotal } = useMemo(() => {
    const approvals = currentTurn(items).filter(
      (it): it is Extract<Item, { kind: "approval" }> => it.kind === "approval",
    );
    const firstPending = approvals.findIndex((it) => it.state === "pending");
    return {
      pendingApprovals: approvals.filter((it) => it.state === "pending"),
      // the sheet shows the first *pending* approval; its 1-based position
      // advances as earlier approvals are resolved.
      approvalPosition: firstPending >= 0 ? firstPending + 1 : 1,
      approvalTotal: approvals.length,
    };
  }, [items]);
  const pendingApproval = pendingApprovals.length > 0;

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

  return (
    <div className={`app${sidebarOpen ? " sidebar-open" : ""}`}>
      <Sidebar
        currentId={currentId}
        archived={archived}
        showArchived={showArchived}
        collapsedProjects={collapsedProjects}
        busy={busy}
        sessionBlocked={sessionBlocked}
        viewToken={openSeqRef}
        models={models}
        workspaces={workspaces}
        groups={groups}
        projectMeta={projectMeta}
        chosenRoot={chosenRoot}
        currentRoot={currentRoot}
        secondary={secondary}
        onNewSession={newSession}
        onOpenSession={openSession}
        onToggleCollapsed={toggleProjectCollapsed}
        onNewChatInProject={newChatInProject}
        onProjectAction={runProjectAction}
        onSetChosenRoot={chooseRoot}
        onSetSecondary={setSecondary}
        onProjects={setProjects}
        onToggleArchived={() => setShowArchived(!showArchived)}
        onDeleteSession={setDeleteTarget}
        onRestoreSession={restoreArchived}
        onModelChange={setModels}
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
        <header className="chat-header">
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
          <div className="chat-context">
            <strong>{currentId ? currentSession?.title : "新会话"}</strong>
            <span className="context-path" title={currentRoot ?? DEFAULT_PROJECT}>
              <span aria-hidden="true">⌘</span> {projectName}
            </span>
          </div>
          <div className="header-actions">
            <span
              className={`connection-state ${busy ? "working" : ""} ${pendingApproval ? "clickable" : ""}`}
              role={pendingApproval ? "button" : undefined}
              title={pendingApproval ? "打开批准" : undefined}
              onClick={pendingApproval ? () => setOverlayOpen(true) : undefined}
            >
              <span aria-hidden="true" />
              {busy ? (pendingApproval ? "等待批准" : "正在工作") : "已连接"}
            </span>
          </div>
        </header>
        <div className="chat-main" ref={mainRef}>
          {items.length === 0 &&
            (sessionLoad?.status === "loading" ? (
              <div className="session-loading" role="status">
                正在加载会话…
              </div>
            ) : sessionLoad?.status === "error" ? (
              <div className="session-load-error" role="alert">
                <strong>会话加载失败：{sessionLoad.message}</strong>
                <span>请再次点击左侧会话重试。</span>
              </div>
            ) : (
              <div className="empty">
                <div className="empty-icon" aria-hidden="true">&gt;_</div>
                <h2>从一个任务开始</h2>
                <p>描述你想完成的工作，Easy code 会在当前工作区中协助你。</p>
              </div>
            ))}
          <ChatMessages items={items} busy={busy} currentModelName={currentModelName} />
          {busy && !activeTool && !pendingApproval && (
            <div className="agent-status running" role="status" aria-live="polite">
              <strong>{exploration.reads || exploration.searches ? "正在探索" : "思考中"}</strong>
              <span>
                {exploration.reads > 0 && `${exploration.reads} 次读取`}
                {exploration.reads > 0 && exploration.searches > 0 && "，"}
                {exploration.searches > 0 && `${exploration.searches} 次搜索`}
                {!exploration.reads && !exploration.searches && "Planning next steps"}
              </span>
            </div>
          )}
        </div>
        <div className="composer-area">
          <div className="chat-input">
            <div className="cmd-wrap">
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
              <textarea
                value={input}
                aria-label="给 Easy code 发送消息"
                placeholder="向 Easy code 提问，使用 / 运行命令…"
                onChange={(e) => {
                  setInput(e.target.value);
                  setCmdOpen(e.target.value.startsWith("/"));
                  setCmdIndex(0);
                }}
                onKeyDown={(e) => {
                  // IME composition (Chinese/Japanese input): Enter confirms
                  // the candidate text and must never send or pick commands.
                  if (e.nativeEvent.isComposing || e.keyCode === 229) return;
                  if (e.key === "Escape") {
                    setCmdOpen(false);
                    return;
                  }
                  if (cmdOpen) {
                    const filtered = filterCommands(commands, input);
                    const count = filtered.length;
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
                    if (e.key === "Enter" && count > 0) {
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
                }}
              />
            </div>
            <div className="composer-toolbar">
              <div className="composer-options">
                <div className="chat-input-perm">
                  <PermissionPicker
                    compact
                    value={permission}
                    disabled={busy || sendBlocked}
                    onChange={changePermission}
                  />
                </div>
                <span className="composer-hint">Enter 发送 · Shift + Enter 换行</span>
              </div>
              <button
                className={`send-btn ${busy ? "stop" : ""}`}
                aria-label={busy ? "停止生成" : "发送消息"}
                title={busy ? "停止生成" : "发送消息"}
                onClick={busy ? stop : send}
                disabled={busy ? false : !input.trim() || sendBlocked}
              >
                {busy ? <span className="stop-square" /> : <span aria-hidden="true">↑</span>}
              </button>
            </div>
          </div>
        </div>
        {pendingApproval && !overlayOpen && (
          <div className="approval-pill">
            <button
              type="button"
              onClick={() => setOverlayOpen(true)}
              aria-label={`重新打开审批（${pendingApprovals.length} 个）`}
            >
              <svg
                className="approval-pill-arrow"
                viewBox="0 0 24 24"
                aria-hidden="true"
                focusable="false"
              >
                <path d="M12 4.5 4.5 12h4.5v7.5h6V12h4.5z" />
              </svg>
              <span className="approval-pill-label">等待批准</span>
              <span className="approval-pill-count">{pendingApprovals.length}</span>
            </button>
          </div>
        )}
        <ApprovalSheet
          open={pendingApproval && overlayOpen}
          position={approvalPosition}
          total={approvalTotal}
          name={pendingApprovals[0]?.name ?? ""}
          reason={pendingApprovals[0]?.reason}
          scope={pendingApprovals[0]?.scope}
          onDecide={(approve, always) => {
            const first = pendingApprovals[0];
            if (first) decideApproval(first, approve, always);
          }}
          onDismiss={() => setOverlayOpen(false)}
        />
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
