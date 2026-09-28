import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import {
  chooseWorkspace,
  fetchSkills,
  importSkill,
  type SkillView,
  type SkillsInfo,
} from "./api";
import { basename } from "./lib/paths";

const SCOPE_LABELS: Record<SkillView["scope"], string> = {
  personal: "个人",
  project: "当前项目",
};

interface ImportForm {
  scope: SkillView["scope"];
  path: string;
}

/**
 * The Skills tab: what is installed for this project, and how to add one.
 *
 * Installing copies a folder into easycode's own directory — the source is not
 * referred to afterwards, so the panel never shows a path the skill does not
 * actually live at. The list is the server's answer, never assembled here: the
 * filesystem is the only source of truth for what a conversation can load.
 */
export function SkillsSettings({
  root,
  active,
  actionsHost,
  onClose,
  onChanged,
  onToast,
}: {
  root: string | null;
  active: boolean;
  actionsHost: HTMLElement | null;
  onClose: () => void;
  onChanged: () => void;
  onToast: (kind: "ok" | "err", text: string) => void;
}) {
  const [info, setInfo] = useState<SkillsInfo | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [form, setForm] = useState<ImportForm | null>(null);
  const [busy, setBusy] = useState(false);
  // Refresh failures of an install that already succeeded. They stay on screen
  // until the next action: they are a fact about this install, not a transient
  // notification, and they are not something a retry of the import would fix.
  const [warnings, setWarnings] = useState<string[]>([]);

  const load = useCallback(
    () =>
      fetchSkills(root)
        .then((next) => {
          setInfo(next);
          setLoadError(null);
        })
        .catch((e: unknown) => setLoadError(e instanceof Error ? e.message : String(e))),
    [root],
  );

  // Read the list the first time this tab is looked at, not before: opening the
  // dialog on Skills must not read a project's folders for nothing.
  const loaded = useRef(false);
  useEffect(() => {
    if (!active || loaded.current) return;
    loaded.current = true;
    void load();
  }, [active, load]);

  const pickFolder = useCallback(async () => {
    if (busy) return;
    try {
      const { paths, supported } = await chooseWorkspace(false, "选择包含 SKILL.md 的文件夹");
      if (!supported) {
        // No native picker here: the field stays a plain path the user can type.
        onToast("err", "当前平台不支持系统选择器，请直接填写文件夹路径");
        return;
      }
      // A cancelled picker answers with no path: nothing to import, nothing to say.
      if (paths[0]) setForm((f) => (f ? { ...f, path: paths[0] } : f));
    } catch (e) {
      onToast("err", e instanceof Error ? e.message : String(e));
    }
  }, [busy, onToast]);

  const submit = useCallback(async () => {
    if (!form) return;
    const source = form.path.trim();
    if (!source) {
      onToast("err", "请选择或填写包含 SKILL.md 的文件夹");
      return;
    }
    setBusy(true);
    setWarnings([]);
    try {
      const result = await importSkill({ source_path: source, scope: form.scope, root });
      // The install is published: the form goes away whatever the warnings say,
      // because importing the same folder again is not the fix for a session
      // that could not be refreshed.
      setForm(null);
      await load();
      onChanged();
      const where = result.imported.directory;
      onToast("ok", `已安装「${result.imported.name}」到 ${where}`);
      if (result.warnings.length) setWarnings(result.warnings);
    } catch (e) {
      // Nothing was published, so the input stays exactly as the user left it.
      onToast("err", e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }, [form, root, load, onChanged, onToast]);

  const installRoot = info && form ? info.install_roots[form.scope] : "";
  const sourceName = form?.path.trim() ? basename(form.path.trim()) : "";

  const actions = (
    <>
      <button type="button" className="modal-cancel" onClick={onClose}>
        关闭
      </button>
      {form ? (
        <>
          <button type="button" className="modal-cancel" onClick={() => setForm(null)}>
            取消
          </button>
          <button type="button" className="primary" disabled={busy} onClick={() => void submit()}>
            导入
          </button>
        </>
      ) : (
        <button
          type="button"
          className="primary"
          disabled={busy}
          onClick={() => setForm({ scope: "project", path: "" })}
        >
          添加 Skill
        </button>
      )}
    </>
  );

  return (
    <>
      {active && actionsHost ? createPortal(actions, actionsHost) : null}

      {info && !info.enabled ? (
        <p className="modal-note">
          配置里已关闭 Skill：这里仍可查看和导入，但对话中的 <code>/名称</code> 与 use_skill
          都不会加载它们。
        </p>
      ) : null}
      {warnings.map((message) => (
        <p className="modal-warn" key={message}>
          {message}下一次发送时会重新加载扩展。
        </p>
      ))}

      {form ? (
        <div className="modal-section">
          <div className="modal-section-title">添加 Skill</div>
          <label className="modal-field">
            <span>作用域</span>
            <span className="modal-select-wrap">
              <select
                value={form.scope}
                onChange={(e) =>
                  setForm({ ...form, scope: e.target.value as SkillView["scope"] })
                }
              >
                <option value="project">当前项目</option>
                <option value="personal">个人（所有项目）</option>
              </select>
            </span>
          </label>
          <label className="modal-field">
            <span>源文件夹</span>
            <span className="skill-source-row">
              <input
                type="text"
                value={form.path}
                placeholder="/路径/到/包含 SKILL.md 的文件夹"
                onChange={(e) => setForm({ ...form, path: e.target.value })}
              />
              <button type="button" disabled={busy} onClick={() => void pickFolder()}>
                选择文件夹
              </button>
            </span>
            <small>
              整个文件夹会复制到安装目录，之后不再引用原位置；导入不执行其中的脚本。
            </small>
          </label>
          <div className="skill-preview">
            <span>安装到</span>
            <code>
              {installRoot}
              {sourceName ? `/${sourceName}` : ""}
            </code>
          </div>
        </div>
      ) : null}

      {loadError ? (
        <div className="modal-error">
          <p>读取 Skill 失败：{loadError}</p>
          <button type="button" onClick={() => void load()}>
            重试
          </button>
        </div>
      ) : null}

      {info?.errors.length ? (
        <div className="modal-warn">
          <p>以下条目无法读取，已跳过：</p>
          {info.errors.map((message) => (
            <p key={message}>{message}</p>
          ))}
        </div>
      ) : null}

      {info ? (
        <div className="modal-section">
          <div className="modal-section-title">
            已安装的 Skill
            <span className="pane-count">{info.skills.length}</span>
          </div>
          {info.skills.length === 0 ? (
            <p className="modal-desc">还没有安装任何 Skill。</p>
          ) : (
            <ul className="mcp-list">
              {info.skills.map((skill) => (
                <li
                  className={`skill-row${skill.effective ? "" : " shadowed"}`}
                  key={`${skill.scope}:${skill.directory}`}
                >
                  <div className="mcp-row-head">
                    <span className="mcp-name">{skill.name}</span>
                    <span className="mcp-scope">{SCOPE_LABELS[skill.scope]}</span>
                    {!skill.effective ? (
                      <span className="skill-shadowed" title="同名项在更高作用域生效">
                        被同名项覆盖
                      </span>
                    ) : null}
                  </div>
                  <p className="skill-desc">{skill.description}</p>
                  <div className="mcp-row-meta">
                    <code className="skill-command">/{skill.name}</code>
                    <span title={skill.path}>{skill.directory}</span>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </div>
      ) : !loadError ? (
        <p className="modal-desc">正在读取 Skill…</p>
      ) : null}
    </>
  );
}
