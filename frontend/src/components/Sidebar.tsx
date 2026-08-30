import type {
  ModelsInfo,
  SessionSummary,
  WorkspaceProject,
  WorkspacesInfo,
} from "../api";
import { ModelPicker } from "../ModelPicker";
import { ProjectMenu, type ProjectAction } from "../ProjectMenu";
import { ProjectPicker, basename } from "../ProjectPicker";
import { SecondaryEditor } from "../SecondaryEditor";

const DEFAULT_PROJECT = "default project";

function FolderIcon() {
  return (
    <svg className="project-folder-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <path d="M3.5 7.5h6l1.8 2h9.2v7.8a2.7 2.7 0 0 1-2.7 2.7H6.2a2.7 2.7 0 0 1-2.7-2.7V7.5Z" />
      <path d="M3.5 7.5V6.7A2.7 2.7 0 0 1 6.2 4h3.1l2 2h3.2" />
    </svg>
  );
}

function PinBadgeIcon() {
  return (
    <svg className="project-pin-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <path d="M12 3v9m0 0-4-4m4 4 4-4M6 21h12" />
    </svg>
  );
}

export interface SidebarProps {
  currentId: string | null;
  archived: SessionSummary[];
  showArchived: boolean;
  collapsedProjects: Record<string, boolean>;
  busy: boolean;
  models: ModelsInfo;
  workspaces: WorkspacesInfo;
  groups: Array<[string | null, SessionSummary[]]>;
  projectMeta: Map<string | null, WorkspaceProject>;
  chosenRoot: string | null;
  chosenSecondary: string[];
  currentRoot: string | null;
  currentSecondary: string[];
  onNewSession: () => void;
  onOpenSession: (id: string) => void;
  onToggleCollapsed: (key: string) => void;
  onNewChatInProject: (root: string | null) => void;
  onProjectAction: (root: string | null, action: ProjectAction) => void;
  onSetChosenRoot: (root: string | null) => void;
  onSetChosenSecondary: (secondary: string[]) => void;
  onSetCurrentSecondary: (secondary: string[]) => void;
  onSetWorkspaces: (workspaces: WorkspacesInfo) => void;
  onToggleArchived: () => void;
  onDeleteSession: (session: SessionSummary) => void;
  onRestoreSession: (session: SessionSummary) => void;
  onModelChange: (models: ModelsInfo) => void;
  /** Global error reporter (app-level toast) for picker/editor failures. */
  onError: (message: string) => void;
}

export function Sidebar({
  currentId,
  archived,
  showArchived,
  collapsedProjects,
  busy,
  models,
  workspaces,
  groups,
  projectMeta,
  chosenRoot,
  chosenSecondary,
  currentRoot,
  currentSecondary,
  onNewSession,
  onOpenSession,
  onToggleCollapsed,
  onNewChatInProject,
  onProjectAction,
  onSetChosenRoot,
  onSetChosenSecondary,
  onSetCurrentSecondary,
  onSetWorkspaces,
  onToggleArchived,
  onDeleteSession,
  onRestoreSession,
  onModelChange,
  onError,
}: SidebarProps) {
  return (
    <aside className="sidebar" id="sidebar">
      <div className="brand">
        <span className="brand-mark" aria-hidden="true">&gt;_</span>
        <h1>Easy code</h1>
      </div>
      <button className="new-btn" type="button" onClick={onNewSession}>
        <span aria-hidden="true">＋</span> 新会话
      </button>
      <div className="project-picker">
        {currentId === null ? (
          <ProjectPicker
            workspaces={workspaces}
            root={chosenRoot}
            secondary={chosenSecondary}
            disabled={busy}
            onRoot={onSetChosenRoot}
            onSecondary={onSetChosenSecondary}
            onWorkspaces={onSetWorkspaces}
            onError={onError}
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
            onSecondary={onSetCurrentSecondary}
            onWorkspaces={onSetWorkspaces}
            onError={onError}
          />
        )}
      </div>
      <div className="session-list">
        {groups.length > 0 && <div className="session-list-label">项目</div>}
        {groups.map(([root, list]) => {
          const meta = projectMeta.get(root);
          const pinned = meta?.pinned ?? false;
          const groupKey = root ?? "__default__";
          const isCollapsed = Boolean(collapsedProjects[groupKey]);
          return (
          <div key={groupKey} className={`project-group ${isCollapsed ? "collapsed" : ""}`}>
            <div className="project-group-head" title={root ?? DEFAULT_PROJECT}>
              <button
                type="button"
                className="group-head-main"
                title={root ?? DEFAULT_PROJECT}
                aria-expanded={!isCollapsed}
                aria-label={isCollapsed ? "展开项目" : "折叠项目"}
                onClick={() => onToggleCollapsed(groupKey)}
              >
                <span className="project-collapse-caret" aria-hidden="true">{isCollapsed ? "›" : "⌄"}</span>
                <FolderIcon />
                <span className="project-group-name">{meta?.name ?? (root ? basename(root) : DEFAULT_PROJECT)}</span>
                {pinned && (
                  <span className="project-pin-badge" title="已置顶" aria-label="已置顶">
                    <PinBadgeIcon />
                  </span>
                )}
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
                    onNewChatInProject(root);
                  }}
                >
                  <span aria-hidden="true">＋</span>
                </button>
                <ProjectMenu
                  pinned={pinned}
                  disabled={busy}
                  onAction={(action) => onProjectAction(root, action)}
                />
              </div>
            </div>
            {!isCollapsed && list.map((s) => (
              <div
                key={s.id}
                className={`session-item ${s.id === currentId ? "active" : ""}`}
                onClick={() => onOpenSession(s.id)}
              >
                <span className="session-title">{s.title}</span>
                <span
                  className="session-del"
                  title="删除"
                  onClick={(e) => {
                    e.stopPropagation();
                    onDeleteSession(s);
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
              onClick={onToggleArchived}
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
          </div>
        )}
      </div>
      <div className="sidebar-foot">
        <ModelPicker models={models} current={models.default} onChange={onModelChange} onError={onError} />
      </div>
    </aside>
  );
}
