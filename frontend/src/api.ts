import type { HistoryMessage } from "./features/chat/history";
import type { ApprovalState, TodoItem } from "./types";

export interface ToolCall {
  id: string;
  name: string;
  arguments: Record<string, unknown>;
}

export type ChatEvent =
  | { type: "session"; session_id?: string }
  | {
      /**
       * The server accepted a turn. The conversation it carries is the
       * authoritative one, so an accepted edit replaces the view's own list
       * instead of appending to it.
       */
      type: "turn_accepted";
      session_id?: string;
      turn_id?: string;
      replaced_turn_id?: string | null;
      revision?: number;
      messages?: HistoryMessage[];
      turns?: TurnSummary[];
      failures?: TurnFailure[];
      todos?: TodoItem[];
      artifacts?: ArtifactRecord[];
      approvals?: ApprovalRecord[];
    }
  | { type: "text"; content?: string }
  | { type: "tool_start"; tool_call: ToolCall }
  | { type: "tool_result"; tool_call: ToolCall; result?: string }
  | { type: "error"; error?: string; code?: string }
  | { type: "done" }
  | { type: "cancelled" }
  | { type: "approval_required"; approval_id: string; tool_call: ToolCall; reason?: string; scope?: string }
  | { type: "review"; content?: string }
  | { type: "todo"; todos?: TodoItem[] };

export interface SessionSummary {
  id: string;
  title: string;
  created_at: string;
  model_alias: string;
  permission_mode?: string;
  root?: string | null;
  secondary_roots?: string[];
  pinned?: boolean;
  pinned_at?: string | null;
  /** False until a turn has begun: what the blank-tab close relies on. */
  started?: boolean;
}

export type ApprovalDecision = Exclude<ApprovalState, "pending">;

export interface ApprovalRecord {
  tool_call_id: string;
  name: string;
  args: Record<string, unknown>;
  reason?: string;
  scope?: string;
  decision: ApprovalDecision;
}

/** A terminal error the server produced, tied to the turn it ended. */
export interface TurnFailure {
  /** The turn whose turn this ended; a reload uses it to place the error. */
  turn_id?: string;
  message: string;
  code?: string;
}

/** One recorded user turn, as the client needs to address it. */
export interface TurnSummary {
  id: string;
  created_at?: string;
  /** ``running`` while a turn holds the session; terminal once it is over. */
  status: "running" | "completed" | "failed" | "cancelled";
  command_id?: string;
}

/** One persisted file-tool result, in the shape the pane already reads. */
export interface ArtifactRecord {
  id: string;
  name: string;
  args: Record<string, unknown>;
  result?: string;
  /** The turn that produced it, so an edit can drop the replaced branch's records. */
  turn_id?: string;
}

export interface SessionDetail extends SessionSummary {
  messages: HistoryMessage[];
  approvals?: ApprovalRecord[];
  /** Complete turns, oldest first; the ids an edit names. */
  turns?: TurnSummary[];
  /** Incremented whenever a turn is accepted; an edit states the one it read. */
  revision?: number;
  busy?: boolean;
  turn_failures?: TurnFailure[];
  todos?: TodoItem[];
  /** File-tool records of the whole conversation; see lib/pane. */
  artifacts?: ArtifactRecord[];
}

/** One file with uncommitted changes in a repository this session runs in. */
export interface GitFileState {
  /** Path relative to the repository. */
  path: string;
  /** Path it was renamed or copied from, "" when it is not one. */
  origin?: string;
  absolute_path: string;
  /** Repository top level the entry came from. */
  repo: string;
  untracked: boolean;
  /** Index (staged) status letter from `git status`, "" when clean. */
  staged: string;
  /** Worktree (unstaged) status letter, "" when clean. */
  unstaged: string;
  /** False for a deletion: there is nothing to preview. */
  exists: boolean;
  /** Lines added against the repository's HEAD (index + worktree). */
  added?: number;
  removed?: number;
  /** Git calls the file binary: its contents are not lines. */
  binary?: boolean;
  /** Diff against HEAD; absent when there is nothing usable to show. */
  diff?: string | null;
  /** Why the row carries no diff text (binary, deleted, oversized). */
  diff_note?: string | null;
}

export interface GitChanges {
  repos: { root: string; name: string }[];
  files: GitFileState[];
  /** Line totals over every reported file. */
  added?: number;
  removed?: number;
  truncated: boolean;
  error?: string | null;
}

export type ModelEntry = { model: string; key_id?: string };

export interface ModelLimits {
  context: number;
}

