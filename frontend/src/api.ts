import type { HistoryMessage } from "./lib/history";

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
  | { type: "error"; error?: string }
  | { type: "done" }
  | { type: "cancelled" }
  | { type: "approval_required"; approval_id: string; tool_call: ToolCall; reason?: string; scope?: string }
  | { type: "review"; content?: string };

export interface SessionSummary {
  id: string;
  title: string;
  created_at: string;
  model_alias: string;
  permission_mode?: string;
  root?: string | null;
  secondary_roots?: string[];
  archived?: boolean;
}

export type ApprovalDecision = "approved" | "denied" | "expired";

export interface ApprovalRecord {
  tool_call_id: string;
  name: string;
  args: Record<string, unknown>;
  reason?: string;
  scope?: string;
  decision: ApprovalDecision;
  always: boolean;
}

export interface SessionDetail extends SessionSummary {
  messages: HistoryMessage[];
  approvals?: ApprovalRecord[];
  user_times?: string[];
}

export type ModelEntry = { model: string; key_id?: string; api_format: string; provider?: string };

export interface ModelLimits {
  context: number;
}

export interface ModelsInfo {
  default: string;
  models: Record<string, ModelEntry>;
  providers?: Record<string, string>;
  limits?: Record<string, ModelLimits | null>;
}

export interface WorkspaceProject {
  root: string | null;
  secondary: string[];
  name?: string;
  pinned?: boolean;
}

export interface WorkspacesInfo {
  default: string;
  projects: WorkspaceProject[];
}

export interface ChatOptions {
  root?: string;
  secondary_roots?: string[];
  permission_mode?: string;
  /** Abort the in-flight chat stream (e.g. on session switch / explicit stop). */
  signal?: AbortSignal;
}

export interface CommandInfo {
  name: string;
  description: string;
  kind: "builtin" | "template" | "skill";
  argument_hint?: string;
  source?: "builtin" | "project" | "user";
}

export function fetchCommands(): Promise<{ commands: CommandInfo[] }> {
  return fetch("/api/commands").then((r) => json<{ commands: CommandInfo[] }>(r));
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
  key_id?: string | null;
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

async function json<T>(resp: Response): Promise<T> {
  if (!resp.ok) {
    let detail = `HTTP ${resp.status}`;
    try {
      const body = (await resp.json()) as { detail?: string };
      if (body.detail) detail = body.detail;
    } catch {
      // Keep the HTTP status when the server did not return JSON.
    }
    throw new Error(detail);
  }
  return (await resp.json()) as T;
}

export function fetchSessions(): Promise<SessionSummary[]> {
  return fetch("/api/sessions").then((r) => json<SessionSummary[]>(r));
}

export function fetchArchivedSessions(): Promise<SessionSummary[]> {
  return fetch("/api/sessions?archived=1").then((r) => json<SessionSummary[]>(r));
}

export function fetchSession(id: string): Promise<SessionDetail> {
  return fetch(`/api/sessions/${id}`).then((r) => json<SessionDetail>(r));
}

export function deleteSession(id: string): Promise<void> {
  return fetch(`/api/sessions/${id}`, { method: "DELETE" }).then((r) => {
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
  });
}

export function fetchWorkspaces(): Promise<WorkspacesInfo> {
  return fetch("/api/workspaces").then((r) => json<WorkspacesInfo>(r));
}

export function saveProject(
  root: string | null,
  secondary: string[],
  sessionId?: string | null,
  name?: string,
): Promise<{ root: string | null; secondary: string[]; projects: WorkspaceProject[] }> {
  return fetch("/api/workspaces/projects", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ root, secondary, session_id: sessionId ?? null, name }),
  }).then((r) => json<{ root: string | null; secondary: string[]; projects: WorkspaceProject[] }>(r));
}

export function pinProject(
  root: string | null,
  pinned: boolean,
): Promise<{ ok: boolean; root: string | null; pinned: boolean; projects: WorkspaceProject[] }> {
  return fetch("/api/workspaces/pin", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ root, pinned }),
  }).then((r) => json<{ ok: boolean; root: string | null; pinned: boolean; projects: WorkspaceProject[] }>(r));
}

export function revealInFinder(root: string | null): Promise<{ ok: boolean; supported: boolean; path?: string }> {
  return fetch("/api/workspaces/reveal", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ root }),
  }).then((r) => json<{ ok: boolean; supported: boolean; path?: string }>(r));
}

export function createWorktree(
  root: string,
): Promise<{ ok: boolean; root: string; name?: string; notes?: string[]; projects: WorkspaceProject[] }> {
  return fetch("/api/workspaces/worktree", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ root }),
  }).then((r) => json<{ ok: boolean; root: string; name?: string; notes?: string[]; projects: WorkspaceProject[] }>(r));
}

