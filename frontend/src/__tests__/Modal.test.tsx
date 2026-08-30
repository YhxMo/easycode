import { act, fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import App from "../App";
import * as api from "../api";
import { Modal } from "../components/Modal";

// The three inline project/session modals previously used a
// bare `.modal-backdrop` div with no WAI-ARIA Dialog semantics, no Escape
// handling and no focus trap. The shared `Modal` component must provide:
//   role="dialog", aria-modal="true", aria-label
//   Escape closes (window keydown listener)
//   a Tab focus trap bounded to the card's focusable controls
//   focus moves in on open and is restored to the trigger on close
//
// All tests run against a mocked ./api (easycode-audit rule 3: no network, no
// real ~/.easycode).

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

describe("Modal 组件", () => {
  it("打开态：渲染 role=dialog / aria-modal / aria-label", () => {
    render(
      <Modal open onClose={vi.fn()} title="删除会话">
        <p>正文</p>
      </Modal>,
    );
    const dlg = screen.getByRole("dialog");
    expect(dlg.getAttribute("aria-modal")).toBe("true");
    expect(dlg.getAttribute("aria-label")).toBe("删除会话");
    expect(screen.getByText("正文")).toBeTruthy();
  });

  it("Escape 触发 onClose（window keydown 监听，焦点不在窗口内也生效）", () => {
    const onClose = vi.fn();
    render(
      <Modal open onClose={onClose} title="T">
        <button type="button">取消</button>
      </Modal>,
    );
    fireEvent.keyDown(window, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("焦点圈禁：Tab 从最后一个控件折返到第一个，Shift+Tab 从第一个折返到最后一个", () => {
    render(
      <Modal open onClose={vi.fn()} title="T">
        <button type="button">甲</button>
        <button type="button">乙</button>
        <button type="button">丙</button>
      </Modal>,
    );
    const buttons = screen.getAllByRole("button");
    const [a, b, c] = buttons;
    expect(a).toBeTruthy();
    expect(b).toBeTruthy();
    expect(c).toBeTruthy();

    // Focus the last control, then Tab wraps to the first.
    c.focus();
    fireEvent.keyDown(window, { key: "Tab" });
    expect(document.activeElement).toBe(a);

    // Focus the first control, then Shift+Tab wraps to the last.
    a.focus();
    fireEvent.keyDown(window, { key: "Tab", shiftKey: true });
    expect(document.activeElement).toBe(c);
  });

});

describe("App · 删除会话模态框", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    m.fetchSessions.mockResolvedValue([session("s1", "会话A")]);
    m.fetchArchivedSessions.mockResolvedValue([]);
    m.fetchWorkspaces.mockResolvedValue({ default: "", projects: [] });
    m.fetchModels.mockResolvedValue({ default: "deepseek-v4flash", models: {} });
    m.fetchCommands.mockResolvedValue({ commands: [] });
    m.fetchSession.mockResolvedValue({
      ...session("s1", "会话A"),
      messages: [{ role: "user", content: "hi" }],
    });
    m.streamChat.mockResolvedValue(undefined);
    m.setSessionPermission.mockImplementation(async (_id, mode) => ({ id: "s1", permission_mode: mode }));
    m.undoSession.mockResolvedValue({ ok: true });
    m.redoSession.mockResolvedValue({ ok: true });
    m.submitApproval.mockResolvedValue(undefined);
  });

  it("点击会话 ✕ 打开删除确认弹窗，Escape 后弹窗消失", async () => {
    render(<App />);
    await screen.findByText("会话A");

    // Open the delete-confirm modal by clicking a session's ✕ control.
    const del = document.querySelector(".session-del") as HTMLElement;
    expect(del).toBeTruthy();
    fireEvent.click(del);

    const dlg = await screen.findByRole("dialog");
    expect(dlg.getAttribute("aria-label")).toBe("删除会话");
    expect(dlg.textContent).toContain("将永久删除");

    // Escape closes the dialog (window keydown listener from Modal).
    act(() => {
      fireEvent.keyDown(window, { key: "Escape" });
    });
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.queryByText("将永久删除")).toBeNull();
  });
});