export interface ModelsInfo {
  default: string;
  models: Record<string, ModelEntry>;
  providers: Record<string, string>;
  limits: Record<string, ModelLimits | null>;
}

export interface WorkspaceProject {
  root: string | null;
  secondary: string[];
  name?: string;
  pinned?: boolean;
}

export interface WorkspacesInfo {
  /** Absolute path of the default workspace; undefined until the list loads. */
  default?: string;
  projects: WorkspaceProject[];
}

export interface ChatOptions {
  root?: string;
  secondary_roots?: string[];
  permission_mode?: string;
  /** Id of the `/` menu entry this message picked, when it came from the menu. */
  command_id?: string;
  /**
   * Editing: the recorded turn this message replaces. The turn and everything
   * after it leave the conversation; the workspace is not rolled back.
   */
  edit_turn_id?: string;
  /** The revision the conversation was read at; required with ``edit_turn_id``. */
  expected_revision?: number;
  /** Abort the in-flight chat stream (e.g. on session switch / explicit stop). */
  signal?: AbortSignal;
}

export interface CommandInfo {
  /** Stable id of this entry, sent back with the message that picks it. */
  id: string;
  name: string;
  description: string;
  /** `mcp` entries ask for one MCP service, and are per project. */
  kind: "builtin" | "template" | "skill" | "mcp";
  argument_hint?: string;
  source?: "builtin" | "project" | "user" | "app";
  /** Where this entry came from, for display: the personal directory, or a project. */
  source_label?: string;
  /** MCP entries only: the service this asks for. */
  mcp_server?: string;
  /** MCP entries only: the project whose service list it came from. */
  project_root?: string;
}

export interface CommandsInfo {
  commands: CommandInfo[];
  /** Names the menu cannot offer: reserved-prefix clashes, unreadable MCP config. */
  errors: string[];
}

/**
 * Everything the `/` menu offers.
 *
 * Skills and templates come from all registered projects plus the personal
 * directory, each entry naming its own source; the same name can appear more
 * than once, so a request that picks from the menu sends the entry's id.
 * MCP services are per project, so the conversation (or the draft's chosen
 * directory) decides which of them are listed.
 */
export function fetchCommands(
  sessionId: string | null,
  root: string | null,
): Promise<CommandsInfo> {
  const query = new URLSearchParams();
  if (sessionId) query.set("session_id", sessionId);
  else if (root) query.set("root", root);
  const suffix = query.toString();
  return request(`/api/commands${suffix ? `?${suffix}` : ""}`);
}

export interface AddModelBody {
  alias: string;
  model: string;
  provider?: string;
  base_url?: string;
  api_key?: string;
  api_format?: string;
}

export interface EditableModel {
  alias: string;
  model: string;
  provider?: string | null;
  base_url?: string | null;
  has_api_key: boolean;
  key_tail: string;
  api_format?: string | null;
}

export interface UpdateModelBody {
  model: string;
  new_alias?: string;
  provider?: string;
  base_url?: string;
  api_key?: string;
  clear_key?: boolean;
  api_format?: string;
}

/** Backend ``detail`` message for a failed response, else the fallback. */
async function errorDetail(resp: Response, fallback: string): Promise<string> {
  try {
    const body = (await resp.json()) as { detail?: string };
    if (body.detail) return String(body.detail);
  } catch {
    // Keep the fallback when the server did not return JSON.
  }
  return fallback;
}

/** Fetch + JSON decode + backend-detail errors for one endpoint. */
async function request<T>(url: string, init: { method?: string; body?: unknown } = {}): Promise<T> {
  const resp = await fetch(url, {
    method: init.method ?? "GET",
    ...(init.body === undefined
      ? {}
      : { headers: { "Content-Type": "application/json" }, body: JSON.stringify(init.body) }),
  });
  if (!resp.ok) throw new Error(await errorDetail(resp, `HTTP ${resp.status}`));
  return (await resp.json()) as T;
}

export interface FileContent {
  path: string;
  text: string;
  start_line: number;
  total_lines: number;
  truncated: boolean;
}

/** One file's text, for the pane's preview. */
export function fetchFileContent(
  sessionId: string,
  path: string,
  offset = 1,
  limit = 0,
): Promise<FileContent> {
  const query = new URLSearchParams({ session_id: sessionId, path, offset: String(offset) });
  if (limit > 0) query.set("limit", String(limit));
  return request(`/api/files/content?${query}`);
}

export function fetchSessions(): Promise<SessionSummary[]> {
  return request("/api/sessions");
}

export function fetchArchivedSessions(): Promise<SessionSummary[]> {
  return request("/api/sessions?archived=1");
}

