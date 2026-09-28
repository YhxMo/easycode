import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import {
  fetchMcp,
  fetchMcpStatus,
  removeMcpCredential,
  removeMcpServer,
  saveMcpCredential,
  saveMcpServer,
  type McpInfo,
  type McpScope,
  type McpServerView,
  type McpStatusServer,
} from "../../api";
import { asString, configFrom, EMPTY_FORM, formFrom, type ServerForm } from "./mcpForm";
import { McpServerForm } from "./McpServerForm";

const SCOPE_LABELS: Record<McpScope, string> = {
  project: "当前项目",
  personal: "个人",
  app: "应用配置",
};

const STATE_LABELS: Record<McpStatusServer["state"], string> = {
  pending: "未连接",
  connected: "已连接",
  failed: "连接失败",
  disabled: "已停用",
};

export interface McpSettingsPanelProps {
  /** The project whose servers are shown; the effective list depends on it. */
  root: string | null;
  /** A session to read live connection status from, when one is open. */
  sessionId?: string | null;
  /** Whether this is the tab on screen; an inactive panel loads nothing. */
  active: boolean;
  /** The dialog's actions row, filled by whichever panel is active. */
  actionsHost: HTMLElement | null;
  onClose: () => void;
  /** The effective server set changed: the `/` menu must be re-read. */
  onChanged: () => void;
  onToast: (kind: "ok" | "err", text: string) => void;
}

