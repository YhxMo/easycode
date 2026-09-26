import { useEffect, useRef, useState } from "react";
import type { RefObject } from "react";
import { useDismiss } from "./lib/useDismiss";
import type { SessionSummary, WorkspaceProject, WorkspacesInfo } from "./api";
import { chooseWorkspace, setSessionWorkspace } from "./api";
import { DirectoryCard } from "./DirectoryCard";
import { basename, DEFAULT_PROJECT } from "./lib/paths";

/**
 * The main-directory row of the sidebar.
 *
 * One card, two modes. A draft and a conversation that has not started a turn
 * yet may pick another directory: the draft's choice stays local until it is
 * sent, while a saved conversation's move is the server's to accept — a refusal
 * leaves the directory on screen exactly as it was. A conversation that already
 * ran something opens a read-only detail panel, because its records, previews
 * and tree report all describe that one directory.
 */
export function ProjectPicker({
  workspaces,
  loaded,
  root,
  sessionId,
  editable,
  disabled,
  viewToken,
  onRoot,
  onSecondary,
  onProjects,
  onSession,
  onNewChatHere,
  onError,
}: {
  workspaces: WorkspacesInfo;
  /** False until the workspace list has been fetched once. */
  loaded: boolean;
  root: string | null;
  /** The saved conversation this card belongs to; null on the start page. */
  sessionId: string | null;
  /** Whether this card may change the main directory. */
  editable: boolean;
  disabled: boolean;
  viewToken: RefObject<number>;
  onRoot: (r: string | null) => void;
  onSecondary: (r: string[]) => void;
  onProjects: (projects: WorkspaceProject[]) => void;
  /** The confirmed move of a saved conversation, with the project list it left behind. */
  onSession?: (session: SessionSummary, projects: WorkspaceProject[]) => void;
  onNewChatHere: (root: string | null) => void;
  onError?: (msg: string) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [open, setOpen] = useState(false);
  const pickerRef = useRef<HTMLDivElement>(null);

  useDismiss(pickerRef, () => setOpen(false), open);

  // switching the main root shows that project's bound secondary roots. Only a
  // draft does that here: a saved conversation's roots are the server's answer.
  useEffect(() => {
    if (sessionId) return;
    const proj = (workspaces.projects ?? []).find((p) => p.root === root);
    onSecondary(proj?.secondary ?? []);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [root]);

  /** Move to another directory, for the draft locally and for a session via the API. */
  const applyRoot = async (next: string | null) => {
    if (!sessionId) {
      onRoot(next);
      return;
    }
    const startView = viewToken.current;
    setBusy(true);
    try {
      const { session, projects } = await setSessionWorkspace(sessionId, next);
      if (viewToken.current !== startView) return;
      onSession?.(session, projects);
    } catch (e) {
      // The server refused the move: the conversation keeps its directory.
      if (viewToken.current === startView) onError?.(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const pickPrimaryViaFinder = async () => {
    if (disabled || busy) return;
    const startView = viewToken.current;
    setBusy(true);
    try {
      const { paths, supported } = await chooseWorkspace(false, "选择主目录");
      // A pick that started before the view changed belongs to that view only.
      if (viewToken.current !== startView) return;
      if (!supported) {
        onError?.("当前平台不支持访达选择，请用已有项目或重启后重试");
      } else if (paths[0]) {
        const chosen = paths[0];
        setOpen(false);
        if (sessionId) {
          await applyRoot(chosen);
        } else {
          onRoot(chosen);
          // register in the local pool so the dropdown keeps showing it
          if (!(workspaces.projects ?? []).some((p) => p.root === chosen)) {
            onProjects([...(workspaces.projects ?? []), { root: chosen, secondary: [] }]);
          }
        }
      }
    } catch (e) {
      if (viewToken.current === startView) onError?.(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const copyPath = async (path: string) => {
    try {
      await navigator.clipboard?.writeText(path);
      setOpen(false);
    } catch {
      onError?.("无法写入剪贴板");
    }
  };

  const projects = (workspaces.projects ?? [])
    .map((p) => p.root)
    .filter((r): r is string => Boolean(r));
  /** The name the sidebar shows: the project's own, else the folder name. */
  const displayName = (r: string | null) =>
    (workspaces.projects ?? []).find((p) => p.root === r)?.name ||
    (r ? basename(r) : DEFAULT_PROJECT);
  const defaultPath = workspaces.default;
  const currentPath = root ?? defaultPath;
  const currentName = displayName(root);
  const detail = currentPath ?? (loaded ? "路径未知" : "正在加载…");

  const selectRoot = (nextRoot: string | null) => {
    setOpen(false);
    void applyRoot(nextRoot);
  };

  return (
    <div className="project-picker" ref={pickerRef}>
      <DirectoryCard
        icon="⌂"
        name={currentName}
        detail={detail}
        title={currentPath ?? detail}
        expanded={open}
        popup
        disabled={editable && (disabled || busy)}
        onClick={() => setOpen(!open)}
      />
      {open && editable && (
        <div className="project-menu">
          <div className="project-menu-label">主项目目录</div>
          <div className="project-options" role="listbox" aria-label="选择主项目目录">
            <button
              type="button"
              role="option"
              aria-selected={!root}
              className={`project-option ${!root ? "active" : ""}`}
              title={defaultPath}
              onClick={() => selectRoot(null)}
            >
              <span className="project-option-mark" aria-hidden="true">{!root ? "✓" : ""}</span>
              {DEFAULT_PROJECT}
            </button>
            {projects.map((project) => (
              <button
                key={project}
                type="button"
                role="option"
                aria-selected={project === root}
                className={`project-option ${project === root ? "active" : ""}`}
                title={project}
                onClick={() => selectRoot(project)}
              >
                <span className="project-option-mark" aria-hidden="true">{project === root ? "✓" : ""}</span>
                {displayName(project)}
              </button>
            ))}
          </div>
          <button
            type="button"
            className="project-option project-option-add"
            disabled={disabled || busy}
            onClick={() => pickPrimaryViaFinder()}
          >
            <span className="project-option-mark" aria-hidden="true">＋</span>
            选择其他目录…
          </button>
        </div>
      )}
      {open && !editable && (
        <div className="project-menu project-detail" aria-label="会话目录">
          <div className="project-menu-label">会话工作目录</div>
          <p className="project-detail-path" title={currentPath ?? detail}>
            {currentPath ?? detail}
          </p>
          <button
            type="button"
            className="project-option"
            disabled={!currentPath}
            onClick={() => currentPath && copyPath(currentPath)}
          >
            <span className="project-option-mark" aria-hidden="true">⧉</span>
            复制路径
          </button>
          <button
            type="button"
            className="project-option"
            onClick={() => {
              setOpen(false);
              onNewChatHere(root);
            }}
          >
            <span className="project-option-mark" aria-hidden="true">＋</span>
            在此目录新建会话
          </button>
          <p className="project-detail-note">已有会话的工作目录不可修改。</p>
        </div>
      )}
    </div>
  );
}
