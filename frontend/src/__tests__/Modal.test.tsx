import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import App from "../App";
import * as api from "../api";
import { session } from "./helpers";
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

vi.mock("../api", async () => (await import("./helpers")).apiMock);

const m = vi.mocked(api);

/** A streamChat mock that stays in-flight and rejects when its signal aborts. */
function abortableStream() {
  let onEvent: ((e: api.ChatEvent) => void) | undefined;
  m.streamChat.mockImplementation((_sid, _msg, cb, opts) => {
    onEvent = cb;
    return new Promise<void>((_resolve, reject) => {
      opts?.signal?.addEventListener("abort", () =>
        reject(new DOMException("aborted", "AbortError")),
      );
    });
  });
  return { get: () => onEvent };
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
    m.fetchWorkspaces.mockResolvedValue({ projects: [] });
    m.fetchModels.mockResolvedValue({ default: "deepseek-v4flash", models: {}, providers: {}, limits: {} });
    m.fetchCommands.mockResolvedValue({ commands: [] });
    m.fetchSession.mockResolvedValue({
      ...session("s1", "会话A"),
      messages: [{ role: "user", content: "hi" }],
    });
    m.streamChat.mockResolvedValue(undefined);
    m.setSessionPermission.mockImplementation(async (_id, mode) => ({ id: "s1", permission_mode: mode }));
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

  it("流进行中删除当前会话：停止流、清空视图并恢复空闲", async () => {
    const user = userEvent.setup();
    m.cancelSessionChat.mockResolvedValue(undefined);
    m.deleteSession.mockResolvedValue(undefined);
    const stream = abortableStream();

    render(<App />);
    await screen.findByText("会话A");
    await user.click(screen.getByText("会话A"));
    await screen.findByText("hi");
    await user.type(screen.getByRole("textbox"), "long task");
    await user.click(screen.getByRole("button", { name: /发送消息/ }));
    await waitFor(() => expect(stream.get()).toBeTruthy());

    fireEvent.click(document.querySelector(".session-del") as HTMLElement);
    await screen.findByRole("dialog");
    await user.click(screen.getByRole("button", { name: "确认删除" }));

    // DELETE owns cancellation (backend stops the turn and waits it out), so
    // the view no longer sends a separate cancel request.
    await waitFor(() => expect(m.deleteSession).toHaveBeenCalledWith("s1"));

    // A late event from the deleted session's stream must not reach the view.
    await act(async () => {
      stream.get()?.({ type: "text", content: "late-text" });
    });
    expect(screen.queryByText("late-text")).toBeNull();

    // The aborted request settles and the app returns to idle.
    await waitFor(() => expect(screen.getByText("已连接")).toBeTruthy());
  });

  it("删除等待期间切换到其他会话，成功回调不清空新视图", async () => {
    const user = userEvent.setup();
    m.fetchSessions.mockResolvedValue([session("s1", "会话A"), session("s2", "会话B")]);
    m.fetchSession.mockImplementation((id: string) =>
      Promise.resolve({
        ...session(id, id === "s1" ? "会话A" : "会话B"),
        messages: [{ role: "user", content: id === "s1" ? "A-内容" : "B-内容" }],
      }),
    );
    m.cancelSessionChat.mockResolvedValue(undefined);
    let resolveDelete!: () => void;
    m.deleteSession.mockReturnValue(
      new Promise<void>((resolve) => {
        resolveDelete = resolve;
      }),
    );

    render(<App />);
    await screen.findByText("会话A");
    await user.click(screen.getByText("会话A"));
    await screen.findByText("A-内容");

    fireEvent.click(document.querySelector(".session-del") as HTMLElement);
    await screen.findByRole("dialog");
    await user.click(screen.getByRole("button", { name: "确认删除" }));

    // Switch to B while A's DELETE is still in flight.
    await user.click(screen.getByText("会话B"));
    await screen.findByText("B-内容");

    await act(async () => {
      resolveDelete();
    });

    // B stays the foreground view: the stale delete must not blank it.
    await waitFor(() =>
      expect(document.querySelector(".chat-context strong")?.textContent).toBe("会话B"),
    );
    expect(screen.getByText("B-内容")).toBeTruthy();
  });

  it("删除失败时保留会话、恢复视图并提示错误", async () => {
    const user = userEvent.setup();
    m.cancelSessionChat.mockResolvedValue(undefined);
    m.deleteSession.mockRejectedValue(new Error("boom"));

    render(<App />);
    await screen.findByText("会话A");
    await user.click(screen.getByText("会话A"));
    await screen.findByText("hi");

    fireEvent.click(document.querySelector(".session-del") as HTMLElement);
    await screen.findByRole("dialog");
    await user.click(screen.getByRole("button", { name: "确认删除" }));

    await waitFor(() => {
      expect(document.querySelector(".app-toast")?.textContent).toContain("删除会话失败");
    });
    // The session stays in the sidebar and stays open: a failed delete must
    // not present itself as success.
    expect(document.querySelector(".session-item.active")?.textContent).toContain("会话A");
    expect(screen.getByText("hi")).toBeTruthy();
  });
});
