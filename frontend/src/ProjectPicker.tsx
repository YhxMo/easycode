import { useEffect, useRef, useState } from "react";
import type { WorkspaceProject, WorkspacesInfo } from "./api";
import { chooseWorkspace } from "./api";
import { basename, DEFAULT_PROJECT } from "./lib/paths";
import { SecondaryEditor } from "./SecondaryEditor";

export function ProjectPicker({
  workspaces,
  root,
  secondary,
  disabled,
  onRoot,
  onSecondary,
  onProjects,
  onError,
}: {
  workspaces: WorkspacesInfo;
  root: string | null;
  secondary: string[];
  disabled: boolean;
  onRoot: (r: string | null) => void;
  onSecondary: (r: string[]) => void;
  onProjects: (projects: WorkspaceProject[]) => void;
  onError?: (msg: string) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [open, setOpen] = useState(false);
  const pickerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const closeOnOutsideClick = (event: MouseEvent) => {
      if (pickerRef.current && !pickerRef.current.contains(event.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener("mousedown", closeOnOutsideClick);
    return () => document.removeEventListener("mousedown", closeOnOutsideClick);
  }, []);

  // switching the main root shows that project's bound secondary roots
  useEffect(() => {
    const proj = (workspaces.projects ?? []).find((p) => p.root === root);
    onSecondary(proj?.secondary ?? []);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [root]);

  const pickPrimaryViaFinder = async () => {
    setBusy(true);
    setError("");
    try {
      const { paths, supported } = await chooseWorkspace(false, "选择主目录");
      if (!supported) {
        setError("当前平台不支持访达选择，请用已有项目或重启后重试");
        onError?.("当前平台不支持访达选择，请用已有项目或重启后重试");
      } else if (paths[0]) {
        const chosen = paths[0];
        onRoot(chosen);
        // register in the local pool so the dropdown keeps showing it
        if (!(workspaces.projects ?? []).some((p) => p.root === chosen)) {
          onProjects([...(workspaces.projects ?? []), { root: chosen, secondary: [] }]);
        }
      }
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      setError(msg);
      onError?.(msg);
    } finally {
      setBusy(false);
    }
  };

  const projects = (workspaces.projects ?? [])
    .map((p) => p.root)
    .filter((r): r is string => Boolean(r));
  const currentName = root ? basename(root) : DEFAULT_PROJECT;

  const selectRoot = (nextRoot: string | null) => {
    setOpen(false);
    onRoot(nextRoot);
  };

  return (
    <div className="project-picker" ref={pickerRef}>
      <div className="project-picker-block">
        <div className="picker-row">
          <button
            type="button"
            className={`project-select ${open ? "open" : ""}`}
            disabled={disabled || busy}
            aria-expanded={open}
            aria-haspopup="listbox"
            onClick={() => setOpen(!open)}
          >
            <span className="project-select-icon" aria-hidden="true">⌂</span>
            <span className="project-select-copy">
              <strong>{currentName}</strong>
              <small>{root ?? "使用默认工作区"}</small>
            </span>
            <span className="project-select-caret" aria-hidden="true">⌄</span>
          </button>
          <button
            className="project-add-btn primary-folder-add"
            title="用访达选择主目录"
            aria-label="用访达添加主目录"
            disabled={disabled || busy}
            onClick={() => pickPrimaryViaFinder()}
          >
            <span aria-hidden="true">＋</span>
          </button>
        </div>
        {open && (
          <div className="project-menu" role="listbox" aria-label="选择主项目目录">
            <div className="project-menu-label">主项目目录</div>
            <button
              type="button"
              role="option"
              aria-selected={!root}
              className={`project-option ${!root ? "active" : ""}`}
              onClick={() => selectRoot(null)}
            >
              <span className="project-option-mark" aria-hidden="true">{!root ? "✓" : ""}</span>
              <span className="project-option-copy">
                <strong>{DEFAULT_PROJECT}</strong>
                <small>不绑定本地目录</small>
              </span>
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
                <span className="project-option-copy">
                  <strong>{basename(project)}</strong>
                  <small>{project}</small>
                </span>
              </button>
            ))}
          </div>
        )}
        <SecondaryEditor
          root={root}
          secondary={secondary}
          disabled={disabled}
          onSecondary={onSecondary}
          onProjects={onProjects}
          onError={onError}
        />
        {error && <div className="picker-error">{error}</div>}
      </div>
    </div>
  );
}
