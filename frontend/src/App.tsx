import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type {
  ApprovalRecord,
  CommandInfo,
  ModelsInfo,
  SessionDetail,
  SessionSummary,
  WorkspacesInfo,
  WorkspaceProject,
} from "./api";
import {
  archiveProjectChats,
  cancelSessionChat,
  createWorktree,
  deleteSession,
  fetchArchivedSessions,
  fetchCommands,
  fetchModels,
  fetchSession,
  fetchSessions,
  fetchWorkspaces,
  pinProject,
  redoSession,
  removeProject,
  revealInFinder,
  saveProject,
  setSessionArchived,
  setSessionPermission,
  streamChat,
  submitApproval,
  undoSession,
} from "./api";
import type { ChatOptions } from "./api";
import { ApprovalSheet } from "./ApprovalSheet";
import { CommandMenu } from "./CommandMenu";
import { ModelPicker } from "./ModelPicker";
import { PermissionPicker } from "./PermissionPicker";
import { ProjectMenu } from "./ProjectMenu";
import type { ProjectAction } from "./ProjectMenu";
import { ProjectPicker, basename } from "./ProjectPicker";
import { SecondaryEditor } from "./SecondaryEditor";
import { ToolCard } from "./ToolCard";

type ApprovalState = "pending" | "approved" | "denied" | "expired";

type Item =
  | { kind: "user"; text: string; time?: string }
  | { kind: "assistant"; text: string }
  | { kind: "tool"; id: string; name: string; args: Record<string, unknown>; result?: string; done: boolean }
  | { kind: "approval"; id: string; toolCallId: string; name: string; args: Record<string, unknown>; reason?: string; scope?: string; state: ApprovalState }
  | { kind: "review"; text: string }
  | { kind: "notice"; text: string }
  | { kind: "error"; text: string };

type RollbackInfo = {
  count: number;
  prompt: string;
  files: number;
  messageOnly: boolean;
};

const EMPTY_MODELS: ModelsInfo = { default: "deepseek-v4flash", models: {} };
const DEFAULT_PROJECT = "default project";

function FolderIcon() {
  return (
    <svg className="project-folder-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <path d="M3.5 7.5h6l1.8 2h9.2v7.8a2.7 2.7 0 0 1-2.7 2.7H6.2a2.7 2.7 0 0 1-2.7-2.7V7.5Z" />
      <path d="M3.5 7.5V6.7A2.7 2.7 0 0 1 6.2 4h3.1l2 2h3.2" />
    </svg>
  );
}

function UndoIcon() {
  return (
    <svg className="message-action-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <path d="M9 7 4 12l5 5" />
      <path d="M4 12h10a6 6 0 0 1 6 6" />
    </svg>
  );
}

function CopyIcon({ copied = false }: { copied?: boolean }) {
  if (copied) {
    return (
      <svg className="message-action-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
        <path d="m5 12 4 4L19 6" />
      </svg>
    );
  }
  return (
    <svg className="message-action-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <rect x="8" y="8" width="11" height="11" rx="1.5" />
      <path d="M16 8V5.5A1.5 1.5 0 0 0 14.5 4h-9A1.5 1.5 0 0 0 4 5.5v9A1.5 1.5 0 0 0 5.5 16H8" />
    </svg>
  );
}

function normalizeToolArgs(value: unknown): Record<string, unknown> {
  if (value && typeof value === "object" && !Array.isArray(value)) {
    return value as Record<string, unknown>;
  }
  if (typeof value === "string") {
    try {
      const parsed = JSON.parse(value);
      if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
        return parsed as Record<string, unknown>;
      }
    } catch {
      return { input: value };
    }
  }
  return {};
}

