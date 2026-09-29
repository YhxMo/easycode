// Shared front-end test utilities: the ./api mock surface plus small builders
// reused by the App-level test files.
import { vi } from "vitest";
import { act, screen } from "@testing-library/react";
import type {
  ChatEvent,
  ChatOptions,
  CommandInfo,
  CommandsInfo,
  SessionDetail,
  SessionSummary,
  WorkspaceProject,
} from "../api";
import type { HistoryMessage } from "../features/chat/history";
import type { Item } from "../types";

export const apiMock = {
  fetchSessions: vi.fn(),
  fetchArchivedSessions: vi.fn(),
  fetchSession: vi.fn(),
  fetchFiles: vi.fn(),
  fetchWorkspaces: vi.fn(),
  fetchModels: vi.fn(),
  fetchCommands: vi.fn(),
  fetchFileContent: vi.fn(),
  fetchGitChanges: vi.fn(),
  fetchGitFileDiff: vi.fn(),
  setSessionPermission: vi.fn(),
  streamChat: vi.fn(),
  submitApproval: vi.fn(),
  cancelSessionChat: vi.fn(),
  createSession: vi.fn(),
  deleteSession: vi.fn(),
  archiveProjectChats: vi.fn(),
  createWorktree: vi.fn(),
  pinProject: vi.fn(),
  pinSession: vi.fn(),
  removeProject: vi.fn(),
  revealInFinder: vi.fn(),
  saveProject: vi.fn(),
  setSessionArchived: vi.fn(),
  setSessionWorkspace: vi.fn(),
  chooseWorkspace: vi.fn(),
  switchModel: vi.fn(),
  addModel: vi.fn(),
  deleteModel: vi.fn(),
  fetchModel: vi.fn(),
  updateModel: vi.fn(),
  fetchSkills: vi.fn(),
  importSkill: vi.fn(),
  fetchMcp: vi.fn(),
  fetchMcpStatus: vi.fn(),
  saveMcpServer: vi.fn(),
  removeMcpServer: vi.fn(),
  saveMcpCredential: vi.fn(),
  removeMcpCredential: vi.fn(),
};

/** Baseline mock behaviour shared by the App-level suites: clears the mock and
 *  stubs the calls every render makes. Tests override what they need (sessions,
 *  models, streams) afterwards. */
export function primeApiMock(m: typeof apiMock) {
  vi.clearAllMocks();
  m.fetchArchivedSessions.mockResolvedValue([]);
  m.fetchWorkspaces.mockResolvedValue({ projects: [] });
  m.fetchCommands.mockResolvedValue(commandsInfo());
  m.fetchFiles.mockResolvedValue({ files: [], total: 0 });
  m.fetchGitChanges.mockResolvedValue({ repos: [], files: [], truncated: false });
  m.fetchFileContent.mockResolvedValue({
    path: "",
    text: "",
    start_line: 1,
    total_lines: 0,
    truncated: false,
  });
  m.streamChat.mockResolvedValue(undefined);
  m.createSession.mockResolvedValue(session("new-1", "新会话"));
  m.deleteSession.mockResolvedValue({ deleted: true });
  m.setSessionWorkspace.mockImplementation(async (id: string, root: string | null) => ({
    session: { ...session(id, "新会话"), root },
    projects: [],
  }));
  m.setSessionPermission.mockImplementation(async (id: string, mode: string) => ({
    id,
    permission_mode: mode,
  }));
  m.submitApproval.mockResolvedValue(undefined);
}

/** The `/` menu's answer: an entry list, and no warnings about it. */
export function commandsInfo(commands: CommandInfo[] = []): CommandsInfo {
  return { commands, errors: [] };
}

export function session(id: string, title: string): SessionSummary {
  return { id, title, created_at: "2026-01-01T00:00:00Z", model_alias: "m", permission_mode: "ask" };
}

export function detail(id: string, title: string, messages: HistoryMessage[]): SessionDetail {
  return {
    id,
    title,
    created_at: "2026-01-01T00:00:00Z",
    model_alias: "m",
    permission_mode: "ask",
    messages,
  };
}