export function fetchSession(id: string): Promise<SessionDetail> {
  return request(`/api/sessions/${id}`);
}

/**
 * Live uncommitted changes of the repositories this session works in.
 *
 * ``diffs: false`` asks for the files and their counts only: the section's
 * summary needs nothing else, and generating a diff per changed file is the
 * expensive half of this request.
 */
export function fetchGitChanges(sessionId: string, diffs = false): Promise<GitChanges> {
  const query = diffs ? "?diffs=1" : "?diffs=0";
  return request(`/api/sessions/${encodeURIComponent(sessionId)}/changes${query}`);
}

/** One file's diff, for a row the reader opened. */
export function fetchGitFileDiff(
  sessionId: string,
  repo: string,
  path: string,
): Promise<{ diff: string | null; diff_note: string | null }> {
  const query = `repo=${encodeURIComponent(repo)}&path=${encodeURIComponent(path)}`;
  return request(`/api/sessions/${encodeURIComponent(sessionId)}/changes/diff?${query}`);
}

/**
 * Create an empty session up front, so "新会话" opens a real conversation
 * instead of a not-yet-existing one. Omitting ``secondaryRoots`` inherits the
 * project's own binding, exactly as a chat-created session would.
 */
export function createSession(
  root: string | null,
  permissionMode?: string,
  secondaryRoots?: string[],
): Promise<SessionSummary> {
  const body: Record<string, unknown> = { root };
  if (permissionMode) {
    body.permission_mode = permissionMode;
    // The current conversation already carries this consent; the server still
    // refuses an unconfirmed full-access session, so it is stated here.
    if (permissionMode === "allow-all") body.confirm_full_access = true;
  }
  if (secondaryRoots !== undefined) body.secondary_roots = secondaryRoots;
  return request("/api/sessions", { method: "POST", body });
}

/** Pin or unpin a session; pinned sessions get their own sidebar section. */
export function pinSession(id: string, pinned: boolean): Promise<{ ok: boolean }> {
  return request(`/api/sessions/${id}/pin`, { method: "POST", body: { pinned } });
}

/**
 * Point a session at another project directory. Only a session that has not
 * started a turn may move; the server answers with the updated summary and the
 * project list, and refuses (409) anything that already ran something.
 * Omitting ``secondaryRoots`` inherits the target project's own binding.
 */
export function setSessionWorkspace(
  id: string,
  root: string | null,
  secondaryRoots?: string[],
): Promise<{ session: SessionSummary; projects: WorkspaceProject[] }> {
  const body: Record<string, unknown> = { root };
  if (secondaryRoots !== undefined) body.secondary_roots = secondaryRoots;
  return request(`/api/sessions/${encodeURIComponent(id)}/workspace`, { method: "POST", body });
}

/**
 * Delete a session. ``blankOnly`` asks the server to remove it only while no
 * turn has begun and answers whether it did; a session that started one is
 * left alone, which is what closing an untouched tab has to rule out.
 */
export function deleteSession(id: string, blankOnly = false): Promise<{ deleted: boolean }> {
  const query = blankOnly ? "?blank_only=1" : "";
  return request(`/api/sessions/${id}${query}`, { method: "DELETE" });
}

export function fetchWorkspaces(): Promise<WorkspacesInfo> {
  return request("/api/workspaces");
}

export interface FileEntry {
  /** Path relative to the workspace root it came from, for display. */
  path: string;
  name: string;
  dir: string;
  root: string;
  /**
   * Canonical target file. Display paths collide across roots, so references
   * and previews always use this one.
   */
  absolute_path: string;
}

/** Workspace files offered by the composer's @-mention menu. */
export function fetchFiles(
  sessionId: string | null,
  draft: { root: string | null; secondary: string[] } | undefined,
  q: string,
  limit = 200,
): Promise<{ files: FileEntry[]; total: number }> {
  const body: Record<string, unknown> = { q, limit };
  if (sessionId) body.session_id = sessionId;
  else if (draft) {
    body.root = draft.root;
    body.secondary_roots = draft.secondary;
  }
  return request("/api/files", { method: "POST", body });
}

export function saveProject(
  root: string | null,
  secondary: string[],
  sessionId?: string | null,
  name?: string,
): Promise<{ root: string | null; secondary: string[]; projects: WorkspaceProject[] }> {
  return request("/api/workspaces/projects", {
    method: "POST",
    body: { root, secondary, session_id: sessionId ?? null, name },
  });
}

export function pinProject(root: string | null, pinned: boolean): Promise<{ projects: WorkspaceProject[] }> {
  return request("/api/workspaces/pin", { method: "POST", body: { root, pinned } });
}