export function archiveProjectChats(
  root: string | null,
): Promise<{ ok: boolean; archived_sessions: number; projects: WorkspaceProject[] }> {
  return fetch("/api/workspaces/archive", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ root }),
  }).then((r) => json<{ ok: boolean; archived_sessions: number; projects: WorkspaceProject[] }>(r));
}

export function removeProject(
  root: string | null,
): Promise<{ ok: boolean; deleted_sessions: number; projects: WorkspaceProject[] }> {
  return fetch("/api/workspaces/projects/remove", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ root, delete_sessions: true }),
  }).then((r) => json<{ ok: boolean; deleted_sessions: number; projects: WorkspaceProject[] }>(r));
}

export function setSessionArchived(
  sessionId: string,
  archived: boolean,
): Promise<{ ok: boolean; archived: boolean }> {
  return fetch(`/api/sessions/${encodeURIComponent(sessionId)}/archive`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ archived }),
  }).then((r) => json<{ ok: boolean; archived: boolean }>(r));
}

export function chooseWorkspace(
  multiple = false,
  prompt = "选择目录",
): Promise<{ paths: string[]; supported: boolean }> {
  return fetch("/api/workspaces/choose", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ multiple, prompt }),
  }).then((r) => json<{ paths: string[]; supported: boolean }>(r));
}

export function fetchModels(): Promise<ModelsInfo> {
  return fetch("/api/models").then((r) => json<ModelsInfo>(r));
}

export function switchModel(alias: string): Promise<ModelsInfo> {
  return fetch("/api/models", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ alias }),
  }).then((r) => json<ModelsInfo>(r));
}

export function addModel(body: AddModelBody): Promise<ModelsInfo> {
  return fetch("/api/models/add", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  }).then((r) => json<ModelsInfo>(r));
}

export function fetchModel(alias: string): Promise<EditableModel> {
  return fetch(`/api/models/${encodeURIComponent(alias)}`).then((r) => json<EditableModel>(r));
}

export function updateModel(alias: string, body: UpdateModelBody): Promise<ModelsInfo> {
  return fetch(`/api/models/${encodeURIComponent(alias)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  }).then((r) => json<ModelsInfo>(r));
}

export function deleteModel(alias: string): Promise<ModelsInfo> {
  return fetch(`/api/models/${encodeURIComponent(alias)}`, {
    method: "DELETE",
  }).then((r) => json<ModelsInfo>(r));
}

export function submitApproval(approvalId: string, approve: boolean, always = false): Promise<void> {
  return fetch(`/api/approval/${encodeURIComponent(approvalId)}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ approve, always }),
  }).then((r) => {
    if (!r.ok) throw new Error(`approval HTTP ${r.status}`);
  });
}

export function setSessionPermission(
  sessionId: string,
  mode: string,
): Promise<{ id: string; permission_mode: string }> {
  return fetch(`/api/sessions/${encodeURIComponent(sessionId)}/permission`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ mode }),
  }).then((r) => json<{ id: string; permission_mode: string }>(r));
}

export function cancelSessionChat(sessionId: string): Promise<{ ok: boolean; cancelled: boolean }> {
  return fetch(`/api/sessions/${encodeURIComponent(sessionId)}/cancel`, {
    method: "POST",
  }).then((r) => json<{ ok: boolean; cancelled: boolean }>(r));
}

export async function streamChat(
  sessionId: string | null,
  message: string,
  onEvent: (e: ChatEvent) => void,
  opts: ChatOptions = {},
): Promise<void> {
  const body: Record<string, unknown> = { message, session_id: sessionId };
  if (opts.root) body.root = opts.root;
  if (opts.secondary_roots?.length) body.secondary_roots = opts.secondary_roots;
  if (opts.permission_mode) body.permission_mode = opts.permission_mode;
  const resp = await fetch("/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal: opts.signal,
  });
  if (!resp.ok) {
    let detail = `chat HTTP ${resp.status}`;
    try {
      const errBody = (await resp.json()) as { detail?: string };
      if (errBody.detail) detail = String(errBody.detail);
    } catch {
      // Keep the HTTP status when the server did not return JSON.
    }
    throw new Error(detail);
  }
  if (!resp.body) throw new Error("no body");
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    const parts = buf.split("\n\n");
    buf = parts.pop() ?? "";
    for (const part of parts) {
      for (const line of part.split("\n")) {
        if (line.startsWith("data: ")) {
          onEvent(JSON.parse(line.slice(6)) as ChatEvent);
        }
      }
    }
  }
}