/** A conversation with one finished turn, ready to be edited. */
export function editable(id: string, title: string, prompt = "原来的问题") {
  return {
    ...detail(id, title, [
      { role: "user", content: prompt, turn_id: `${id}-t1` },
      { role: "assistant", content: "原来的回答", turn_id: `${id}-t1` },
    ]),
    turns: [{ id: `${id}-t1`, status: "completed" as const }],
    revision: 1,
  };
}

export function project(root: string, extra: Partial<WorkspaceProject> = {}): WorkspaceProject {
  return { root, secondary: [], ...extra };
}

type StreamCallback = (e: ChatEvent) => void;

/**
 * A streamChat mock the test drives: it keeps the latest stream's callback and
 * options, and every stream stays open until `finish()`.
 */
export function controllableStream() {
  let onEvent: StreamCallback | undefined;
  let options: ChatOptions | undefined;
  let resolve!: () => void;
  const done = new Promise<void>((res) => {
    resolve = res;
  });
  apiMock.streamChat.mockImplementation(
    (_sid: unknown, _msg: string, cb: StreamCallback, opts?: ChatOptions) => {
      onEvent = cb;
      options = opts;
      return done;
    },
  );
  return {
    get: () => onEvent,
    emit: (e: ChatEvent) => act(async () => onEvent?.(e)),
    options: () => options,
    /** Resolve the in-flight stream so send()'s finally block runs. */
    finish: () => act(async () => resolve()),
  };
}

/** A streamChat mock recording every stream; each stays open until its `resolve`. */
export function captureStreams() {
  const calls: Array<{
    onEvent: StreamCallback;
    resolve: () => void;
    /** True once the caller aborted the request this call represents. */
    aborted: () => boolean;
  }> = [];
  apiMock.streamChat.mockImplementation(
    (_sid: unknown, _msg: string, cb: StreamCallback, opts?: ChatOptions) => {
      let resolve!: () => void;
      const done = new Promise<void>((res) => {
        resolve = res;
      });
      calls.push({ onEvent: cb, resolve, aborted: () => opts?.signal?.aborted === true });
      return done;
    },
  );
  return calls;
}

/** A tool step in the chat stream; it counts as finished once it has a result. */
export function tool(
  id: string,
  name: string,
  args: Record<string, unknown>,
  result?: string,
  done = result !== undefined,
): Item {
  return { kind: "tool", id, name, args, result, done };
}

/** A controllable promise whose resolve/reject are owned by the test. */
export function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

/**
 * The control that opens a conversation from the sidebar — the tab strip shows
 * the same title. Acting on a row (pin, delete) goes through `sessionRow`.
 */
export function sidebarRow(title: string): HTMLElement {
  const row = sessionRow(title);
  const open = row.querySelector<HTMLElement>(".session-open");
  if (!open) throw new Error(`会话行没有打开按钮: ${title}`);
  return open;
}

/** The whole row, for the actions that live beside the open button. */
export function sessionRow(title: string): HTMLElement {
  const row = [...document.querySelectorAll<HTMLElement>(".session-item")].find(
    (el) => el.querySelector(".session-title")?.textContent === title,
  );
  if (!row) throw new Error(`没有找到侧栏会话行: ${title}`);
  return row;
}

/** The conversation currently on screen, as named by its active tab. */
export function activeTitle(): string {
  return document.querySelector(".tab.active .tab-title")?.textContent ?? "";
}

/** Real session tabs: the start page's own tab has no close button. */
export function tabTitles(): string[] {
  return [...document.querySelectorAll(".tab-strip .tab")]
    .filter((t) => t.querySelector(".tab-close"))
    .map((t) => t.querySelector(".tab-title")?.textContent ?? "");
}

/** The composer's field: it carries the combobox role while a menu may open. */
export function composerField(): HTMLTextAreaElement {
  return screen.getByRole("combobox") as HTMLTextAreaElement;
}
