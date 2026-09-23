import { useState } from "react";
import type { WorkspaceProject } from "./api";
import { chooseWorkspace, saveProject } from "./api";
import { basename, DEFAULT_PROJECT } from "./lib/paths";

export function SecondaryEditor({
  root,
  secondary,
  sessionId,
  disabled,
  onSecondary,
  onProjects,
  onError,
}: {
  root: string | null;
  secondary: string[];
  sessionId?: string | null;
  disabled: boolean;
  onSecondary: (r: string[]) => void;
  onProjects?: (projects: WorkspaceProject[]) => void;
  onError?: (msg: string) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [open, setOpen] = useState(false);

  const mutate = async (next: string[]) => {
    setBusy(true);
    setError("");
    try {
      const { secondary: saved, projects } = await saveProject(
        root,
        next,
        sessionId ?? undefined,
      );
      onSecondary(saved);
      onProjects?.(projects);
      return saved;
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      setError(msg);
      onError?.(msg);
      return null;
    } finally {
      setBusy(false);
    }
  };

  const pickViaFinder = async () => {
    setBusy(true);
    setError("");
    try {
      const { paths, supported } = await chooseWorkspace(
        true,
        `为 ${root ? basename(root) : DEFAULT_PROJECT} 添加次目录（可多选）`,
      );
      if (!supported) {
        setError("当前平台不支持访达选择");
        onError?.("当前平台不支持访达选择");
      } else if (paths.length) {
        await mutate([...new Set([...secondary, ...paths])]);
      }
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      setError(msg);
      onError?.(msg);
    } finally {
      setBusy(false);
    }
  };

  const remove = async (p: string) => {
    await mutate(secondary.filter((x) => x !== p));
  };

  return (
    <div className="secondary-editor">
      <button
        type="button"
        className={`sec-toggle ${open ? "open" : ""}`}
        aria-expanded={open}
        onClick={() => setOpen(!open)}
      >
        <span className="sec-toggle-icon" aria-hidden="true">↳</span>
        <span className="sec-toggle-copy">
          <strong>次目录</strong>
          <small>{secondary.length > 0 ? `已连接 ${secondary.length} 个目录` : "添加辅助工作区"}</small>
        </span>
        <span className="sec-toggle-caret" aria-hidden="true">⌄</span>
      </button>
      {open && (
        <div className="secondary-panel">
          <div className="secondary-list">
            {secondary.map((p) => (
              <div key={p} className="secondary-item" title={p}>
                <span className="secondary-item-icon" aria-hidden="true">⌘</span>
                <span className="secondary-item-copy">
                  <strong>{basename(p)}</strong>
                  <small>{p}</small>
                </span>
                <button
                  type="button"
                  className="secondary-remove"
                  title={`移除 ${basename(p)}`}
                  aria-label={`移除次目录 ${basename(p)}`}
                  disabled={busy}
                  onClick={() => remove(p)}
                >
                  ✕
                </button>
              </div>
            ))}
          </div>
          {secondary.length === 0 && (
            <div className="secondary-empty">次目录可同时提供其他代码库作为上下文</div>
          )}
          <button
            type="button"
            className="secondary-add"
            title="用访达添加次目录（可多选）"
            disabled={disabled || busy}
            onClick={() => pickViaFinder()}
          >
            <span aria-hidden="true">＋</span>
            添加次目录
          </button>
        </div>
      )}
      {error && <div className="picker-error">{error}</div>}
    </div>
  );
}
