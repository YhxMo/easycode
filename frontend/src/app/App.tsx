import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { CSSProperties } from "react";
import type {
  ModelsInfo,
  SessionSummary,
  WorkspaceProject,
} from "../api";
import {
  archiveProjectChats,
  createWorktree,
  fetchModels,
  fetchSession,
  pinSession,
  pinProject,
  revealInFinder,
  saveProject,
  setSessionArchived,
  submitApproval,
} from "../api";
import { DRAFT_KEY, useChatStream } from "../features/chat/useChatStream";
import { useComposer } from "../features/composer/useComposer";
import { MENTION_LIST_ID, MENTION_OPTION_PREFIX, MentionMenu } from "../features/composer/MentionMenu";
import { ExtensionsSettings, type ExtensionTarget } from "../features/extensions/ExtensionsSettings";
import { ModelPicker } from "../features/models/ModelPicker";
import { currentTurn } from "../features/chat/chatStream";
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
} from "./constants";
import { useSessionList } from "./useSessionList";
import { useTabs } from "./useTabs";
import { ConfirmDialogs } from "./ConfirmDialogs";
import { useSessionLifecycle } from "./useSessionLifecycle";
import { usePermission } from "./usePermission";
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
  const [currentId, setCurrentId] = useState<string | null>(null);
  // Unsent composer text, one record per conversation: switching sessions must
  // never carry what was typed here into another conversation's field.
  const drafts = useDrafts();
  // ---- toast (shared by openSession failure + project actions) ----
  const [toast, showToast] = useToast();
  const [models, setModels] = useState<ModelsInfo>(EMPTY_MODELS);
  // The sidebar's two lists and the tabs opened from them. The list hands its
  // ids to the tabs, which is what keeps a tab from outliving its conversation.
  const tabList = useTabs();
  const {
    sessions,
    setSessions,
    archived,
    setArchived,
    workspaces,
    workspacesLoaded,
    setProjects,
    sessionById,
    refresh: refreshSessions,
    refreshArchived,
  } = useSessionList({ onListLoaded: tabList.keepOnly, onArchivedLoaded: tabList.drop });
  // Bound by name: several callbacks below depend on these, and through the
  // hook's own object the rule asks for the whole per-render value.
  const { register: registerTab, open: openTabs } = tabList;

  const [chosenRoot, setChosenRoot] = useState<string | null>(null);
  // One pair of states serves both the new-session draft and the open session:
  // a session event updates them in place, and openSession resets them.
  const [secondary, setSecondary] = useState<string[]>([]);
  // Foreground session load state: while loading (or after a failed load) the
  // composer, permission picker and secondary editor are disabled, so no
  // request can be sent against a target whose state is not confirmed yet.
  const [sessionLoad, setSessionLoad] = useState<SessionLoad | null>(null);
  /** Bumped whenever the extensions dialog installs or saves something: the `/`
   *  menu reads its sources again, so a new skill is selectable without a reload. */
  const [extensionsRevision, setExtensionsRevision] = useState(0);

  // on mobile (<=760px) the sidebar is hidden; `sidebarOpen` drives the
  // drawer overlay so core session/project/model navigation stays reachable.
  const [sidebarOpen, setSidebarOpen] = useState(false);
  // Open session tabs: the tab list is the single record of opened conversations,
  // in the order they were opened. Closing one closes only the view: the
  // conversation stays in the sidebar and a running turn keeps streaming into
  // its own slot.
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
  const permission = usePermission({
    sessionId: currentId,
    sessionBlocked: sessionLoad !== null,
    viewToken: openSeqRef,
    onError: showToast,
  });

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
  const sendBlocked = permission.sendBlocked;

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
    permission: permission.mode,
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
      window.localStorage.setItem(CURRENT_TAB_KEY, currentId ?? "");
    } catch {
      // localStorage 不可用（隐私模式等）——仅内存态，忽略即可
    }
  }, [currentId]);

  // ---- sidebar collapse state (sections + project groups, in localStorage) ----
  const [collapsedProjects, toggleProjectCollapsed] = usePersistedFlags("easycode:collapsed_projects");
  const [collapsedSections, toggleSection] = usePersistedFlags("easycode:collapsed_sections");



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
    [setProjects, setSessions],
  );

  /**
   * Create the conversation the user just asked for, before anything is sent:
   * the tab that appears is backed by a real session, so an empty one survives
   * a refresh and closing it can remove it for good.
   */


  // ---- project row actions (new chat / more menu) ----
  const [editTarget, setEditTarget] = useState<{ root: string | null; name: string } | null>(null);
  /** The project whose skills and servers the extensions dialog is editing. */
  const [extensionTarget, setExtensionTarget] = useState<ExtensionTarget | null>(null);
  const [showArchived, setShowArchived] = useState(false);


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
    fullAccess: permission.mode === "allow-all",
    narrow: chatNarrow,
    toggleRef: paneToggleRef,
  });

  const lifecycle = useSessionLifecycle({
    stream: { forgetEntry, dropEntry, loadHistory, isStreaming },
    pane,
    sticky,
    drafts,
    tabs: tabList,
    list: {
      sessions,
      archived,
      setArchived,
      sessionById,
      setSessions,
      setProjects,
      refresh: refreshSessions,
      refreshArchived,
    },
    permission,
    composer: { closeCmdMenu, dismissMention },
    toast: showToast,
    viewTokenRef: openSeqRef,
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
  });
  const {
    openSession,
    createAndOpen,
    newSession,
    selectTab,
    closeTab,
    confirmDeleteSession,
    confirmRemoveProject,
  } = lifecycle;
  const setRemoveTarget = lifecycle.setRemoveTarget;

  const newChatInProject = useCallback(
    (root: string | null) => {
      // This row's project is the requested one, whatever is on screen now.
      void createAndOpen(root);
    },
    [createAndOpen],
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

  // The centred first-run stage replaces the stream until a conversation has
  // something to show; a load in progress is never masked by it.
  const isEmptyStage = items.length === 0 && sessionLoad === null;

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
      permission={permission.mode}
      permissionDisabled={busy || sendBlocked}
      onPermission={permission.request}
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
        sessionBlocked={sessionLoad !== null}
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
        onDeleteSession={lifecycle.setDeleteTarget}
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
        <ConfirmDialogs
          fullAccess={permission.confirming}
          onDismissFullAccess={() => permission.setConfirming(false)}
          onConfirmFullAccess={() => {
            permission.setConfirming(false);
            void permission.change("allow-all", true);
          }}
        />
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
        {lifecycle.deleteTarget && (
          <Modal
            open={Boolean(lifecycle.deleteTarget)}
            onClose={() => lifecycle.setDeleteTarget(null)}
            title="删除会话"
            variant="project-remove-modal"
            actions={
              <>
                <button type="button" className="modal-cancel" onClick={() => lifecycle.setDeleteTarget(null)}>
                  取消
                </button>
                <button type="button" className="danger" onClick={confirmDeleteSession}>
                  确认删除
                </button>
              </>
            }
          >
            <p className="modal-desc">
              将永久删除「{lifecycle.deleteTarget.title}」及其全部消息（不可恢复）。
            </p>
          </Modal>
        )}
        {lifecycle.removeTarget && (
          <Modal
            open={Boolean(lifecycle.removeTarget)}
            onClose={() => lifecycle.setRemoveTarget(null)}
            title="移除项目"
            variant="project-remove-modal"
            actions={
              <>
                <button type="button" className="modal-cancel" onClick={() => lifecycle.setRemoveTarget(null)}>
                  取消
                </button>
                <button type="button" className="danger" onClick={confirmRemoveProject}>
                  确认移除
                </button>
              </>
            }
          >
            <p className="modal-desc">
              将删除「{lifecycle.removeTarget.root ? basename(lifecycle.removeTarget.root) : DEFAULT_PROJECT}」的绑定，
              并删除其下 {lifecycle.removeTarget.count} 条会话（不可恢复）。
            </p>
          </Modal>
        )}
      </main>
    </div>
  );
}
