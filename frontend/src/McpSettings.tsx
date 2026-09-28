import { useCallback, useEffect, useMemo, useRef, useState } from "react";
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
} from "./api";

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

interface ServerForm {
  scope: McpScope;
  name: string;
  transport: "stdio" | "http";
  command: string;
  args: string;
  cwd: string;
  env: string;
  url: string;
  headers: string;
  startup: string;
  tool: string;
  network: boolean;
  enabledTools: string;
  disabledTools: string;
  secretKind: "env" | "header" | "bearer";
  secretName: string;
  secretValue: string;
}

const EMPTY_FORM: ServerForm = {
  scope: "project",
  name: "",
  transport: "stdio",
  command: "",
  args: "",
  cwd: "",
  env: "",
  url: "",
  headers: "",
  startup: "",
  tool: "",
  network: false,
  enabledTools: "",
  disabledTools: "",
  secretKind: "env",
  secretName: "",
  secretValue: "",
};

function lines(text: string): string[] {
  return text
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);
}

function pairs(text: string, separator: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const line of lines(text)) {
    const at = line.indexOf(separator);
    if (at <= 0) continue;
    out[line.slice(0, at).trim()] = line.slice(at + separator.length).trim();
  }
  return out;
}

function formatPairs(source: unknown, separator: string): string {
  if (!source || typeof source !== "object") return "";
  return Object.entries(source as Record<string, string>)
    .map(([k, v]) => `${k}${separator}${v}`)
    .join("\n");
}

function list(text: string): string[] | null {
  const items = text
    .split(/[,\s]+/)
    .map((item) => item.trim())
    .filter(Boolean);
  return items.length ? items : null;
}

function asString(source: Record<string, unknown>, key: string): string {
  const value = source[key];
  return typeof value === "string" ? value : "";
}

function asNumber(source: Record<string, unknown>, key: string): string {
  const value = source[key];
  return typeof value === "number" ? String(value) : "";
}

function formFrom(server: McpServerView): ServerForm {
  const cfg = server.config;
  const secretEnv = (cfg.secret_env ?? {}) as Record<string, string>;
  const secretHeaders = (cfg.secret_headers ?? {}) as Record<string, string>;
  const bearer = typeof cfg.bearer_credential === "string";
  return {
    ...EMPTY_FORM,
    scope: server.scope,
    name: server.name,
    transport: cfg.transport === "http" ? "http" : "stdio",
    command: asString(cfg, "command"),
    args: Array.isArray(cfg.args) ? (cfg.args as string[]).join("\n") : "",
    cwd: asString(cfg, "cwd"),
    env: formatPairs(cfg.env, "="),
    url: asString(cfg, "url"),
    headers: formatPairs(cfg.http_headers, ": "),
    startup: asNumber(cfg, "startup_timeout_sec"),
    tool: asNumber(cfg, "tool_timeout_sec"),
    network: cfg.network_enabled === true,
    enabledTools: Array.isArray(cfg.enabled_tools) ? (cfg.enabled_tools as string[]).join(", ") : "",
    disabledTools: Array.isArray(cfg.disabled_tools)
      ? (cfg.disabled_tools as string[]).join(", ")
      : "",
    secretKind: bearer ? "bearer" : Object.keys(secretHeaders).length ? "header" : "env",
    secretName: bearer
      ? ""
      : (Object.keys(secretHeaders)[0] ?? Object.keys(secretEnv)[0] ?? ""),
  };
}

