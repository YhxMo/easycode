import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { CSSProperties } from "react";
import type {
  ModelsInfo,
  SessionSummary,
  WorkspaceProject,
} from "../api";
import { fetchModels } from "../api";
import { DRAFT_KEY, useChatStream } from "../features/chat/useChatStream";
import { useComposer } from "../features/composer/useComposer";
import { MENTION_LIST_ID, MENTION_OPTION_PREFIX, MentionMenu } from "../features/composer/MentionMenu";
import { ExtensionsSettings } from "../features/extensions/ExtensionsSettings";
import { ModelPicker } from "../features/models/ModelPicker";
import { COMMAND_LIST_ID, CommandMenu } from "../features/composer/CommandMenu";
import { activeCommandId } from "../features/composer/commands";
import { ChatMessages } from "../features/chat/ChatMessages";
import { Sidebar } from "../features/sidebar/Sidebar";
import { ComposerBar } from "../features/composer/ComposerBar";
import { RightPane } from "../features/pane/RightPane";
import { PaneBody } from "../features/pane/PaneBody";
import { usePane } from "../features/pane/usePane";
import { EmptyState } from "../features/chat/EmptyState";
import { LoadingState } from "../components/primitives/LoadingState";
import type { OpenTab } from "../features/sidebar/TabBar";
import { SelectionActions } from "../features/chat/SelectionActions";
import { groupSessions } from "../features/sidebar/sessionGroups";
import { usePersistedFlags } from "../lib/usePersistedFlags";
import { useDrafts, type Draft } from "../features/composer/useDrafts";
import { useStickToBottom } from "../lib/useStickToBottom";
import type { StickToBottom } from "../lib/useStickToBottom";
import { basename, DEFAULT_PROJECT } from "../lib/paths";
import { EMPTY_MODELS, NARROW_CHAT_PX, PANE_SECTIONS } from "./constants";
import { useSessionList } from "./useSessionList";
import { useTabs } from "./useTabs";
import { ChatHeader } from "./ChatHeader";
import { ConfirmDialogs } from "./ConfirmDialogs";
import { ProjectEditDialog } from "./ProjectEditDialog";
import { useSessionLifecycle } from "./useSessionLifecycle";
import { usePermission } from "./usePermission";
import { useSessionView } from "./useSessionView";
import { useSidebarDrawer } from "./useSidebarDrawer";
import { useToast } from "./useToast";
import { useMessageActions } from "./useMessageActions";
import { useProjectActions } from "./useProjectActions";
import { useTranscriptView } from "./useTranscriptView";

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
  // View version: a response is applied only while it still matches, and the
  // ref is readable at response time by child editors.
  const openSeqRef = useRef(0);
  const {
    currentId,
    setCurrentId,
    chosenRoot,
    chooseRoot,
    secondary,
    setSecondary,
    sessionLoad,
    setSessionLoad,
    artifactRecords,
    setArtifactRecords,
    extensionsRevision,
    setExtensionsRevision,
  } = useSessionView(openSeqRef);
  // Unsent composer text, one record per conversation: switching sessions must
  // never carry what was typed here into another conversation's field.
  const drafts = useDrafts();
  // ---- toast (shared by openSession failure + project actions) ----
  const [toast, showToast] = useToast();
  const [models, setModels] = useState<ModelsInfo>(EMPTY_MODELS);
  // The sidebar's two lists and the tabs opened from them. The list hands its
  // ids to the tabs, which is what keeps a tab from outliving its conversation.
  const tabList = useTabs(currentId);
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

  const { open: sidebarOpen, setOpen: setSidebarOpen, close: closeSidebar } = useSidebarDrawer();
  const mainRef = useRef<HTMLDivElement>(null);
  const fieldRef = useRef<HTMLTextAreaElement>(null);
  // Height of the floating composer: the message stream reserves exactly that
  // much room at its bottom, so a grown input never hides the last reply.
  const [composerHeight, setComposerHeight] = useState(0);
  const permission = usePermission({
    sessionId: currentId,
    sessionBlocked: sessionLoad !== null,
    viewToken: openSeqRef,
    onError: showToast,
  });

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

  // The drafts store changes identity on every edit, so the session lifecycle
  // reads it through this ref instead of depending on it.
  const draftsRef = useRef(drafts);
  useEffect(() => {
    draftsRef.current = drafts;
  }, [drafts]);

  useEffect(() => {
    refreshSessions();
    fetchModels().then(setModels).catch(() => {});
  }, [refreshSessions]);

  // Changes whenever the registered projects do, so the menu re-reads them.
  const projectSig = (workspaces.projects ?? []).map((p) => p.root ?? "").join("\u0000");

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
  const { closeCmdMenu, dismissMention } = composer;
  const insertDraftText = composer.setInput;

  const messageActions = useMessageActions({
    drafts,
    draftKey,
    currentId,
    busy,
    sendBlocked,
    fieldRef,
    sticky,
    insertDraftText,
    setItems,
    toast: showToast,
  });
  const { decideApproval, continueUnfinished, startEdit } = messageActions;

  useEffect(() => {
    refreshArchived();
  }, [refreshArchived]);

  // ---- sidebar collapse state (sections + project groups, in localStorage) ----
  const [collapsedProjects, toggleProjectCollapsed] = usePersistedFlags("easycode:collapsed_projects");
  const [collapsedSections, toggleSection] = usePersistedFlags("easycode:collapsed_sections");

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
    [setProjects, setSecondary, setSessions],
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
  const transcript = useTranscriptView(items, activity);
  const { exploration, activeTool, anyBusy, turn, turnNo, pendingApproval } = transcript;
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
    forgetEntry,
    dropEntry,
    loadHistory,
    isStreaming,
    forgetPane: pane.forget,
    forgetScroll: sticky.forget,
    draftsRef,
    openTabs,
    setOpenTabs: tabList.setOpen,
    registerTab,
    rememberedTab: tabList.remembered,
    sessions,
    archived,
    setArchived,
    sessionById,
    setProjects,
    refreshSessions,
    refreshArchived,
    permissionMode: permission.mode,
    resetPermission: permission.reset,
    clearPermissionPending: permission.clearPending,
    closeCmdMenu,
    dismissMention,
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
  const projects = useProjectActions({
    projectMeta,
    sessions,
    setProjects,
    setArchived,
    refreshSessions,
    refreshArchived,
    currentSession,
    currentId,
    createAndOpen,
    setRemoveTarget: lifecycle.setRemoveTarget,
    toast: showToast,
  });
  const {
    editTarget,
    extensionTarget,
    showArchived,
    newChatInProject,
    togglePin,
    restoreArchived,
    openExtensions,
    runProjectAction,
  } = projects;

  // The centred first-run stage replaces the stream until a conversation has
  // something to show; a load in progress is never masked by it.
  const isEmptyStage = items.length === 0 && sessionLoad === null;

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
        onToggleArchived={projects.toggleArchived}
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
          <ChatHeader
            tabs={tabs}
            currentId={currentId}
            activity={activity}
            onSelectTab={selectTab}
            onCloseTab={closeTab}
            onNewTab={newSession}
            sidebarOpen={sidebarOpen}
            onOpenSidebar={() => setSidebarOpen(true)}
            busy={busy}
            pendingApproval={pendingApproval}
            onRevealApproval={revealApproval}
            paneOpen={pane.open}
            onTogglePane={() => (pane.open ? pane.close() : pane.setOpen(true))}
            paneToggleRef={paneToggleRef}
          />
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
          deleteTarget={lifecycle.deleteTarget}
          onCancelDelete={() => lifecycle.setDeleteTarget(null)}
          onConfirmDelete={confirmDeleteSession}
          removeTarget={lifecycle.removeTarget}
          onCancelRemove={() => lifecycle.setRemoveTarget(null)}
          onConfirmRemove={confirmRemoveProject}
        />
        {extensionTarget && (
          <ExtensionsSettings
            // A new target is a new dialog: its tab, its loaded panels and the
            // project it names all belong to this one opening.
            key={`${extensionTarget.root ?? ""}|${extensionTarget.sessionId ?? ""}|${extensionTarget.initialTab}`}
            {...extensionTarget}
            projectName={projectLabel(extensionTarget.root)}
            projectPath={extensionTarget.root ?? workspaces.default ?? null}
            onClose={() => projects.setExtensionTarget(null)}
            onChanged={() => setExtensionsRevision((n) => n + 1)}
            onToast={showToast}
          />
        )}
        {editTarget && (
          <ProjectEditDialog
            key={editTarget.root ?? ""}
            target={editTarget}
            secondary={projectMeta.get(editTarget.root)?.secondary ?? []}
            viewTokenRef={openSeqRef}
            onClose={() => projects.setEditTarget(null)}
            onProjects={setProjects}
            onSaved={() => showToast("ok", "已保存项目设置")}
            onError={(msg) => showToast("err", msg)}
            onSessionsChanged={refreshSessions}
          />
        )}
      </main>
    </div>
  );
}