export function revealInFinder(root: string | null): Promise<{ ok: boolean; error?: string }> {
  return request("/api/workspaces/reveal", { method: "POST", body: { root } });
}

export function createWorktree(
  root: string,
): Promise<{ name?: string; warnings?: string[]; projects: WorkspaceProject[] }> {
  return request("/api/workspaces/worktree", { method: "POST", body: { root } });
}

export function archiveProjectChats(
  root: string | null,
): Promise<{ archived_sessions: number; projects: WorkspaceProject[] }> {
  return request("/api/workspaces/archive", { method: "POST", body: { root } });
}

export function removeProject(
  root: string | null,
): Promise<{ deleted_sessions: number; projects: WorkspaceProject[] }> {
  return request("/api/workspaces/projects/remove", {
    method: "POST",
    body: { root, delete_sessions: true },
  });
}

export function setSessionArchived(sessionId: string, archived: boolean): Promise<void> {
  return request(`/api/sessions/${encodeURIComponent(sessionId)}/archive`, {
    method: "POST",
    body: { archived },
  });
}

export function chooseWorkspace(
  multiple = false,
  prompt = "选择目录",
): Promise<{ paths: string[]; supported: boolean }> {
  return request("/api/workspaces/choose", { method: "POST", body: { multiple, prompt } });
}

export function fetchModels(): Promise<ModelsInfo> {
  return request("/api/models");
}

export function switchModel(alias: string): Promise<ModelsInfo> {
  return request("/api/models", { method: "POST", body: { alias } });
}

export function addModel(body: AddModelBody): Promise<ModelsInfo> {
  return request("/api/models/add", { method: "POST", body });
}

export function fetchModel(alias: string): Promise<EditableModel> {
  return request(`/api/models/${encodeURIComponent(alias)}`);
}

export function updateModel(alias: string, body: UpdateModelBody): Promise<ModelsInfo> {
  return request(`/api/models/${encodeURIComponent(alias)}`, { method: "PUT", body });
}

export function deleteModel(alias: string): Promise<ModelsInfo> {
  return request(`/api/models/${encodeURIComponent(alias)}`, { method: "DELETE" });
}

export function submitApproval(approvalId: string, approve: boolean, always = false): Promise<void> {
  return request(`/api/approval/${encodeURIComponent(approvalId)}`, {
    method: "POST",
    body: { approve, always },
  });
}

export function setSessionPermission(
  sessionId: string,
  mode: string,
  confirmFullAccess = false,
): Promise<{ permission_mode: string }> {
  return request(`/api/sessions/${encodeURIComponent(sessionId)}/permission`, {
    method: "POST",
    body: { mode, confirm_full_access: confirmFullAccess },
  });
}

export function cancelSessionChat(sessionId: string): Promise<void> {
  return request(`/api/sessions/${encodeURIComponent(sessionId)}/cancel`, { method: "POST" });
}

export async function streamChat(
  sessionId: string | null,
  message: string,
  onEvent: (e: ChatEvent) => void,
  opts: ChatOptions = {},
): Promise<void> {
  const body: Record<string, unknown> = { message, session_id: sessionId };
  if (opts.root) body.root = opts.root;
  // A missing field means "inherit the project binding"; an explicit empty
  // array means "no secondary roots", so the check is presence, not length.
  if (opts.secondary_roots !== undefined) body.secondary_roots = opts.secondary_roots;
  if (opts.permission_mode) {
    body.permission_mode = opts.permission_mode;
    // A conversation that already shows full access has been through the risk
    // dialog, and the server refuses an unconfirmed full-access turn.
    if (opts.permission_mode === "allow-all") body.confirm_full_access = true;
  }
  if (opts.command_id) body.command_id = opts.command_id;
  if (opts.edit_turn_id) {
    body.edit_turn_id = opts.edit_turn_id;
    body.expected_revision = opts.expected_revision;
  }
  const resp = await fetch("/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal: opts.signal,
  });
  if (!resp.ok) {
    throw new Error(await errorDetail(resp, `chat HTTP ${resp.status}`));
  }
  if (!resp.body) throw new Error("no body");
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  // The backend always ends a turn with done/cancelled, or reports an error
  // event. Reaching EOF without any of those means the connection dropped.
  let terminated = false;
  const handleRaw = (raw: string) => {
    for (const line of raw.split("\n")) {
      if (!line.startsWith("data: ")) continue;
      const ev = JSON.parse(line.slice(6)) as ChatEvent;
      if (ev.type === "done" || ev.type === "cancelled" || ev.type === "error") {
        terminated = true;
      }
      onEvent(ev);
    }
  };
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    const parts = buf.split("\n\n");
    buf = parts.pop() ?? "";
    for (const part of parts) handleRaw(part);
  }
  // Flush the decoder and process a trailing event that lost its blank-line
  // separator; a truncated final frame counts as an interruption below.
  buf += decoder.decode();
  if (buf.trim()) {
    try {
      handleRaw(buf);
    } catch {
      // truncated JSON: leave terminated=false so the turn reports an interruption
    }
  }
  if (!terminated) {
    throw new Error("连接中断：本轮未正常结束，已保留已生成内容。");
  }
}

