import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import App from "../App";
import * as api from "../api";

// Regression test: the Undo button relies on the async `busy`
// state to disable itself on the next render, leaving a microtask window where
// two consecutive clicks can both reach rollback() and issue a duplicate
// undoSession. A synchronous ref guard must drop the second invocation.
//
// Purely against a mocked ./api (easycode-audit rule 3: no network, no real
// ~/.easycode). The two clicks are dispatched in a single `act` block so React
// batches the state update — the button is not re-rendered (disabled) between
// them, exactly reproducing the double-click race.
vi.mock("../api", () => ({
  fetchSessions: vi.fn(),
  fetchArchivedSessions: vi.fn(),
  fetchSession: vi.fn(),
  fetchWorkspaces: vi.fn(),
  fetchModels: vi.fn(),
  fetchCommands: vi.fn(),
  setSessionPermission: vi.fn(),
  streamChat: vi.fn(),
  undoSession: vi.fn(),
  redoSession: vi.fn(),
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
}));

const m = vi.mocked(api);

function session(id: string, title: string): api.SessionSummary {
  return { id, title, created_at: "2026-01-01T00:00:00Z", model_alias: "m", permission_mode: "ask" };
}

function detail(id: string, title: string, messages: unknown[]): api.SessionDetail {
  return {
    id,
    title,
    created_at: "2026-01-01T00:00:00Z",
    model_alias: "m",
    permission_mode: "ask",
    messages,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  m.fetchSessions.mockResolvedValue([session("s1", "会话A")]);
  m.fetchArchivedSessions.mockResolvedValue([]);
  m.fetchWorkspaces.mockResolvedValue({ default: "", projects: [] });
  m.fetchModels.mockResolvedValue({ default: "deepseek-v4flash", models: {} });
  m.fetchCommands.mockResolvedValue({ commands: [] });
  m.fetchSession.mockResolvedValue(
    detail("s1", "会话A", [
      { role: "user", content: "问题1" },
      { role: "assistant", content: "答复1" },
    ]),
  );
  m.streamChat.mockResolvedValue(undefined);
  m.setSessionPermission.mockImplementation(async (_id, mode) => ({ id: "s1", permission_mode: mode }));
  m.undoSession.mockResolvedValue({ ok: true, restored: [] });
  m.redoSession.mockResolvedValue({ ok: true, restored: [] });
  m.submitApproval.mockResolvedValue(undefined);
});

describe("App · rollback 防抖", () => {
  it("连续两次撤销点击，undoSession 仅被调用 1 次", async () => {
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText("会话A");
    await user.click(screen.getByText("会话A"));
    await screen.findByText("问题1");

    const undoBtn = screen.getByRole("button", { name: /撤销/ });
    // Two synchronous clicks inside one act: React batches the `busy` update, so
    // the button is NOT disabled between them — the ref guard must do the work.
    act(() => {
      undoBtn.dispatchEvent(new MouseEvent("click", { bubbles: true }));
      undoBtn.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });

    await waitFor(() => expect(m.undoSession).toHaveBeenCalledTimes(1));
    // The duplicate was dropped, not re-driven.
    expect(m.redoSession).not.toHaveBeenCalled();
  });

  it("单独点击撤销仍正常工作（未误伤单次回滚）", async () => {
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText("会话A");
    await user.click(screen.getByText("会话A"));
    await screen.findByText("问题1");

    await user.click(screen.getByRole("button", { name: /撤销/ }));

    await waitFor(() => expect(m.undoSession).toHaveBeenCalledTimes(1));
  });
});
