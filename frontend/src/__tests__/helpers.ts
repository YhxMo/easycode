// Shared front-end test utilities: the ./api mock surface plus small builders
// reused by the App-level test files.
import { vi } from "vitest";
import { screen } from "@testing-library/react";
import type { CommandInfo, CommandsInfo, SessionDetail, SessionSummary } from "../api";
import type { HistoryMessage } from "../lib/history";

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

/** The composer's field: it carries the combobox role while a menu may open. */
export function composerField(): HTMLTextAreaElement {
  return screen.getByRole("combobox") as HTMLTextAreaElement;
}