export type McpScope = "personal" | "app" | "project";

export interface McpCredentialView {
  id: string;
  kind: string;
  /** Names the record holds. Values are never sent to the browser. */
  names: string[];
  has_value: boolean;
  expires_at?: string | null;
  updated_at?: string | null;
}

export interface McpServerView {
  name: string;
  /** The scope that won the name; `overrides` lists the scopes it shadows. */
  scope: McpScope;
  overrides: McpScope[];
  enabled: boolean;
  config: Record<string, unknown>;
  credential: McpCredentialView | null;
}

export interface McpScopeFile {
  scope: McpScope;
  root: string | null;
  name?: string;
  path: string;
  exists: boolean;
}

export interface McpInfo {
  root: string | null;
  projects: Array<{ root: string; name: string }>;
  scopes: McpScopeFile[];
  servers: McpServerView[];
  /** Files that could not be read; the panel shows them instead of hiding them. */
  errors: string[];
}

export function fetchMcp(root: string | null): Promise<McpInfo> {
  return request(root ? `/api/mcp?root=${encodeURIComponent(root)}` : "/api/mcp");
}

export function saveMcpServer(
  scope: McpScope,
  root: string | null,
  name: string,
  config: Record<string, unknown>,
): Promise<McpInfo> {
  return request("/api/mcp/servers", {
    method: "POST",
    body: { scope, root, name, config },
  });
}

export function removeMcpServer(
  scope: McpScope,
  root: string | null,
  name: string,
): Promise<McpInfo> {
  return request("/api/mcp/servers/remove", { method: "POST", body: { scope, root, name } });
}

export function saveMcpCredential(
  scope: McpScope,
  root: string | null,
  server: string,
  kind: string,
  values: Record<string, string | null>,
): Promise<McpInfo> {
  return request("/api/mcp/credentials", {
    method: "POST",
    body: { scope, root, server, kind, values },
  });
}

export function removeMcpCredential(
  scope: McpScope,
  root: string | null,
  server: string,
): Promise<McpInfo> {
  return request("/api/mcp/credentials/remove", {
    method: "POST",
    body: { scope, root, server },
  });
}

export interface SkillView {
  name: string;
  description: string;
  /** Which install root it came from; a same-named entry can exist in both. */
  scope: "personal" | "project";
  /** Absolute path of the SKILL.md it was read from. */
  path: string;
  /** The package root its relative resources resolve against. */
  directory: string;
  /** False when a same-named entry in a higher scope would load instead. */
  effective: boolean;
}

export interface SkillsInfo {
  root: string;
  /** ``skills.enabled`` from the configuration; false means calls are off. */
  enabled: boolean;
  /** Where an install would land, per scope. Display only. */
  install_roots: { personal: string; project: string };
  skills: SkillView[];
  /** Packages that could not be read: reported, never silently dropped. */
  errors: string[];
}

export interface ImportSkillBody {
  source_path: string;
  scope?: "personal" | "project";
  root?: string | null;
}

export interface ImportSkillResult {
  imported: SkillView;
  root: string;
  /** Non-empty when the install succeeded but a session could not be refreshed. */
  warnings: string[];
}

export function fetchSkills(root: string | null): Promise<SkillsInfo> {
  return request(root ? `/api/skills?root=${encodeURIComponent(root)}` : "/api/skills");
}

/** Install a local folder as a skill; the server decides where it lands. */
export function importSkill(body: ImportSkillBody): Promise<ImportSkillResult> {
  return request("/api/skills/import", { method: "POST", body });
}

export interface McpStatusServer {
  name: string;
  scope: McpScope;
  state: "pending" | "connected" | "failed" | "disabled";
  error: string | null;
  tools: string[];
}

export function fetchMcpStatus(
  sessionId: string,
): Promise<{ started: boolean; servers: McpStatusServer[] }> {
  return request(`/api/mcp/status?session_id=${encodeURIComponent(sessionId)}`);
}
