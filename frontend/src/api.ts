import type { HistoryMessage } from "./lib/history";
import type { ApprovalState, TodoItem } from "./types";

export interface ToolCall {
  id: string;
  name: string;
  arguments: Record<string, unknown>;
}

export type ChatEvent =
  | { type: "session"; session_id?: string }
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

/** A terminal error the server produced, tied to the user turn it ended. */
export interface TurnFailure {
  /** Timestamp of that turn's user message; a reload uses it to place the error. */
  time?: string;
  message: string;
  code?: string;
}

/** One persisted file-tool result, in the shape the pane already reads. */
export interface ArtifactRecord {
  id: string;
  name: string;
  args: Record<string, unknown>;
  result?: string;
}

export interface SessionDetail extends SessionSummary {
  messages: HistoryMessage[];
  approvals?: ApprovalRecord[];
  user_times?: string[];
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
  /** Abort the in-flight chat stream (e.g. on session switch / explicit stop). */
  signal?: AbortSignal;
}

export interface CommandInfo {
  /** Stable id of this entry, sent back with the message that picks it. */
  id: string;
  name: string;
  description: string;
  kind: "builtin" | "template" | "skill";
  argument_hint?: string;
  source?: "builtin" | "project" | "user";
  /** Where this entry came from, for display: the personal directory, or a project. */
  source_label?: string;
}

/**
 * Every command the `/` menu offers: all registered projects plus the personal
 * directory, each entry naming its own source. The same name can appear more
 * than once, so a request that picks from the menu sends the entry's id.
 */
export function fetchCommands(): Promise<{ commands: CommandInfo[] }> {
  return request("/api/commands");
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

/** Live uncommitted changes of the repositories this session works in. */
export function fetchGitChanges(sessionId: string): Promise<GitChanges> {
  return request(`/api/sessions/${encodeURIComponent(sessionId)}/changes`);
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
  if (permissionMode) body.permission_mode = permissionMode;
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
): Promise<{ permission_mode: string }> {
  return request(`/api/sessions/${encodeURIComponent(sessionId)}/permission`, {
    method: "POST",
    body: { mode },
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
  if (opts.permission_mode) body.permission_mode = opts.permission_mode;
  if (opts.command_id) body.command_id = opts.command_id;
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
