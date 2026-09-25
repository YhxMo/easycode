import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { CSSProperties } from "react";
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
import { MENTION_LIST_ID, MENTION_OPTION_PREFIX, MentionMenu } from "./MentionMenu";
import type { MentionAnswer, MentionState } from "./MentionMenu";
import { ModelPicker } from "./ModelPicker";
import { currentTurn } from "./chatStream";
import { historyToItems } from "./lib/history";
import type { ApprovalState, Item } from "./types";
import { COMMAND_LIST_ID, CommandMenu } from "./CommandMenu";
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
import { useDrafts } from "./lib/useDrafts";
import { useStickToBottom } from "./lib/useStickToBottom";
import type { StickToBottom } from "./lib/useStickToBottom";
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

/**
 * One conversation's right-pane state. `turn` is the turn the open/section
 * choices were made in, so they expire with that turn and the next one can
 * open the pane again.
 */
interface PaneState {
  turn: number;
  open: boolean | null;
  section: string | null;
  preview: string | null;
}

/** Restore the stored tab list: strings only, de-duplicated, never fatal. */
function readStoredTabs(): string[] {
  try {
    const saved = window.localStorage.getItem("easycode:open_tabs");
    if (!saved) return [];
    const parsed: unknown = JSON.parse(saved);
    if (!Array.isArray(parsed)) return [];
    return [...new Set(parsed.filter((v): v is string => typeof v === "string" && v !== ""))];
  } catch {
    // localStorage 不可用（隐私模式等）——仅内存态，忽略即可
    return [];
  }
}