/** The stored configuration, built from the form over whatever it already had. */
function configFrom(form: ServerForm, base: Record<string, unknown>): Record<string, unknown> {
  const cfg: Record<string, unknown> = { ...base, transport: form.transport };
  const startup = Number(form.startup);
  const tool = Number(form.tool);
  if (startup > 0) cfg.startup_timeout_sec = startup;
  else delete cfg.startup_timeout_sec;
  if (tool > 0) cfg.tool_timeout_sec = tool;
  else delete cfg.tool_timeout_sec;

  const enabledTools = list(form.enabledTools);
  const disabledTools = list(form.disabledTools);
  if (enabledTools) cfg.enabled_tools = enabledTools;
  else delete cfg.enabled_tools;
  if (disabledTools) cfg.disabled_tools = disabledTools;
  else delete cfg.disabled_tools;

  // Fields the other transport owns are removed rather than left behind: a
  // server with both a command and a url is a configuration nobody wrote.
  if (form.transport === "stdio") {
    delete cfg.url;
    delete cfg.http_headers;
    delete cfg.bearer_token_env_var;
    cfg.command = form.command.trim();
    const args = lines(form.args);
    if (args.length) cfg.args = args;
    else delete cfg.args;
    if (form.cwd.trim()) cfg.cwd = form.cwd.trim();
    else delete cfg.cwd;
    const env = pairs(form.env, "=");
    if (Object.keys(env).length) cfg.env = env;
    else delete cfg.env;
    if (form.network) cfg.network_enabled = true;
    else delete cfg.network_enabled;
  } else {
    delete cfg.command;
    delete cfg.args;
    delete cfg.cwd;
    delete cfg.env;
    delete cfg.network_enabled;
    cfg.url = form.url.trim();
    const headers = pairs(form.headers, ":");
    if (Object.keys(headers).length) cfg.http_headers = headers;
    else delete cfg.http_headers;
  }

  // The reference to a stored secret is written here; the value itself goes to
  // the credential endpoint, so the configuration file never holds it.
  if (form.secretValue) {
    delete cfg.secret_env;
    delete cfg.secret_headers;
    delete cfg.bearer_credential;
    if (form.secretKind === "bearer") cfg.bearer_credential = "token";
    else if (form.secretKind === "header") {
      cfg.secret_headers = { [form.secretName.trim() || "Authorization"]: "token" };
    } else cfg.secret_env = { [form.secretName.trim()]: "token" };
  }
  return cfg;
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

  const scopesFor = useCallback(
    (scope: McpScope) => info?.scopes.find((s) => s.scope === scope),
    [info],
  );
  const projectLabel = useMemo(
    () => info?.projects.find((p) => p.root === info.root)?.name ?? null,
    [info],
  );

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

  const field = (
    label: string,
    key: keyof ServerForm,
    props: { placeholder?: string; hint?: string; multiline?: boolean } = {},
  ) => (
    <label className="modal-field">
      <span>{label}</span>
      {props.multiline ? (
        <textarea
          rows={3}
          value={String(form?.[key] ?? "")}
          placeholder={props.placeholder}
          onChange={(e) => setForm((f) => (f ? { ...f, [key]: e.target.value } : f))}
        />
      ) : (
        <input
          type="text"
          value={String(form?.[key] ?? "")}
          placeholder={props.placeholder}
          onChange={(e) => setForm((f) => (f ? { ...f, [key]: e.target.value } : f))}
        />
      )}
      {props.hint ? <small>{props.hint}</small> : null}
    </label>
  );

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

      {form ? (
        <>
          <div className="modal-section">
            <div className="modal-section-title">基本信息</div>
            <label className="modal-field">
              <span>作用域</span>
              <span className="modal-select-wrap">
                <select
                  value={form.scope}
                  onChange={(e) => setForm({ ...form, scope: e.target.value as McpScope })}
                >
                  {(info?.scopes ?? [{ scope: "project" as McpScope, path: "" }]).map((row) => (
                    <option key={row.scope} value={row.scope}>
                      {row.scope === "project"
                        ? `当前项目${projectLabel ? `（${projectLabel}）` : ""}`
                        : row.scope === "personal"
                          ? "个人（所有项目）"
                          : "应用启动配置"}
                    </option>
                  ))}
                </select>
              </span>
              <small>{scopesFor(form.scope)?.path ?? ""}</small>
            </label>
            {field("名称", "name", { placeholder: "filesystem" })}
            <label className="modal-field">
              <span>传输</span>
              <span className="modal-select-wrap">
                <select
                  value={form.transport}
                  onChange={(e) =>
                    setForm({ ...form, transport: e.target.value as "stdio" | "http" })
                  }
                >
                  <option value="stdio">本地命令（stdio）</option>
                  <option value="http">远程地址（Streamable HTTP）</option>
                </select>
              </span>
            </label>
          </div>

          <div className="modal-section">
            <div className="modal-section-title">
              {form.transport === "stdio" ? "启动方式" : "连接方式"}
            </div>
            {form.transport === "stdio" ? (
              <>
                {field("命令", "command", { placeholder: "npx" })}
                {field("参数", "args", { multiline: true, hint: "每行一个" })}
                {field("工作目录", "cwd", {
                  placeholder: "留空即当前目录",
                  hint: "必须在工作区内",
                })}
                {field("环境变量", "env", { multiline: true, hint: "每行 KEY=VALUE" })}
                <label className="modal-check">
                  <input
                    type="checkbox"
                    checked={form.network}
                    onChange={(e) => setForm({ ...form, network: e.target.checked })}
                  />
                  <span>允许访问网络（首次下载包时需要）</span>
                </label>
              </>
            ) : (
              <>
                {field("地址", "url", { placeholder: "https://example.com/mcp" })}
                {field("请求头", "headers", { multiline: true, hint: "每行 KEY: VALUE" })}
              </>
            )}
          </div>

          <div className="modal-section">
            <div className="modal-section-title">工具与超时</div>
            {field("启动超时（秒）", "startup", { placeholder: "10" })}
            {field("工具超时（秒）", "tool", { placeholder: "60" })}
            {field("只允许这些工具", "enabledTools", { placeholder: "留空即全部，* 表示全部" })}
            {field("禁用这些工具", "disabledTools", { placeholder: "逗号或空格分隔" })}
          </div>

          <div className="modal-section">
            <div className="modal-section-title">凭据</div>
            <label className="modal-field">
              <span>凭据类型</span>
              <span className="modal-select-wrap">
                <select
                  value={form.secretKind}
                  onChange={(e) =>
                    setForm({ ...form, secretKind: e.target.value as ServerForm["secretKind"] })
                  }
                >
                  <option value="env">环境变量</option>
                  <option value="header">请求头</option>
                  <option value="bearer">Bearer Token</option>
                </select>
              </span>
            </label>
            {form.secretKind !== "bearer"
              ? field(form.secretKind === "env" ? "变量名" : "请求头名", "secretName", {
                  placeholder: form.secretKind === "env" ? "GITHUB_TOKEN" : "Authorization",
                })
              : null}
            {field("凭据值", "secretValue", {
              placeholder: form.secretKind === "bearer" ? "粘贴 Token" : "粘贴密钥",
              hint: "保存后不再回显；留空表示不修改",
            })}
          </div>
        </>
      ) : null}

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