export function McpSettingsPanel({
  root,
  sessionId,
  active,
  actionsHost,
  onClose,
  onChanged,
  onToast,
}: McpSettingsPanelProps) {
  const [info, setInfo] = useState<McpInfo | null>(null);
  const [status, setStatus] = useState<Record<string, McpStatusServer>>({});
  const [loadError, setLoadError] = useState<string | null>(null);
  const [form, setForm] = useState<ServerForm | null>(null);
  const [busy, setBusy] = useState(false);

  // Every mutation answers with the whole new view, so this is the only fetch
  // that is not the direct result of something the user just did. It waits for
  // the first look at this tab: the dialog must not read a project's servers
  // for a reader who never opened it.
  const loaded = useRef(false);
  useEffect(() => {
    if (!active || loaded.current) return;
    loaded.current = true;
    fetchMcp(root)
      .then((next) => {
        setInfo(next);
        setLoadError(null);
      })
      .catch((e) => setLoadError(e instanceof Error ? e.message : String(e)));
  }, [active, root]);

  // Status is per session and only exists once a turn has connected the
  // servers, so it is read separately from the configuration — and re-read
  // whenever the configuration changed, since a save drops the connections.
  useEffect(() => {
    if (!active || !sessionId) return;
    let live = true;
    void fetchMcpStatus(sessionId)
      .then((r) => {
        if (live) setStatus(Object.fromEntries(r.servers.map((s) => [s.name, s])));
      })
      .catch(() => {
        if (live) setStatus({});
      });
    return () => {
      live = false;
    };
  }, [active, sessionId, info]);

  const apply = async (run: () => Promise<McpInfo>, message: string) => {
    setBusy(true);
    try {
      setInfo(await run());
      onChanged();
      onToast("ok", message);
    } catch (e) {
      onToast("err", e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const submit = async () => {
    if (!form) return;
    const name = form.name.trim();
    if (!name) {
      onToast("err", "请填写服务名称");
      return;
    }
    const base =
      info?.servers.find((s) => s.name === name && s.scope === form.scope)?.config ?? {};
    const config = configFrom(form, base);
    setBusy(true);
    try {
      let next = await saveMcpServer(form.scope, root, name, config);
      if (form.secretValue) {
        try {
          next = await saveMcpCredential(form.scope, root, name, form.secretKind, {
            token: form.secretValue,
          });
        } catch (e) {
          // The server entry itself is already saved and in effect, so this is
          // a partial success: the menu is told to re-read, and the user is
          // told exactly which half is missing.
          setInfo(next);
          onChanged();
          onToast(
            "err",
            `已保存「${name}」，但凭据保存失败：${e instanceof Error ? e.message : String(e)}`,
          );
          return;
        }
      }
      setInfo(next);
      setForm(null);
      onChanged();
      onToast("ok", `已保存「${name}」`);
    } catch (e) {
      onToast("err", e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const setEnabled = (server: McpServerView, enabled: boolean) =>
    apply(
      () => saveMcpServer(server.scope, root, server.name, { ...server.config, enabled }),
      enabled ? `已启用「${server.name}」` : `已停用「${server.name}」`,
    );

  /** Disable a server for this project alone, leaving other projects alone. */
  const disableHere = (server: McpServerView) =>
    apply(
      () =>
        saveMcpServer(
          "project",
          root,
          server.name,
          { ...server.config, transport: server.config.transport ?? "stdio", enabled: false },
        ),
      `已在当前项目停用「${server.name}」`,
    );

  const restoreHere = (server: McpServerView) =>
    apply(
      () => removeMcpServer("project", root, server.name),
      `已恢复「${server.name}」的上一层配置`,
    );

  const remove = (server: McpServerView) =>
    apply(async () => {
      const next = await removeMcpServer(server.scope, root, server.name);
      await removeMcpCredential(server.scope, root, server.name).catch(() => undefined);
      return next;
    }, `已删除「${server.name}」`);

  return (
    <>
      {active && actionsHost
        ? createPortal(
            <>
              <button type="button" className="modal-cancel" onClick={onClose}>
                关闭
              </button>
              {form ? (
                <>
                  <button type="button" className="modal-cancel" onClick={() => setForm(null)}>
                    取消编辑
                  </button>
                  <button
                    type="button"
                    className="primary"
                    disabled={busy}
                    onClick={() => void submit()}
                  >
                    保存
                  </button>
                </>
              ) : (
                <button
                  type="button"
                  className="primary"
                  disabled={busy}
                  onClick={() => setForm({ ...EMPTY_FORM })}
                >
                  添加服务
                </button>
              )}
            </>,
            actionsHost,
          )
        : null}
      {loadError ? <p className="modal-error">{loadError}</p> : null}
      {info?.errors.map((message) => (
        <p className="modal-error" key={message}>
          {message}
        </p>
      ))}

      {form ? <McpServerForm form={form} info={info} onChange={setForm} /> : null}

      {info ? (
        <div className="modal-section">
          <div className="modal-section-title">
            当前生效的服务
            <span className="pane-count">{info.servers.length}</span>
          </div>
          {info.servers.length === 0 ? (
            <p className="modal-desc">还没有配置任何 MCP 服务。</p>
          ) : (
            <ul className="mcp-list">
              {info.servers.map((server) => {
                const live = status[server.name];
                return (
                  <li className={`mcp-row${server.enabled ? "" : " off"}`} key={server.name}>
                    <div className="mcp-row-head">
                      <span className="mcp-name">{server.name}</span>
                      <span className="mcp-scope">{SCOPE_LABELS[server.scope]}</span>
                      {live ? (
                        <span className={`mcp-state ${live.state}`}>{STATE_LABELS[live.state]}</span>
                      ) : null}
                      <span className="mcp-transport">
                        {server.config.transport === "http" ? "HTTP" : "stdio"}
                      </span>
                    </div>
                    <div className="mcp-row-meta">
                      <span title={asString(server.config, "command") || asString(server.config, "url")}>
                        {asString(server.config, "command") || asString(server.config, "url")}
                      </span>
                      {server.credential ? (
                        <span className="mcp-cred" title={server.credential.names.join(", ")}>
                          已存凭据
                        </span>
                      ) : null}
                      {live?.tools.length ? <span>{live.tools.length} 个工具</span> : null}
                    </div>
                    {live?.error ? <p className="mcp-error">{live.error}</p> : null}
                    {server.overrides.length ? (
                      <p className="mcp-note">
                        覆盖了{SCOPE_LABELS[server.overrides[server.overrides.length - 1]]}的同名配置
                      </p>
                    ) : null}
                    <div className="mcp-actions">
                      <button
                        type="button"
                        disabled={busy}
                        onClick={() => void setEnabled(server, !server.enabled)}
                      >
                        {server.enabled ? "停用" : "启用"}
                      </button>
                      <button type="button" disabled={busy} onClick={() => setForm(formFrom(server))}>
                        编辑
                      </button>
                      {server.scope === "project" && server.overrides.length ? (
                        <button type="button" disabled={busy} onClick={() => void restoreHere(server)}>
                          改用上一层
                        </button>
                      ) : server.scope !== "project" ? (
                        <button type="button" disabled={busy} onClick={() => void disableHere(server)}>
                          本项目停用
                        </button>
                      ) : null}
                      <button
                        type="button"
                        className="danger"
                        disabled={busy}
                        onClick={() => void remove(server)}
                      >
                        删除
                      </button>
                    </div>
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      ) : !loadError ? (
        <p className="modal-desc">正在读取配置…</p>
      ) : null}
    </>
  );
}
