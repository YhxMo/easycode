import type { ReactNode, RefObject } from "react";
import { useCallback, useState } from "react";
import type { SessionSummary, WorkspaceProject, WorkspacesInfo } from "../api";
import type { StreamActivityMap } from "../useChatStream";
import { ProjectMenu, type ProjectAction } from "../ProjectMenu";
import { ProjectPicker } from "../ProjectPicker";
import { SecondaryEditor } from "../SecondaryEditor";
import type { ProjectGroup, SessionGroups } from "../lib/sessionGroups";
import { SessionProjectTooltip, type ProjectHint } from "./SessionProjectTooltip";
import { basename, DEFAULT_PROJECT } from "../lib/paths";

/** Working directories listed before the project list spills. */
const PROJECT_PREVIEW = 5;

function FolderIcon() {
  return (
    <svg className="side-folder" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <path d="M3.5 7.5h6l1.8 2h9.2v7.8a2.7 2.7 0 0 1-2.7 2.7H6.2a2.7 2.7 0 0 1-2.7-2.7V7.5Z" />
      <path d="M3.5 7.5V6.7A2.7 2.7 0 0 1 6.2 4h3.1l2 2h3.2" />
    </svg>
  );
}

function PinIcon() {
  return (
    <svg className="side-pin" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <path d="M9.5 4h5l-.7 5.2 2.7 2.6v1.7H7.5v-1.7l2.7-2.6z" />
      <path d="M12 13.5V20" />
    </svg>
  );
}

/** The pin's own body is the closed cap path; the stem stays a stroke. */
function PinButton({ session, onToggle }: { session: SessionSummary; onToggle: () => void }) {
  return (
    <button
      type="button"
      className={`session-pin${session.pinned ? " on" : ""}`}
      title={session.pinned ? "取消置顶" : "置顶会话"}
      aria-label={session.pinned ? `取消置顶 ${session.title}` : `置顶会话 ${session.title}`}
      aria-pressed={Boolean(session.pinned)}
      onClick={onToggle}
    >
      <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
        <path className="session-pin-cap" d="M9.5 4h5l-.7 5.2 2.7 2.6v1.7H7.5v-1.7l2.7-2.6z" />
        <path d="M12 13.5V20" />
      </svg>
    </button>
  );
}

function Caret({ open }: { open: boolean }) {
  return (
    <span className={`side-caret${open ? " open" : ""}`} aria-hidden="true">
      <svg viewBox="0 0 24 24" focusable="false">
        <path d="M9 5.5 15.5 12 9 18.5" />
      </svg>
    </span>
  );
}

/** New conversation in this project: a square with a pencil over its corner. */
function ComposeIcon() {
  return (
    <svg className="side-compose" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <path d="M13.6 5.6H6.3a2.6 2.6 0 0 0-2.6 2.6v9.5a2.6 2.6 0 0 0 2.6 2.6h9.5a2.6 2.6 0 0 0 2.6-2.6v-7.3" />
      <path d="M17.3 3.5l3.2 3.2-7.2 7.2-3.9.7.7-3.9z" />
    </svg>
  );
}

/**
 * One conversation row.
 *
 * The row is a container rather than a single button: opening it and acting on
 * it (pin, delete) are separate controls, so no button is nested in another.
 */
