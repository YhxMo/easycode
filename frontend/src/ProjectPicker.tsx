import { useEffect, useState } from "react";
import type { WorkspacesInfo } from "./api";
import { chooseWorkspace } from "./api";
import { SecondaryEditor } from "./SecondaryEditor";

const DEFAULT_PROJECT = "default project";

export const basename = (p: string) => p.split(/[\\/]/).filter(Boolean).pop() ?? p;

export function ProjectPicker({
  workspaces,
  root,
  secondary,
  disabled,
  onRoot,
  onSecondary,
  onWorkspaces,
}: {
  workspaces: WorkspacesInfo;
  root: string | null;
  secondary: string[];
  disabled: boolean;
  onRoot: (r: string | null) => void;
  onSecondary: (r: string[]) => void;
  onWorkspaces: (w: WorkspacesInfo) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  // switching the main root shows that project's bound secondary roots
  useEffect(() => {
    const proj = (workspaces.projects ?? []).find((p) => p.root === root);
    onSecondary((proj?.secondary ?? []).filter((x) => x !== undefined));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [root]);

  const pickPrimaryViaFinder = async () => {
    setBusy(true);
    setError("");
    try {
      const { paths, supported } = await chooseWorkspace(false, "选择主目录");
      if (!supported) {
        setError("当前平台不支持访达选择，请用已有项目或重启后重试");
      } else if (paths[0]) {
        const chosen = paths[0];
        onRoot(chosen);
        // register in the local pool so the dropdown keeps showing it
        if (!(workspaces.projects ?? []).some((p) => p.root === chosen)) {
          onWorkspaces({
            ...workspaces,
            projects: [...(workspaces.projects ?? []), { root: chosen, secondary: [] }],
          });
        }
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="project-picker">
      <div className="project-picker-block">
        <div className="picker-row">
          <select
            className="project-select"
            value={root ?? ""}
            disabled={disabled || busy}
            onChange={(e) => onRoot(e.target.value || null)}
          >
            <option value="">{DEFAULT_PROJECT}</option>
            {(workspaces.projects ?? [])
              .map((p) => p.root)
              .filter((r): r is string => Boolean(r))
              .map((w) => (
                <option key={w} value={w} title={w}>
                  {basename(w)}
                </option>
              ))}
          </select>
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
        <SecondaryEditor
          root={root}
          secondary={secondary}
          disabled={disabled}
          onSecondary={onSecondary}
          onWorkspaces={onWorkspaces}
        />
        {error && <div className="picker-error">{error}</div>}
      </div>
    </div>
  );
}
