// Shared front-end test utilities: the ./api mock surface plus small builders
// reused by the App-level test files.
import { vi } from "vitest";
import type { SessionDetail, SessionSummary } from "../api";
import type { HistoryMessage } from "../lib/history";

export const apiMock = {
  fetchSessions: vi.fn(),
  fetchArchivedSessions: vi.fn(),
  fetchSession: vi.fn(),
  fetchWorkspaces: vi.fn(),
  fetchModels: vi.fn(),
  fetchCommands: vi.fn(),
  setSessionPermission: vi.fn(),
  streamChat: vi.fn(),
  submitApproval: vi.fn(),
  cancelSessionChat: vi.fn(),
  deleteSession: vi.fn(),
  archiveProjectChats: vi.fn(),
  createWorktree: vi.fn(),
  pinProject: vi.fn(),
  removeProject: vi.fn(),
  revealInFinder: vi.fn(),
  saveProject: vi.fn(),
  setSessionArchived: vi.fn(),
  chooseWorkspace: vi.fn(),
  switchModel: vi.fn(),
  addModel: vi.fn(),
  deleteModel: vi.fn(),
  fetchModel: vi.fn(),
  updateModel: vi.fn(),
};

/** Baseline mock behaviour shared by the App-level suites: clears the mock and
 *  stubs the calls every render makes. Tests override what they need (sessions,
 *  models, streams) afterwards. */
export function primeApiMock(m: typeof apiMock) {
  vi.clearAllMocks();
  m.fetchArchivedSessions.mockResolvedValue([]);
  m.fetchWorkspaces.mockResolvedValue({ projects: [] });
  m.fetchCommands.mockResolvedValue({ commands: [] });
  m.streamChat.mockResolvedValue(undefined);
  m.setSessionPermission.mockImplementation(async (id: string, mode: string) => ({
    id,
    permission_mode: mode,
  }));
  m.submitApproval.mockResolvedValue(undefined);
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