export default function App() {
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
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
  const [commandResult, setCommandResult] = useState<{ scope: string; commands: CommandInfo[] }>({
    scope: "",
    commands: [],
  });
  const [cmdOpen, setCmdOpen] = useState(false);
  const [cmdIndex, setCmdIndex] = useState(0);
  // `@` file references: the token under the caret and whether the user
  // dismissed the menu for this token.
  const [mentionOff, setMentionOff] = useState(false);
  const [mentionIndex, setMentionIndex] = useState(0);
  // The last answer for a file query, tagged with what it answered. A token
  // without a matching answer is still being looked up, so "searching" never
  // has to be written into state and a completed-but-empty result stays
  // distinguishable from a failure.
  const [mentionAnswer, setMentionAnswer] = useState<{
    scope: string;
    query: string;
    answer: MentionAnswer;
  } | null>(null);
  // Bumped by the menu's retry: the effect below is what owns the request.
  const [mentionRetry, setMentionRetry] = useState(0);
  // on mobile (<=760px) the sidebar is hidden; `sidebarOpen` drives the
  // drawer overlay so core session/project/model navigation stays reachable.
  const [sidebarOpen, setSidebarOpen] = useState(false);
  // Open session tabs: `openTabs` is the single list of opened conversations,
  // in the order they were opened. Closing one closes only the view: the
  // conversation stays in the sidebar and a running turn keeps streaming into
  // its own slot.
  const [openTabs, setOpenTabs] = useState<string[]>(() => readStoredTabs());
  // The pane follows the turn: it opens itself once a turn has produced context
  // or changes, and a choice the user makes (toggle or ✕) holds for that turn
  // only, so the next turn can open it again. Choices are per conversation, so
  // one session's open/closed pane never governs another's.
  const [paneState, setPaneState] = useState<Record<string, PaneState>>({});
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
  // Draft key whose dictation is running, so a late transcript lands in the
  // conversation it was spoken into.
  const voiceKeyRef = useRef(DRAFT_KEY);

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
  const currentSession = sessions.find((s) => s.id === currentId);
  const currentModelName =
    (currentId ? (models.models[currentSession?.model_alias ?? ""]?.model ?? currentSession?.model_alias) : null) ??
    models.models[models.default]?.model ??
    models.default;
  const sessionBlocked = sessionLoad !== null;
  const sendBlocked = sessionBlocked || permissionPending;

  // The composer edits exactly one conversation's draft: the one on screen.
  const draftKey = currentId ?? DRAFT_KEY;
  const draft = drafts.get(draftKey);
  const input = draft.text;
  const caret = draft.caret;
  const setInput = useCallback(
    (v: string) => drafts.update(draftKey, (d) => ({ ...d, text: v })),
    [drafts, draftKey],
  );
  const setCaret = useCallback(
    (c: number) => drafts.update(draftKey, (d) => ({ ...d, caret: c })),
    [drafts, draftKey],
  );

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
    clearDraft: drafts.clear,
    moveDraft: drafts.move,
    setCurrentId,
    onApprovalRequired: revealApproval,
    onSessionNamed: registerTab,
  });

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

  // Command discovery follows the exact foreground scope: an open session's
  // own roots/skills, or the draft's root + secondary list. A result carries
  // the scope it was fetched for, so a stale menu disappears the moment the
  // scope changes and a late reply can never replace the current one.
  const commandScope = JSON.stringify([currentId, chosenRoot, secondary]);
  const commands = useMemo(
    () => (commandResult.scope === commandScope ? commandResult.commands : []),
    [commandResult, commandScope],
  );
  const commandMatches = useMemo(
    () => (cmdOpen ? filterCommands(commands, input) : []),
    [cmdOpen, commands, input],
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
    const scope = mentionScope;
    const query = mentionQuery;
    let live = true;
    // Debounced so typing does not fire a request per keystroke.
    const timer = window.setTimeout(() => {
      fetchFiles(currentId, draft, query)
        .then((r) => {
          if (live) {
            setMentionAnswer({ scope, query, answer: { status: "ready", files: r.files, total: r.total } });
          }
        })
        .catch((e: unknown) => {
          if (live) {
            setMentionAnswer({
              scope,
              query,
              answer: { status: "error", message: e instanceof Error ? e.message : String(e) },
            });
          }
        });
    }, 140);
    return () => {
      live = false;
      window.clearTimeout(timer);
    };
  }, [mentionQuery, mentionScope, currentId, chosenRoot, secondary, mentionRetry]);

  const mentionOpen = Boolean(mentionInfo) && !mentionOff && !cmdOpen;
  // A reply only counts while it still answers the token under the caret; a
  // token without one is being looked up.
  const mentionCurrent: MentionState | null = useMemo(() => {
    if (!mentionInfo) return null;
    const answered = mentionAnswer;
    if (answered && answered.scope === mentionScope && answered.query === mentionInfo.query) {
      return { ...answered.answer, scope: answered.scope, query: answered.query };
    }
    return { status: "loading", scope: mentionScope, query: mentionInfo.query };
  }, [mentionInfo, mentionAnswer, mentionScope]);
  const mentionMatches = useMemo(
    () => (mentionCurrent?.status === "ready" ? mentionCurrent.files : []),
    [mentionCurrent],
  );

  const pickMention = useCallback(
    (file: FileEntry) => {
      const token = mentionInfo;
      if (!token) return;
      const next = applyMention(input, token, file.absolute_path);
      setInput(next.value);
      setCaret(next.caret);
      setMentionOff(true);
      window.requestAnimationFrame(() => {
        fieldRef.current?.setSelectionRange(next.caret, next.caret);
      });
    },
    [input, mentionInfo, setCaret, setInput],
  );

  /** Dictated text appends to the draft the dictation was started in. */
  const appendToDraft = useCallback(
    (key: string, text: string) =>
      drafts.update(key, (d) => ({ ...d, text: d.text ? `${d.text} ${text}` : text })),
    [drafts],
  );
  const voice = useVoiceInput((text) => appendToDraft(voiceKeyRef.current, text));
  const toggleVoice = useCallback(() => {
    // Remember where this dictation belongs: a transcript that arrives after
    // the user switched away must not append to the new conversation.
    if (!voice.listening) voiceKeyRef.current = draftKey;
    voice.toggle();
  }, [voice, draftKey]);

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
      // Menus belong to the composer's text, which is about to change: a
      // dismissed state keeps them closed until the user types again.
      setCmdOpen(false);
      setMentionOff(true);
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
    [showToast, currentId, busy, sessionLoad, loadHistory, dropEntry, isStreaming, registerTab],
  );

  // Restore the caret a conversation was left at. The field keeps the focus it
  // has: this only puts the insertion point where the user stopped typing.
  useEffect(() => {
    const field = fieldRef.current;
    if (!field) return;
    const pos = Math.min(drafts.get(draftKey).caret, field.value.length);
    if (field.selectionStart !== pos || field.selectionEnd !== pos) {
      field.setSelectionRange(pos, pos);
    }
  }, [draftKey, drafts]);

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
    // Only a successful delete drops the tab (and the draft it holds).
    setOpenTabs((prev) => prev.filter((t) => t !== target.id));
    drafts.clear(target.id);
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
    // Sessions of this project, captured now: a failed removal leaves every
    // tab where it was.
    const removed = sessions.filter((s) => (s.root ?? null) === removedRoot).map((s) => s.id);
    try {
      const r = await removeProject(removedRoot);
      setProjects(r.projects);
      setOpenTabs((prev) => prev.filter((id) => !removed.includes(id)));
      for (const id of removed) drafts.clear(id);
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
    sessions,
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
      // running turn keeps its slot (and its draft) so its output is not lost
      // and the tab can be reopened without cancelling anything.
      dropEntry(id);
      if (id !== currentId) return;
      const neighbour = remaining[index] ?? remaining[index - 1] ?? null;
      openSession(neighbour);
    },
    [openTabs, currentId, dropEntry, openSession],
  );

  const currentRoot = currentSession?.root ?? null;
  // What the empty stage can promise: the directory this draft would run in.
  const draftContext = chosenRoot
    ? basename(chosenRoot)
    : workspaces.default
      ? basename(workspaces.default)
      : DEFAULT_PROJECT;
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
  // Any turn on this page: a model switch rebinds every session, so a
  // background turn blocks it just as the foreground one does.
  const anyBusy = useMemo(() => Object.values(activity).some((a) => a.busy), [activity]);
  const turn = useMemo(() => currentTurn(items), [items]);
  // Turns are counted by user messages: a choice made in one turn must not
  // carry over to the next.
  const turnNo = useMemo(() => items.filter((it) => it.kind === "user").length, [items]);
  const pane = useMemo(() => paneData(turn), [turn]);
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
  const turnTodos = useMemo(() => turn.some((it) => it.kind === "todo"), [turn]);
  // Only turns produced in this view drive the pane: a restored session's
  // history must not pop it open on load.
  const liveTurn =
    busy || turn.some((it) => it.kind === "assistant" && typeof it.durationMs === "number");
  const hasArtifacts = pane.context.length > 0 || pane.changes.length > 0 || turnTodos;
  const paneSelf = paneState[draftKey];
  const paneOpen =
    paneSelf?.turn === turnNo && paneSelf.open !== null ? paneSelf.open : liveTurn && hasArtifacts;
  const paneSection =
    paneSelf?.turn === turnNo && paneSelf.section
      ? paneSelf.section
      : pane.changes.length
        ? "changes"
        : todos.length
          ? "tasks"
          : "context";
  const preview = paneSelf?.preview ?? null;
  const patchPaneState = useCallback(
    (key: string, patch: Partial<PaneState>) =>
      setPaneState((prev) => {
        const self = prev[key] ?? { turn: 0, open: null, section: null, preview: null };
        return { ...prev, [key]: { ...self, ...patch } };
      }),
    [],
  );
  const setPane = useCallback(
    (open: boolean) => patchPaneState(draftKey, { turn: turnNo, open }),
    [patchPaneState, draftKey, turnNo],
  );
  const chooseSection = useCallback(
    (section: string) => patchPaneState(draftKey, { turn: turnNo, section }),
    [patchPaneState, draftKey, turnNo],
  );
  const setPreview = useCallback(
    (path: string | null) => patchPaneState(draftKey, { preview: path }),
    [patchPaneState, draftKey],
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
      // While the file menu is up, Enter belongs to it: picking a reference is
      // the only way forward, so a query still loading, a failure or a blank
      // result can never turn into an accidental send. Escape closes the menu
      // and gives Enter back to the composer.
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
        if (e.key === "Enter") {
          e.preventDefault();
          const file = mentionMatches[Math.min(mentionIndex, mentionMatches.length - 1)];
          if (file) pickMention(file);
          return;
        }
        if (e.key === "Tab" && mentionMatches.length > 0) {
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
          // Same rule as the file menu while it is actually showing something.
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
    [
      cmdOpen,
      commands,
      input,
      cmdIndex,
      send,
      setInput,
      mentionOpen,
      mentionMatches,
      mentionIndex,
      pickMention,
    ],
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
      menuOpen={mentionOpen || commandMatches.length > 0}
      menuId={mentionOpen ? MENTION_LIST_ID : COMMAND_LIST_ID}
      activeOptionId={
        mentionOpen
          ? mentionMatches.length
            ? `${MENTION_OPTION_PREFIX}${Math.min(mentionIndex, mentionMatches.length - 1)}`
            : undefined
          : undefined
      }
      onSend={() => {
        // Sending is an explicit return to the live edge: the reply to your own
        // message is never something you have to scroll back down for.
        sticky.stick();
        send();
      }}
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
      voice={{ supported: voice.supported, listening: voice.listening, toggle: toggleVoice }}
      menu={
        mentionOpen ? (
          <MentionMenu
            state={mentionCurrent}
            query={mentionInfo?.query ?? ""}
            index={mentionIndex}
            onPick={pickMention}
            onClose={() => setMentionOff(true)}
            onRetry={() => setMentionRetry((n) => n + 1)}
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
        workspacesLoaded={workspacesLoaded}
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
                {busy ? (pendingApproval ? "等待批准" : "正在工作") : "就绪"}
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
                  setInput(text);
                  fieldRef.current?.focus();
                }}
              >
                {composer("tall")}
              </EmptyState>
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
          {!isEmptyStage && (
            <div className="composer-area" ref={attachComposer}>
              {sticky.unread && (
                <button type="button" className="scroll-latest" onClick={sticky.stick}>
                  <span aria-hidden="true">↓</span> 有新内容 · 回到最新
                </button>
              )}
              {composer("docked")}
            </div>
          )}
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
              onOpen={(target) => {
                setPreview(target);
                chooseSection("file");
              }}
            />
          )}
          {paneSection === "changes" &&
            (pane.changes.length ? (
              pane.changes.map((change) => (
                <DiffView
                  key={change.id}
                  path={change.path}
                  diff={change.diff}
                  applied={change.applied}
                />
              ))
            ) : (
              <p className="pane-empty">这一轮还没有修改文件。</p>
            ))}
          {paneSection === "file" &&
            (preview && currentId ? (
              // The key carries the conversation as well as the path: the same
              // relative name in another session must never show the old text.
              <FilePreview
                key={`${currentId}:${preview}`}
                sessionId={currentId}
                path={preview}
                onBack={() => {
                  setPreview(null);
                  chooseSection("context");
                }}
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
