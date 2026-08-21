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
  | { type: "approval_required"; approval_id: string; tool_call: ToolCall }
  | { type: "review"; content?: string };

export interface SessionSummary {
  id: string;
  title: string;
  created_at: string;
  model_alias: string;
  permission_mode?: string;
  root?: string | null;
  secondary_roots?: string[];
}

export type PermissionMode = "ask" | "auto-review" | "allow-all";

export interface SessionDetail extends SessionSummary {
  messages: any[];
}

export type ModelEntry = string | { model: string; key_id?: string };

export interface ModelsInfo {
  default: string;
  models: Record<string, ModelEntry>;
}

export interface WorkspaceProject {
  root: string | null;
  secondary: string[];
}

export interface WorkspacesInfo {
  default: string;
  projects: WorkspaceProject[];
}

export interface ChatOptions {
  root?: string;
  secondary_roots?: string[];
  permission_mode?: string;
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
}

async function json<T>(resp: Response): Promise<T> {
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  return (await resp.json()) as T;
}

export function fetchSessions(): Promise<SessionSummary[]> {
  return fetch("/api/sessions").then((r) => json<SessionSummary[]>(r));
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
): Promise<{ root: string | null; secondary: string[]; projects: WorkspaceProject[] }> {
  return fetch("/api/workspaces/projects", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ root, secondary, session_id: sessionId ?? null }),
  }).then((r) => json<{ root: string | null; secondary: string[]; projects: WorkspaceProject[] }>(r));
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

export function deleteModel(alias: string): Promise<ModelsInfo> {
  return fetch(`/api/models/${encodeURIComponent(alias)}`, {
    method: "DELETE",
  }).then((r) => json<ModelsInfo>(r));
}

export function submitApproval(approvalId: string, approve: boolean): Promise<void> {
  return fetch(`/api/approval/${encodeURIComponent(approvalId)}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ approve }),
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

export interface RollbackResult {
  ok: boolean;
  undo_available?: boolean;
  redo_available?: boolean;
  restored?: string[];
  message_only?: boolean;
}

export function cancelSessionChat(sessionId: string): Promise<{ ok: boolean; cancelled: boolean }> {
  return fetch(`/api/sessions/${encodeURIComponent(sessionId)}/cancel`, {
    method: "POST",
  }).then((r) => json<{ ok: boolean; cancelled: boolean }>(r));
}

export function undoSession(sessionId: string, untilUser?: number): Promise<RollbackResult> {
  const body = untilUser ? JSON.stringify({ until_user: untilUser }) : undefined;
  return fetch(`/api/sessions/${encodeURIComponent(sessionId)}/undo`, {
    method: "POST",
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body,
  }).then((r) => json<RollbackResult>(r));
}

export function redoSession(sessionId: string): Promise<RollbackResult> {
  return fetch(`/api/sessions/${encodeURIComponent(sessionId)}/redo`, { method: "POST" }).then((r) =>
    json<RollbackResult>(r),
  );
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
  });
  if (!resp.ok) throw new Error(`chat HTTP ${resp.status}`);
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