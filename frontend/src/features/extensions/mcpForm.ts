import type { McpScope, McpServerView } from "../../api";

export interface ServerForm {
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

export const EMPTY_FORM: ServerForm = {
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

/** One field of a stored server config, as a displayable string. */
export function asString(source: Record<string, unknown>, key: string): string {
  const value = source[key];
  return typeof value === "string" ? value : "";
}

function asNumber(source: Record<string, unknown>, key: string): string {
  const value = source[key];
  return typeof value === "number" ? String(value) : "";
}

/** One stored server, opened in the form: what to put in each field. */
export function formFrom(server: McpServerView): ServerForm {
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
export function configFrom(form: ServerForm, base: Record<string, unknown>): Record<string, unknown> {
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