function SessionRow({
  session,
  current,
  activity,
  hint,
  onOpen,
  onTogglePin,
  onDelete,
}: {
  session: SessionSummary;
  current: boolean;
  activity: { busy?: boolean; approvals?: number } | undefined;
  /** ``null`` for an ordinary row, which keeps its plain title instead. */
  hint: ProjectHint | null;
  onOpen: () => void;
  onTogglePin: () => void;
  onDelete: () => void;
}) {
  // The hint places itself beside this element, so it needs the node itself.
  const [anchor, setAnchor] = useState<HTMLButtonElement | null>(null);
  return (
    <div
      className={`session-item${current ? " active" : ""}${activity?.busy ? " running" : ""}`}
    >
      <button
        type="button"
        className="session-open"
        ref={setAnchor}
        title={hint ? undefined : session.title}
        aria-current={current ? "page" : undefined}
        onClick={onOpen}
      >
        <span className="session-title">{session.title}</span>
        {activity?.busy && <span className="session-run" aria-label="正在运行" />}
      </button>
      {activity?.approvals ? (
        <span className="session-ask" title={`${activity.approvals} 个待批准`}>
          待批准
        </span>
      ) : null}
      <span className="session-actions">
        <PinButton session={session} onToggle={onTogglePin} />
        <button
          type="button"
          className="session-del"
          title="删除"
          aria-label={`删除会话 ${session.title}`}
          onClick={onDelete}
        >
          <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
            <path d="M6 6l12 12M18 6L6 18" />
          </svg>
        </button>
      </span>
      {hint && <SessionProjectTooltip anchor={anchor} title={session.title} hint={hint} />}
    </div>
  );
}

export interface SidebarProps {
  currentId: string | null;
  /** Per-session running/approval state, for row badges. */
  activity: StreamActivityMap;
  groups: SessionGroups;
  archived: SessionSummary[];
  showArchived: boolean;
  /** Collapsed top-level sections, keyed `sec:<name>`. */
  collapsedSections: Record<string, boolean>;
  collapsedProjects: Record<string, boolean>;
  busy: boolean;
  /** Foreground session loading/failed: session-scoped edits stay disabled. */
  sessionBlocked: boolean;
  /** Current view version, for dropping superseded secondary-root saves. */
  viewToken: RefObject<number>;
  workspaces: WorkspacesInfo;
  /** False until the workspace list has been fetched once (loading card state). */
  workspacesLoaded: boolean;
  chosenRoot: string | null;
  currentRoot: string | null;
  /** Secondary roots of the draft (new session) or open session. */
  secondary: string[];
  /** Whether the open conversation may still change its main directory. */
  rootEditable: boolean;
  onNewSession: () => void;
  /** Project a new conversation joins, for the button's hint. */
  newSessionLabel: string;
  onOpenSession: (id: string) => void;
  onToggleSection: (key: string) => void;
  onToggleCollapsed: (key: string) => void;
  onNewChatInProject: (root: string | null) => void;
  onProjectAction: (root: string | null, action: ProjectAction) => void;
  onSetChosenRoot: (root: string | null) => void;
  onSetSecondary: (secondary: string[]) => void;
  onProjects: (projects: WorkspaceProject[]) => void;
  /** The workspace a saved conversation moved to, as the server confirmed it. */
  onSessionWorkspace: (session: SessionSummary, projects: WorkspaceProject[]) => void;
  onToggleArchived: () => void;
  onDeleteSession: (session: SessionSummary) => void;
  onTogglePin: (session: SessionSummary) => void;
  onRestoreSession: (session: SessionSummary) => void;
  /** Open the MCP settings for the project the current view is in. */
  onOpenMcp: () => void;
  /** Global error reporter (app-level toast) for picker/editor failures. */
  onError: (message: string) => void;
}

