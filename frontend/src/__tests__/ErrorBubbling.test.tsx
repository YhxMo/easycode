import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import App from "../App";
import * as api from "../api";

// Finder-choose failures used to be reported twice: a local `.picker-error`
// (easy to miss when the sidebar is collapsed or a modal covers it) plus the
// App-level toast. DEC-F1 keeps a single error channel: the global toast via
// the `onError` prop.
//
// Test drives App purely against a mocked ./api (easycode-audit rule 3: no
// network, no real ~/.easycode).
vi.mock("../api", () => ({
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
}));

const m = vi.mocked(api);

beforeEach(() => {
  vi.clearAllMocks();
  m.fetchSessions.mockResolvedValue([]);
  m.fetchArchivedSessions.mockResolvedValue([]);
  m.fetchWorkspaces.mockResolvedValue({ projects: [] });
  m.fetchModels.mockResolvedValue({ default: "deepseek-v4flash", models: {}, providers: {}, limits: {} });
  m.fetchCommands.mockResolvedValue({ commands: [] });
  m.streamChat.mockResolvedValue(undefined);
  m.setSessionPermission.mockImplementation(async (_id, mode) => ({ id: "s1", permission_mode: mode }));
  m.submitApproval.mockResolvedValue(undefined);
});

describe("App · 访达选择失败冒泡", () => {
  it("chooseWorkspace 拒绝后，App 级 toast 出现错误文案", async () => {
    const user = userEvent.setup();
    m.chooseWorkspace.mockRejectedValue(new Error("访达引擎不可用"));

    render(<App />);
    // Foreground is a blank new session (currentId === null), so the sidebar
    // shows the ProjectPicker with its "用访达添加主目录" button.
    await screen.findByRole("button", { name: "用访达添加主目录" });

    await user.click(screen.getByRole("button", { name: "用访达添加主目录" }));

    // The error must bubble through ProjectPicker.onError to the app toast
    // instead of living only in the local .picker-error.
    await waitFor(() => {
      const toast = document.querySelector(".app-toast");
      expect(toast?.textContent).toContain("访达引擎不可用");
    });
    // DEC-F1: no local duplicate — the error lives only in the global toast.
    expect(document.querySelector(".picker-error")).toBeNull();
  });
});