function currentTimeLabel(): string {
  return new Intl.DateTimeFormat("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date());
}

function formatClock(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return currentTimeLabel();
  return new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit" }).format(d);
}

function historyToItems(
  messages: any[],
  approvals: ApprovalRecord[] = [],
  userTimes: string[] = [],
): Item[] {
  const items: Item[] = [];
  const toolResults = new Map<string, string>();
  for (const m of messages) {
    if (m.role === "tool" && m.tool_call_id) toolResults.set(String(m.tool_call_id), String(m.content ?? ""));
  }
  const approvalByCall = new Map<string, ApprovalRecord>();
  for (const a of approvals) approvalByCall.set(String(a.tool_call_id), a);
  let userIndex = 0;
  for (const m of messages) {
    if (m.role === "user") {
      items.push({ kind: "user", text: String(m.content ?? ""), time: userTimes[userIndex] });
      userIndex += 1;
    } else if (m.role === "assistant") {
      if (m.content) items.push({ kind: "assistant", text: String(m.content) });
      if (m.tool_calls?.length) {
        for (const tc of m.tool_calls) {
          if (!tc.function) continue;
          const id = String(tc.id ?? "");
          const approval = approvalByCall.get(id);
          if (approval) {
            items.push({
              kind: "approval",
              id: `hist-${id}`,
              toolCallId: id,
              name: approval.name,
              args: approval.args ?? {},
              reason: approval.reason,
              scope: approval.scope,
              state: approval.decision,
            });
          }
          items.push({
            kind: "tool",
            id,
            name: tc.function.name,
            args: normalizeToolArgs(tc.function.arguments),
            result: toolResults.get(id),
            done: true,
          });
        }
      }
    }
  }
  return items;
}

export default function App() {
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [currentId, setCurrentId] = useState<string | null>(null);
  const [items, setItems] = useState<Item[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [models, setModels] = useState<ModelsInfo>(EMPTY_MODELS);
  const [workspaces, setWorkspaces] = useState<WorkspacesInfo>({ default: "", projects: [] });
  const [chosenRoot, setChosenRoot] = useState<string | null>(null);
  const [chosenSecondary, setChosenSecondary] = useState<string[]>([]);
  const [currentSecondary, setCurrentSecondary] = useState<string[]>([]);
  const [chosenPermission, setChosenPermission] = useState<string>("ask");
  const [currentPermission, setCurrentPermission] = useState<string>("ask");
  const [overlayOpen, setOverlayOpen] = useState(true);
  const [commands, setCommands] = useState<CommandInfo[]>([]);
  const [cmdOpen, setCmdOpen] = useState(false);
  const [cmdIndex, setCmdIndex] = useState(0);
  const [rollbackInfo, setRollbackInfo] = useState<RollbackInfo | null>(null);
  const [copiedMessage, setCopiedMessage] = useState<number | null>(null);
  const endRef = useRef<HTMLDivElement>(null);

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
    [],
  );

  const refreshSessions = useCallback(() => {
    fetchSessions().then(setSessions).catch(() => {});
    fetchWorkspaces().then(setWorkspaces).catch(() => {});
  }, []);

  const refreshArchived = useCallback(() => {
    fetchArchivedSessions().then(setArchived).catch(() => {});
  }, []);

  useEffect(() => {
    refreshSessions();
    fetchModels().then(setModels).catch(() => {});
    fetchCommands()
      .then((r) => setCommands(r.commands))
      .catch(() => {});
  }, [refreshSessions]);

  useEffect(() => {
    refreshArchived();
  }, [refreshArchived]);

  useEffect(() => {
    // Scroll only the message pane. scrollIntoView can walk up to ancestor
    // overflow containers (e.g. .app with overflow:hidden), scrolling the
    // whole UI out of view.
    const pane = document.querySelector(".chat-main");
    pane?.scrollTo({ top: pane.scrollHeight, behavior: "smooth" });
  }, [items]);

  const openSession = useCallback(
    async (id: string | null) => {
      setCurrentId(id);
      setItems([]);
      setRollbackInfo(null);
      setOverlayOpen(true);
      if (id) {
        try {
          const detail: SessionDetail = await fetchSession(id);
          setItems(historyToItems(detail.messages, detail.approvals, detail.user_times));
          setCurrentSecondary(detail.secondary_roots ?? []);
          setCurrentPermission(detail.permission_mode ?? "ask");
        } catch {
          // session gone
        }
      } else {
        setCurrentSecondary([]);
        setCurrentPermission("ask");
      }
    },
    [],
  );

  const projectMeta = useMemo(() => {
    const map = new Map<string | null, WorkspaceProject>();
    for (const p of workspaces.projects ?? []) map.set(p.root ?? null, p);
    return map;
  }, [workspaces]);

  const newSession = useCallback(() => {
    openSession(null);
    setChosenRoot(null);
    setChosenSecondary([]);
    setChosenPermission("ask");
    refreshSessions();
  }, [openSession, refreshSessions]);

  // ---- project row actions (new chat / more menu) ----
  const [toast, setToast] = useState<{ kind: "ok" | "err"; text: string } | null>(null);
  const [editTarget, setEditTarget] = useState<{ root: string | null; name: string } | null>(null);
  const [removeTarget, setRemoveTarget] = useState<{ root: string | null; count: number } | null>(null);
  const [archived, setArchived] = useState<SessionSummary[]>([]);
  const [showArchived, setShowArchived] = useState(false);
  const toastTimer = useRef<number | null>(null);

  const showToast = useCallback((kind: "ok" | "err", text: string) => {
    setToast({ kind, text });
    if (toastTimer.current) window.clearTimeout(toastTimer.current);
    toastTimer.current = window.setTimeout(() => setToast(null), 3500);
  }, []);

  const newChatInProject = useCallback(
    (root: string | null) => {
      const proj = root ? projectMeta.get(root) : null;
      openSession(null);
      setChosenRoot(root);
      setChosenSecondary(proj?.secondary ?? []);
      setChosenPermission("ask");
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
          setWorkspaces((w) => ({ ...w, projects: r.projects }));
          refreshSessions();
          showToast("ok", action === "pin" ? "已置顶" : "已取消置顶");
        } else if (action === "reveal") {
          const r = await revealInFinder(root);
          if (!r.supported) showToast("err", "当前平台不支持在 Finder 中显示");
          else if (!r.ok) showToast("err", r.path ? `无法打开: ${r.path}` : "无法打开目录");
        } else if (action === "worktree") {
          if (!root) {
            showToast("err", "默认项目没有目录");
            return;
          }
          const r = await createWorktree(root);
          setWorkspaces((w) => ({ ...w, projects: r.projects }));
          refreshSessions();
          showToast("ok", `已创建永久工作树 ${r.name ?? ""}`);
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
    [projectMeta, refreshSessions, refreshArchived, sessions, showToast],
  );

  const confirmRemoveProject = useCallback(async () => {
    if (!removeTarget) return;
    try {
      const r = await removeProject(removeTarget.root);
      setWorkspaces((w) => ({ ...w, projects: r.projects }));
      refreshSessions();
      if (currentId === null && removeTarget.root === chosenRoot) setChosenRoot(null);
      showToast("ok", `已移除项目（删除 ${r.deleted_sessions} 条会话）`);
    } catch (e) {
      showToast("err", e instanceof Error ? e.message : String(e));
    } finally {
      setRemoveTarget(null);
    }
  }, [removeTarget, refreshSessions, chosenRoot, showToast]);

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

  const currentRoot = sessions.find((s) => s.id === currentId)?.root ?? null;
  const projectName = currentRoot ? basename(currentRoot) : DEFAULT_PROJECT;
  const exploration = useMemo(() => {
    let reads = 0;
    let searches = 0;
    for (const item of items) {
      if (item.kind !== "tool") continue;
      if (/read|list_dir|glob|find/i.test(item.name)) reads += 1;
      if (/search|grep/i.test(item.name)) searches += 1;
    }
    return { reads, searches };
  }, [items]);
  const lastItem = items[items.length - 1];
  const activeTool = lastItem?.kind === "tool" && !lastItem.done;
  const pendingApprovals = useMemo(
    () => items.filter((it): it is Extract<Item, { kind: "approval" }> => it.kind === "approval" && it.state === "pending"),
    [items],
  );
  const pendingApproval = pendingApprovals.length > 0;
  const currentSession = sessions.find((s) => s.id === currentId);
  const currentModelName =
    (currentId ? (models.models[currentSession?.model_alias ?? ""]?.model ?? currentSession?.model_alias) : null) ??
    models.models[models.default]?.model ??
    models.default;

  const changePermission = useCallback(
    async (mode: string) => {
      if (currentId === null) {
        setChosenPermission(mode);
        return;
      }
      setCurrentPermission(mode);
      try {
        const r = await setSessionPermission(currentId, mode);
        setCurrentPermission(r.permission_mode);
      } catch {
        refreshSessions();
      }
    },
    [currentId, refreshSessions],
  );

  const rollback = useCallback(
    async (dir: "undo" | "redo", untilUser?: number) => {
      if (currentId === null) return;
      const userItems = items.filter((item): item is Extract<Item, { kind: "user" }> => item.kind === "user");
      const rollbackCount = untilUser ? Math.max(1, userItems.length - untilUser + 1) : 1;
      const rollbackPrompt = untilUser
        ? userItems[Math.max(0, untilUser - 1)]?.text ?? "上一回合"
        : userItems[userItems.length - 1]?.text ?? "上一回合";
      setBusy(true);
      try {
        const r =
          dir === "undo" ? await undoSession(currentId, untilUser) : await redoSession(currentId);
        if (r.ok) {
          const restored = r.restored ?? [];
          if (dir === "undo") {
            setRollbackInfo({
              count: rollbackCount,
              prompt: rollbackPrompt,
              files: restored.length,
              messageOnly: Boolean(r.message_only),
            });
          } else {
            setRollbackInfo(null);
          }
          const detail = await fetchSession(currentId).catch(() => null);
          if (detail) setItems(historyToItems(detail.messages));
        }
      } catch {
        // nothing to undo/redo
      } finally {
        setBusy(false);
        refreshSessions();
      }
    },
    [currentId, items, refreshSessions],
  );

  const stop = useCallback(() => {
    if (currentId) cancelSessionChat(currentId).catch(() => {});
  }, [currentId]);

  const copyMessage = useCallback((text: string, index: number) => {
    const write = navigator.clipboard?.writeText(text);
    if (!write) return;
    void write.then(() => {
      setCopiedMessage(index);
      window.setTimeout(() => setCopiedMessage((current) => (current === index ? null : current)), 2000);
    });
  }, []);

  const ranTool = useRef(false);
  const send = useCallback(async () => {
    const text = input.trim();
    if (!text || busy) return;
    setInput("");
    setRollbackInfo(null);
    setBusy(true);
    ranTool.current = false;
    const sessionId = currentId;
    const opts: ChatOptions = {};
    if (currentId === null) {
      if (chosenRoot) opts.root = chosenRoot;
      if (chosenSecondary.length) opts.secondary_roots = chosenSecondary;
    }
    opts.permission_mode = currentId === null ? chosenPermission : currentPermission;
    const patches: Item[] = [
      { kind: "user", text },
      { kind: "assistant", text: "" },
    ];
    setItems((prev) => [...prev, ...patches]);
    try {
      await streamChat(sessionId, text, (ev) => {
        const expirePending = () =>
          setItems((prev) =>
            prev.map((it) =>
              it.kind === "approval" && it.state === "pending" ? { ...it, state: "expired" } : it,
            ),
          );
        if (ev.type === "session") {
          if (ev.session_id) setCurrentId(ev.session_id);
          refreshSessions();
        } else if (ev.type === "text") {
          setItems((prev) => {
            const next = [...prev];
            const last = next[next.length - 1];
            if (last?.kind === "assistant") {
              last.text += ev.content ?? "";
            } else {
              // Text after a tool call starts a new assistant block (the old
              // handler dropped it, hiding the model's final reply).
              next.push({ kind: "assistant", text: ev.content ?? "" });
            }
            return next;
          });
        } else if (ev.type === "cancelled") {
          expirePending();
          setItems((prev) => [...prev, { kind: "notice", text: "⏹ 已中断" }]);
        } else if (ev.type === "tool_start") {
          ranTool.current = true;
          setItems((prev) => [
            ...prev,
            {
              kind: "tool",
              id: ev.tool_call.id,
              name: ev.tool_call.name,
              args: ev.tool_call.arguments,
              done: false,
            },
          ]);
        } else if (ev.type === "tool_result") {
          setItems((prev) => {
            const next = [...prev];
            const tool = [...next].reverse().find(
              (item) => item.kind === "tool" && item.id === ev.tool_call?.id,
            );
            if (tool?.kind === "tool") {
              tool.result = ev.result;
              tool.done = true;
            }
            return next;
          });
        } else if (ev.type === "done") {
          expirePending();
          // 工具执行后若无最终回复，提示完成状态，避免看起来卡住
          setItems((prev) => {
            const last = prev[prev.length - 1];
            const noReply =
              ranTool.current &&
              (last?.kind !== "assistant" || !(last.text ?? "").trim());
            if (!noReply) return prev;
            const msg =
              last?.kind === "tool" && !last.done
                ? "⚠ 回合已结束但工具未返回结果"
                : "✓ 已完成（模型未输出文字回复，工具可能已生效）";
            return [
              ...prev,
              {
                kind: noReply && last?.kind === "tool" && !last.done ? "error" : "notice",
                text: msg,
              },
            ];
          });
        } else if (ev.type === "error") {
          expirePending();
          setItems((prev) => [...prev, { kind: "error", text: ev.error ?? "error" }]);
        } else if (ev.type === "approval_required") {
          setOverlayOpen(true);
          setItems((prev) => [
            ...prev,
            {
              kind: "approval",
              id: ev.approval_id,
              toolCallId: ev.tool_call.id,
              name: ev.tool_call.name,
              args: ev.tool_call.arguments,
              reason: ev.reason,
              scope: ev.scope,
              state: "pending" as ApprovalState,
            },
          ]);
        } else if (ev.type === "review") {
          setItems((prev) => [...prev, { kind: "review", text: ev.content ?? "" }]);
        }
      }, opts);
    } catch (err) {
      setItems((prev) => [
        ...prev,
        { kind: "error", text: err instanceof Error ? err.message : String(err) },
      ]);
    } finally {
      setBusy(false);
      setItems((prev) =>
        prev.map((it) => (it.kind === "approval" && it.state === "pending" ? { ...it, state: "expired" } : it)),
      );
      refreshSessions();
      if (currentId === null) {
        // pick up the newly-created session
        const list = await fetchSessions().catch(() => []);
        if (list.length) {
          setCurrentId(list[0].id);
          setCurrentPermission(list[0].permission_mode ?? chosenPermission);
        }
      }
    }
  }, [input, busy, currentId, chosenRoot, chosenSecondary, chosenPermission, refreshSessions]);

  return (
    <div className="app">
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-mark" aria-hidden="true">&gt;_</span>
          <h1>Easy code</h1>
        </div>
        <button className="new-btn" type="button" onClick={newSession}>
          <span aria-hidden="true">＋</span> 新会话
        </button>
        <div className="project-picker">
          {currentId === null ? (
            <ProjectPicker
              workspaces={workspaces}
              root={chosenRoot}
              secondary={chosenSecondary}
              disabled={busy}
              onRoot={setChosenRoot}
              onSecondary={setChosenSecondary}
              onWorkspaces={setWorkspaces}
            />
          ) : currentRoot ? (
            <div className="project-tag" title={currentRoot}>
              {basename(currentRoot)}
            </div>
          ) : (
            <div className="project-tag default">{DEFAULT_PROJECT}</div>
          )}
          {currentId !== null && (
            <SecondaryEditor
              root={currentRoot}
              secondary={currentSecondary}
              sessionId={currentId}
              disabled={busy}
              onSecondary={setCurrentSecondary}
              onWorkspaces={setWorkspaces}
            />
          )}
        </div>
        <div className="session-list">
          {groups.length > 0 && <div className="session-list-label">项目</div>}
          {groups.map(([root, list]) => {
            const meta = projectMeta.get(root);
            const pinned = meta?.pinned ?? false;
            return (
            <div key={root ?? "__default__"} className="project-group">
              <div
                className="project-group-head"
                title={root ?? DEFAULT_PROJECT}
              >
                <button
                  type="button"
                  className="group-head-main"
                  title={root ?? DEFAULT_PROJECT}
                  onClick={() => newChatInProject(root)}
                >
                  <FolderIcon />
                  <span className="project-group-name">{meta?.name ?? (root ? basename(root) : DEFAULT_PROJECT)}</span>
                </button>
                <div className="group-head-actions">
                  <button
                    type="button"
                    className="group-new-btn"
                    title="在此项目下新建会话"
                    aria-label="在此项目下新建会话"
                    disabled={busy}
                    onClick={(e) => {
                      e.stopPropagation();
                      newChatInProject(root);
                    }}
                  >
                    <span aria-hidden="true">＋</span>
                  </button>
                  <ProjectMenu
                    pinned={pinned}
                    disabled={busy}
                    onAction={(action) => runProjectAction(root, action)}
                  />
                </div>
              </div>
              {list.map((s) => (
                <div
                  key={s.id}
                  className={`session-item ${s.id === currentId ? "active" : ""}`}
                  onClick={() => openSession(s.id)}
                >
                  <span className="session-title">{s.title}</span>
                  <span
                    className="session-del"
                    title="删除"
                    onClick={async (e) => {
                      e.stopPropagation();
                      await deleteSession(s.id).catch(() => {});
                      if (currentId === s.id) {
                        setCurrentId(null);
                        setItems([]);
                      }
                      refreshSessions();
                    }}
                  >
                    ✕
                  </span>
                </div>
              ))}
            </div>
          );
          })}
          {groups.length === 0 && <div className="session-empty">新会话会保存在这里</div>}
          {archived.length > 0 && (
            <div className="archive-section">
              <button
                type="button"
                className="archive-head"
                onClick={() => setShowArchived(!showArchived)}
                aria-expanded={showArchived}
              >
                <span className="archive-icon" aria-hidden="true">📦</span>
                <span>已归档</span>
                <span className="archive-count">{archived.length}</span>
                <span className="archive-caret" aria-hidden="true">{showArchived ? "⌄" : "›"}</span>
              </button>
              {showArchived && (
                <div className="archive-list">
                  {archived.map((s) => (
                    <div key={s.id} className="session-item archived">
                      <span className="session-title" title={s.title}>{s.title}</span>
                      <div className="archive-actions">
                        <button
                          type="button"
                          className="archive-restore"
                          title="恢复"
                          aria-label="恢复"
                          onClick={async (e) => {
                            e.stopPropagation();
                            await setSessionArchived(s.id, false).catch(() => {});
                            setArchived((prev) => prev.filter((x) => x.id !== s.id));
                            refreshSessions();
                          }}
                        >
                          ↥
                        </button>
                        <button
                          type="button"
                          className="archive-del"
                          title="彻底删除"
                          aria-label="彻底删除"
                          onClick={async (e) => {
                            e.stopPropagation();
                            await deleteSession(s.id).catch(() => {});
                            setArchived((prev) => prev.filter((x) => x.id !== s.id));
                          }}
                        >
                          ✕
                        </button>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}
        </div>
        <div className="sidebar-foot">
          <ModelPicker
            models={models}
            current={models.default}
            onChange={setModels}
          />
        </div>
      </aside>
      <main className="chat">
        <header className="chat-header">
          <div className="chat-context">
            <strong>{currentId ? sessions.find((s) => s.id === currentId)?.title : "新会话"}</strong>
            <span className="context-path" title={currentRoot ?? DEFAULT_PROJECT}>
              <span aria-hidden="true">⌘</span> {projectName}
            </span>
          </div>
          <div className="header-actions">
            <button
              className="header-action"
              title="撤销上一回合"
              disabled={busy || currentId === null || items.every((item) => item.kind !== "user")}
              onClick={() => rollback("undo")}
            >
              ↶ <span>撤销</span>
            </button>
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
        <div className="chat-main">
          {items.length === 0 && (
            <div className="empty">
              <div className="empty-icon" aria-hidden="true">&gt;_</div>
              <h2>从一个任务开始</h2>
              <p>描述你想完成的工作，Easy code 会在当前工作区中协助你。</p>
            </div>
          )}
          {items.map((it, i) => {
            if (it.kind === "user") {
              // 该消息在会话历史中的序号（第 n 条用户消息，1-based）
              const nth = items.filter((x, j) => j <= i && x.kind === "user").length;
              return (
                <div key={i} className="msg-row user">
                  <div className="msg user">{it.text}</div>
                  <div className="msg-meta">
                    <span>Build</span>
                    <span className="msg-meta-separator">·</span>
                    <span>{currentModelName}</span>
                    <span className="msg-meta-separator">·</span>
                    <time>{it.time ? formatClock(it.time) : currentTimeLabel()}</time>
                    <button
                      className="msg-action"
                      title="回滚到这条 prompt 之前"
                      aria-label="回滚到这条消息之前"
                      disabled={busy || currentId === null}
                      onClick={() => rollback("undo", nth)}
                    >
                      <UndoIcon />
                    </button>
                    <button
                      className="msg-action"
                      title="复制消息"
                      aria-label="复制消息"
                      onClick={() => copyMessage(it.text, i)}
                    >
                      <CopyIcon copied={copiedMessage === i} />
                    </button>
                  </div>
                </div>
              );
            }
            if (it.kind === "assistant") {
              if (!it.text) return null;
              return (
                <div key={i} className="msg assistant">
                  {it.text}
                  {busy && i === items.length - 1 && <span className="cursor" />}
                </div>
              );
            }
            if (it.kind === "tool") {
              return (
              <ToolCard key={i} tool={{ id: it.id || String(i), name: it.name, arguments: it.args }} result={it.result} done={it.done} />
              );
            }
            if (it.kind === "approval") {
              const label =
                it.state === "approved"
                  ? "已允许"
                  : it.state === "denied"
                    ? "已拒绝"
                    : it.state === "expired"
                      ? "已过期"
                      : "等待批准";
              return (
                <div key={i} className={`approval-line ${it.state}`}>
                  <span className="approval-line-dot" aria-hidden="true">!</span>
                  <span className="approval-line-text">
                    {label} · {it.name}
                    {it.scope ? <code>{it.scope}</code> : null}
                  </span>
                </div>
              );
            }
            if (it.kind === "review") {
              return (
                <details key={i} className="review-card">
                  <summary>自动审查与变更记录</summary>
                  <pre>{it.text}</pre>
                </details>
              );
            }
            if (it.kind === "notice") {
              return (
                <div key={i} className="msg notice">
                  {it.text}
                </div>
              );
            }
            return (
              <div key={i} className="msg error">
                {it.text}
              </div>
            );
          })}
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
          <div ref={endRef} />
        </div>
        <div className="composer-area">
          {rollbackInfo && (
            <div className="rollback-banner" role="status">
              <span className="rollback-icon" aria-hidden="true">↶</span>
              <div className="rollback-copy">
                <strong>{rollbackInfo.count} 条已回滚消息</strong>
                <span title={rollbackInfo.prompt}>{rollbackInfo.prompt}</span>
                <small>
                  {rollbackInfo.messageOnly
                    ? "非 Git 工作区，仅恢复了对话"
                    : rollbackInfo.files > 0
                      ? `同时恢复了 ${rollbackInfo.files} 个文件`
                      : "对话与工作区已回到此处"}
                </small>
              </div>
              <button
                type="button"
                className="rollback-restore"
                disabled={busy}
                onClick={() => rollback("redo")}
              >
                恢复
              </button>
            </div>
          )}
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
                  if (e.key === "Escape") {
                    setCmdOpen(false);
                    return;
                  }
                  if (cmdOpen) {
                    const q = input.startsWith("/") ? input.slice(1) : input;
                    const count = commands.filter(
                      (c) => c.name.startsWith(q) || (q.length > 0 && c.name.includes(q)),
                    ).length;
                    if (e.key === "ArrowDown") {
                      e.preventDefault();
                      setCmdIndex((i) => (count ? Math.min(i + 1, count - 1) : i));
                      return;
                    }
                    if (e.key === "ArrowUp") {
                      e.preventDefault();
                      setCmdIndex((i) => Math.max(i - 1, 0));
                      return;
                    }
                    if (e.key === "Enter" && count > 0) {
                      e.preventDefault();
                      const filtered = commands
                        .filter(
                          (c) => c.name.startsWith(q) || (q.length > 0 && c.name.includes(q)),
                        )
                        .slice(0, 12);
                      const pick = filtered[Math.min(cmdIndex, filtered.length - 1)];
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
                    value={currentId === null ? chosenPermission : currentPermission}
                    disabled={busy}
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
                disabled={!busy && !input.trim()}
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
          position={1}
          total={pendingApprovals.length}
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
          <div className="modal-backdrop" onClick={() => setEditTarget(null)}>
            <div className="modal project-edit-modal" onClick={(e) => e.stopPropagation()}>
              <div className="modal-title">编辑项目</div>
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
                onSecondary={() => {}}
                onWorkspaces={setWorkspaces}
              />
              <div className="modal-actions">
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
                      setWorkspaces((w) => ({ ...w, projects: r.projects }));
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
              </div>
            </div>
          </div>
        )}
        {removeTarget && (
          <div className="modal-backdrop" onClick={() => setRemoveTarget(null)}>
            <div className="modal project-remove-modal" onClick={(e) => e.stopPropagation()}>
              <div className="modal-title">移除项目</div>
              <p className="modal-desc">
                将删除「{removeTarget.root ? basename(removeTarget.root) : DEFAULT_PROJECT}」的绑定，
                并删除其下 {removeTarget.count} 条会话（不可恢复）。
              </p>
              <div className="modal-actions">
                <button type="button" className="modal-cancel" onClick={() => setRemoveTarget(null)}>
                  取消
                </button>
                <button type="button" className="danger" onClick={confirmRemoveProject}>
                  确认移除
                </button>
              </div>
            </div>
          </div>
        )}
      </main>
    </div>
  );
}