export function Sidebar({
  currentId,
  activity,
  groups,
  archived,
  showArchived,
  collapsedSections,
  collapsedProjects,
  busy,
  sessionBlocked,
  viewToken,
  workspaces,
  workspacesLoaded,
  chosenRoot,
  currentRoot,
  secondary,
  rootEditable,
  onNewSession,
  newSessionLabel,
  onOpenSession,
  onToggleSection,
  onToggleCollapsed,
  onNewChatInProject,
  onProjectAction,
  onSetChosenRoot,
  onSetSecondary,
  onProjects,
  onSessionWorkspace,
  onToggleArchived,
  onDeleteSession,
  onTogglePin,
  onRestoreSession,
  onOpenMcp,
  onError,
}: SidebarProps) {
  const [showAllProjects, setShowAllProjects] = useState(false);

  /**
   * Which project a conversation runs in, for the hovering hint.
   *
   * A row's own root names its project directly. A row without one runs in the
   * default workspace, which is known only once the workspace list has loaded:
   * until then the hint says so rather than claiming the conversation has no
   * project at all.
   */
  const projectHint = useCallback(
    (s: SessionSummary): ProjectHint => {
      if (s.root) {
        const group = groups.projects.find((g) => g.root === s.root);
        return { name: group?.name ?? basename(s.root), root: s.root };
      }
      if (!workspacesLoaded) return { name: "正在读取所属项目", root: null };
      const fallback = workspaces.default ?? null;
      // The default workspace may be bound as a project under its own path, or
      // configured as the root-less entry itself.
      const group = groups.projects.find(
        (g) => g.root === null || (fallback !== null && g.root === fallback),
      );
      return {
        name: group?.name ?? (fallback ? basename(fallback) : DEFAULT_PROJECT),
        root: fallback,
      };
    },
    [groups.projects, workspaces.default, workspacesLoaded],
  );

  /**
   * One conversation row: open it, or act on it from its own buttons.
   *
   * A pinned row carries its project in the hovering hint instead of the row
   * ``title``: the row sits in the pinned section, away from the project that
   * would otherwise be obvious, and one title cannot be both facts.
   */
  const sessionRow = (s: SessionSummary) => (
    <SessionRow
      key={s.id}
      session={s}
      current={s.id === currentId}
      activity={activity[s.id]}
      hint={s.pinned ? projectHint(s) : null}
      onOpen={() => onOpenSession(s.id)}
      onTogglePin={() => onTogglePin(s)}
      onDelete={() => onDeleteSession(s)}
    />
  );

  const projectBlock = (g: ProjectGroup) => {
    const key = `proj:${g.root}`;
    const collapsed = Boolean(collapsedProjects[key]);
    return (
      <div key={g.root} className={`project-group${collapsed ? " collapsed" : ""}`}>
        <div className="project-group-head" title={g.root}>
          <button
            type="button"
            className="group-head-main"
            title={g.root}
            aria-expanded={!collapsed}
            aria-label={collapsed ? "展开项目" : "折叠项目"}
            onClick={() => onToggleCollapsed(key)}
          >
            <Caret open={!collapsed} />
            <FolderIcon />
            <span className="project-group-name">{g.name}</span>
            {g.pinned && (
              <span className="project-pin-badge" title="已置顶" aria-label="已置顶">
                <PinIcon />
              </span>
            )}
          </button>
          <div className="group-head-actions">
            <ProjectMenu
              pinned={g.pinned}
              running={g.sessions.some((s) => activity[s.id]?.busy)}
              onAction={(action) => onProjectAction(g.root, action)}
            />
            <button
              type="button"
              className="group-new-btn"
              title="在此项目下新建会话"
              aria-label="在此项目下新建会话"
              onClick={(e) => {
                e.stopPropagation();
                onNewChatInProject(g.root);
              }}
            >
              <ComposeIcon />
            </button>
          </div>
        </div>
        {!collapsed && g.visibleSessions.map(sessionRow)}
        {!collapsed && g.sessions.length === 0 && (
          <div className="project-empty">还没有会话</div>
        )}
        {!collapsed && g.sessions.length > 0 && g.visibleSessions.length === 0 && (
          <div className="project-empty">会话已全部置顶</div>
        )}
      </div>
    );
  };

  const section = (key: string, label: string, body: ReactNode, count?: number) => {
    const collapsed = Boolean(collapsedSections[key]);
    return (
      <section className={`side-sec${collapsed ? " collapsed" : ""}`}>
        <button
          type="button"
          className="side-head"
          aria-expanded={!collapsed}
          onClick={() => onToggleSection(key)}
        >
          <Caret open={!collapsed} />
          <span className="side-head-label">{label}</span>
          {count ? <span className="side-count">{count}</span> : null}
        </button>
        {!collapsed && <div className="side-body">{body}</div>}
      </section>
    );
  };

  const projects = showAllProjects ? groups.projects : groups.projects.slice(0, PROJECT_PREVIEW);

  return (
    <aside className="sidebar" id="sidebar">
      <div className="brand">
        <span className="brand-mark" aria-hidden="true">
          &gt;_
        </span>
        <h1>Easy code</h1>
      </div>
      {/* Global, not per conversation: every project needs a way in, and a
          sidebar with no project group (the default workspace) has none. */}
      <button
        type="button"
        className="brand-settings"
        title="MCP 服务"
        aria-label="MCP 服务设置"
        onClick={onOpenMcp}
      >
        <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
          <path d="M12 3v6m0 0-3 3v9m3-12 3 3v9M4 6h4M16 6h4M9 18h6" />
        </svg>
      </button>
      <div className="sidebar-scroll">
        {section(
          "sec:dirs",
          "目录",
          <>
            <div className="dir-area">
              <ProjectPicker
                workspaces={workspaces}
                loaded={workspacesLoaded}
                root={currentId === null ? chosenRoot : currentRoot}
                sessionId={currentId}
                editable={rootEditable}
                disabled={busy}
                viewToken={viewToken}
                onRoot={onSetChosenRoot}
                onSecondary={onSetSecondary}
                onProjects={onProjects}
                onSession={onSessionWorkspace}
                onNewChatHere={onNewChatInProject}
                onError={onError}
              />
              <SecondaryEditor
                root={currentId === null ? chosenRoot : currentRoot}
                secondary={secondary}
                sessionId={currentId}
                disabled={busy || sessionBlocked}
                viewToken={viewToken}
                onSecondary={onSetSecondary}
                onProjects={onProjects}
                onError={onError}
              />
            </div>
            <button
              className="new-btn"
              type="button"
              title={`在「${newSessionLabel}」新建会话`}
              onClick={onNewSession}
            >
              <span aria-hidden="true">＋</span> 新会话
              <small className="new-btn-hint">{newSessionLabel}</small>
            </button>
          </>,
        )}

        {groups.pinned.length > 0 &&
          section("sec:pinned", "置顶", groups.pinned.map(sessionRow))}

        {section(
          "sec:projects",
          "项目",
          <>
            {projects.map(projectBlock)}
            {groups.projects.length > PROJECT_PREVIEW && (
              <button
                type="button"
                className="side-more"
                onClick={() => setShowAllProjects((v) => !v)}
              >
                {showAllProjects ? "收起" : "展开显示"}
              </button>
            )}
            {groups.projects.length === 0 && (
              <div className="session-empty">添加项目以组织会话</div>
            )}
          </>,
          groups.projects.length || undefined,
        )}

        {section(
          "sec:recents",
          "最近",
          groups.recents.length ? (
            groups.recents.map(sessionRow)
          ) : (
            <div className="session-empty">默认工作区的会话显示在这里</div>
          ),
        )}

        {archived.length > 0 && (
          <section className="side-sec">
            <button
              type="button"
              className="side-head"
              onClick={onToggleArchived}
              aria-expanded={showArchived}
            >
              <Caret open={showArchived} />
              <span className="side-head-label">已归档</span>
              <span className="side-count">{archived.length}</span>
            </button>
            {showArchived && (
              <div className="side-body archive-list">
                {archived.map((s) => (
                  <div key={s.id} className="session-item archived">
                    <span className="session-title" title={s.title}>
                      {s.title}
                    </span>
                    <div className="archive-actions">
                      <button
                        type="button"
                        className="archive-restore"
                        title="恢复"
                        aria-label="恢复"
                        onClick={(e) => {
                          e.stopPropagation();
                          onRestoreSession(s);
                        }}
                      >
                        ↥
                      </button>
                      <button
                        type="button"
                        className="archive-del"
                        title="彻底删除"
                        aria-label="彻底删除"
                        onClick={(e) => {
                          e.stopPropagation();
                          onDeleteSession(s);
                        }}
                      >
                        ✕
                      </button>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </section>
        )}
      </div>
    </aside>
  );
}
