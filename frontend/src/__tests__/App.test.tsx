import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import App from "../App";
import * as api from "../api";
import type { HistoryMessage } from "../lib/history";

// The App is exercised purely against a mocked ./api. No real backend, no
// network, no ~/.easycode data (easycode-audit rule 3). SSE is driven by
// capturing the `onEvent` callback that App forwards to `streamChat`, so we can
// replay browser-like events deterministically.
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
}));

const m = vi.mocked(api);

function session(id: string, title: string): api.SessionSummary {
  return { id, title, created_at: "2026-01-01T00:00:00Z", model_alias: "m", permission_mode: "ask" };
}

function detail(id: string, title: string, messages: HistoryMessage[]): api.SessionDetail {
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
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

/** A streamChat mock that captures `onEvent` and stays in-flight forever. */
function captureStream() {
  let onEvent: ((e: api.ChatEvent) => void) | undefined;
  m.streamChat.mockImplementation((_sid, _msg, cb, _opts) => {
    onEvent = cb;
    return new Promise<void>(() => {});
  });
  return { get: () => onEvent };
}

beforeEach(() => {
  vi.clearAllMocks();
  m.fetchSessions.mockResolvedValue([]);
  m.fetchArchivedSessions.mockResolvedValue([]);
  m.fetchWorkspaces.mockResolvedValue({ projects: [] });
  m.fetchModels.mockResolvedValue({ default: "deepseek-v4flash", models: {}, providers: {}, limits: {} });
  m.fetchCommands.mockResolvedValue({ commands: [] });
  m.streamChat.mockResolvedValue(undefined);
  // Echo the requested mode back so the App's optimistic update is confirmed
  // rather than being reverted to a stale value.
  m.setSessionPermission.mockImplementation(async (_id, mode) => ({ id: "s1", permission_mode: mode }));
  m.submitApproval.mockResolvedValue(undefined);
});

describe("App", () => {
  it("权限改动后发送请求使用最新 currentPermission", async () => {
    const user = userEvent.setup();
    m.fetchSessions.mockResolvedValue([session("s1", "会话A")]);
    m.fetchSession.mockResolvedValue(detail("s1", "会话A", [{ role: "user", content: "hi" }]));

    render(<App />);
    await screen.findByText("会话A");
    await user.click(screen.getByText("会话A"));
    await screen.findByText("hi"); // currentSession loaded, permission_mode -> ask

    // Flip permission to allow-all through the real picker UI.
    await user.click(screen.getByRole("button", { name: /请求批准/ }));
    await user.click(screen.getByRole("menuitemradio", { name: /完全访问/ }));

    await user.type(screen.getByRole("textbox"), "hello");
    await user.click(screen.getByRole("button", { name: /发送消息/ }));

    await waitFor(() => expect(m.streamChat).toHaveBeenCalledTimes(1));
    expect(m.streamChat).toHaveBeenCalledWith(
      "s1",
      "hello",
      expect.any(Function),
      expect.objectContaining({ permission_mode: "allow-all" }),
    );
  });

  it("fetchSession 乱序响应最终只显示最后点击的会话", async () => {
    const user = userEvent.setup();
    m.fetchSessions.mockResolvedValue([session("s1", "会话A"), session("s2", "会话B")]);
    const d1 = deferred<api.SessionDetail>();
    const d2 = deferred<api.SessionDetail>();
    m.fetchSession.mockImplementation((id: string) => (id === "s1" ? d1.promise : d2.promise));

    render(<App />);
    await screen.findByText("会话A");
    await screen.findByText("会话B");

    await user.click(screen.getByText("会话A")); // open #1 (token 1)
    await user.click(screen.getByText("会话B")); // open #2 (token 2, newest)

    // Resolve the newest request first, then the stale one.
    await act(async () => {
      d2.resolve(detail("s2", "会话B", [{ role: "user", content: "B-内容" }]));
    });
    await screen.findByText("B-内容");

    await act(async () => {
      d1.resolve(detail("s1", "会话A", [{ role: "user", content: "A-内容" }]));
    });

    // The stale response must not clobber the active session view.
    expect(screen.queryByText("A-内容")).toBeNull();
    expect(screen.getByText("B-内容")).toBeTruthy();
  });

  it("旧 SSE 回调在切换会话后不污染新会话", async () => {
    const user = userEvent.setup();
    m.fetchSessions.mockResolvedValue([session("s1", "会话A"), session("s2", "会话B")]);
    m.fetchSession.mockImplementation((id: string) =>
      Promise.resolve(
        id === "s1"
          ? detail("s1", "会话A", [{ role: "user", content: "A-初始" }])
          : detail("s2", "会话B", [{ role: "user", content: "B-视图" }]),
      ),
    );
    const stream = captureStream();

    render(<App />);
    await screen.findByText("会话A");

    // Open s1 and start a stream that stays in-flight.
    await user.click(screen.getByText("会话A"));
    await screen.findByText("A-初始");
    await user.type(screen.getByRole("textbox"), "hello");
    await user.click(screen.getByRole("button", { name: /发送消息/ }));
    await waitFor(() => expect(stream.get()).toBeTruthy());

    // Switch to s2; the stale stream's callback must be ignored afterwards.
    await user.click(screen.getByText("会话B"));
    await screen.findByText("B-视图");

    await act(async () => {
      // stale event belonging to the s1 request
      stream.get()?.({ type: "text", content: "stale-s1-text" });
    });

    expect(screen.queryByText("stale-s1-text")).toBeNull();
    expect(screen.getByText("B-视图")).toBeTruthy();
  });


  it("流进行中点击当前会话：不刷新、不丢失本轮内容和待审批项", async () => {
    const user = userEvent.setup();
    m.fetchSessions.mockResolvedValue([session("s1", "会话A")]);
    m.fetchSession.mockResolvedValue(detail("s1", "会话A", [{ role: "user", content: "hi" }]));
    const stream = captureStream();

    render(<App />);
    await screen.findByText("会话A");
    await user.click(screen.getByText("会话A"));
    await screen.findByText("hi");
    await user.type(screen.getByRole("textbox"), "do it");
    await user.click(screen.getByRole("button", { name: /发送消息/ }));
    await waitFor(() => expect(stream.get()).toBeTruthy());

    await act(async () => {
      stream.get()?.({
        type: "approval_required",
        approval_id: "ap1",
        tool_call: { id: "t1", name: "execute_shell", arguments: {} },
        reason: "r1",
      });
    });
    await screen.findByText("1/1 个问题");
    expect(m.fetchSession).toHaveBeenCalledTimes(1);

    // Re-open the current session while its turn is streaming.
    await user.click(document.querySelector(".session-item") as HTMLElement);

    // The stale disk snapshot must not replace this turn's items.
    await waitFor(() => expect(screen.getByText("1/1 个问题")).toBeTruthy());
    expect(m.fetchSession).toHaveBeenCalledTimes(1);
  });

  it("历史审批不参与本轮序号：新审批显示 1/1", async () => {
    const user = userEvent.setup();
    m.fetchSessions.mockResolvedValue([session("s1", "会话A")]);
    m.fetchSession.mockResolvedValue({
      ...detail("s1", "会话A", [
        { role: "user", content: "old" },
        {
          role: "assistant",
          content: "",
          tool_calls: [{ id: "hist1", function: { name: "write_file", arguments: "{}" } }],
        },
        { role: "tool", tool_call_id: "hist1", content: "ok" },
      ]),
      approvals: [
        {
          tool_call_id: "hist1",
          name: "write_file",
          args: {},
          decision: "approved",
        },
      ],
    });
    const stream = captureStream();

    render(<App />);
    await screen.findByText("会话A");
    await user.click(screen.getByText("会话A"));
    await user.type(screen.getByRole("textbox"), "new task");
    await user.click(screen.getByRole("button", { name: /发送消息/ }));
    await waitFor(() => expect(stream.get()).toBeTruthy());

    await act(async () => {
      stream.get()?.({
        type: "approval_required",
        approval_id: "ap1",
        tool_call: { id: "t1", name: "execute_shell", arguments: {} },
        reason: "r",
      });
    });
    await screen.findByText("1/1 个问题");
  });

  it("命令菜单高亮与回车选中一致（按 kind 稳定排序）", async () => {
    const user = userEvent.setup();
    m.fetchCommands.mockResolvedValue({
      commands: [
        { name: "a", description: "", kind: "skill" },
        { name: "b", description: "", kind: "template" },
        { name: "c", description: "", kind: "skill" },
      ],
    });

    render(<App />);
    const box = await screen.findByRole("textbox");
    await user.type(box, "/");
    fireEvent.keyDown(box, { key: "ArrowDown" });
    fireEvent.keyDown(box, { key: "Enter" });

    await waitFor(() => expect((box as HTMLTextAreaElement).value).toBe("/c "));
  });

  it("已发送的用户消息时间不随后续渲染变化", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime.bind(vi) });
      m.fetchSessions.mockResolvedValue([session("s1", "会话A")]);
      m.fetchSession.mockResolvedValue(detail("s1", "会话A", []));

      render(<App />);
      await screen.findByText("会话A");
      await user.click(screen.getByText("会话A"));
      await user.type(screen.getByRole("textbox"), "hi");
      await user.click(screen.getByRole("button", { name: /发送消息/ }));

      const timeEl = await waitFor(() => {
        const el = document.querySelector(".msg-row.user time");
        expect(el).toBeTruthy();
        return el as HTMLElement;
      });
      const before = timeEl.textContent;

      await act(async () => {
        vi.advanceTimersByTime(120_000);
      });
      // A re-render (state change) must keep the stamped send time.
      fireEvent.change(screen.getByRole("textbox"), { target: { value: "x" } });

      expect(document.querySelector(".msg-row.user time")?.textContent).toBe(before);
    } finally {
      vi.useRealTimers();
    }
  });

  it("网络中断时工具卡片不再显示执行中", async () => {
    const user = userEvent.setup();
    m.fetchSessions.mockResolvedValue([session("s1", "会话A")]);
    m.fetchSession.mockResolvedValue(detail("s1", "会话A", []));
    m.streamChat.mockImplementation(async (_sid, _msg, cb) => {
      cb({ type: "tool_start", tool_call: { id: "t1", name: "execute_shell", arguments: {} } });
      throw new Error("net");
    });

    render(<App />);
    await screen.findByText("会话A");
    await user.click(screen.getByText("会话A"));
    await user.type(screen.getByRole("textbox"), "run");
    await user.click(screen.getByRole("button", { name: /发送消息/ }));

    await screen.findByText("net");
    expect(document.querySelector(".tool-card.running")).toBeNull();
    expect(document.querySelector(".tool-card")?.textContent).not.toContain("执行中");
  });

  it("多审批显示动态 position/total", async () => {
    const user = userEvent.setup();
    m.fetchSessions.mockResolvedValue([session("s1", "会话A")]);
    m.fetchSession.mockResolvedValue(detail("s1", "会话A", []));
    const stream = captureStream();

    render(<App />);
    await screen.findByText("会话A");
    await user.click(screen.getByText("会话A"));
    await user.type(screen.getByRole("textbox"), "do it");
    await user.click(screen.getByRole("button", { name: /发送消息/ }));
    await waitFor(() => expect(stream.get()).toBeTruthy());

    const emit = (e: api.ChatEvent) =>
      act(async () => {
        stream.get()?.(e);
      });

    // First pending approval => 1/1
    await emit({
      type: "approval_required",
      approval_id: "ap1",
      tool_call: { id: "t1", name: "execute_shell", arguments: {} },
      reason: "r1",
    });
    await screen.findByText("1/1 个问题");

    // Second approval queued behind the first => still 1/2 (first pending)
    await emit({
      type: "approval_required",
      approval_id: "ap2",
      tool_call: { id: "t2", name: "read_file", arguments: {} },
      reason: "r2",
    });
    await screen.findByText("1/2 个问题");

    // Resolve the first (deny) => position advances to 2/2
    await user.click(screen.getByRole("button", { name: "拒绝" }));
    await screen.findByText("2/2 个问题");
  });

  it("Escape 关闭权限菜单（useDismiss 统一行为）", async () => {
    const user = userEvent.setup();

    render(<App />);
    await screen.findByRole("button", { name: /请求批准/ });
    await user.click(screen.getByRole("button", { name: /请求批准/ }));
    expect(screen.getByRole("menu", { name: "选择权限模式" })).toBeTruthy();

    fireEvent.keyDown(window, { key: "Escape" });

    expect(screen.queryByRole("menu", { name: "选择权限模式" })).toBeNull();
  });

  it("移动端侧栏抽屉——按钮存在、点击显示、遮罩/Escape 关闭、点会话或新会话关闭", async () => {
    const user = userEvent.setup();
    m.fetchSessions.mockResolvedValue([session("s1", "会话A")]);
    m.fetchSession.mockResolvedValue(detail("s1", "会话A", [{ role: "user", content: "hi" }]));

    render(<App />);
    await screen.findByText("会话A");
    const app = () => document.querySelector(".app") as HTMLElement;
    const toggle = screen.getByRole("button", { name: "打开侧栏" });

    // The hamburger button is present; the drawer starts closed (state/class,
    // not real CSS media query — easycode-audit rule: verify via class/state).
    expect(toggle).toBeTruthy();
    expect(app().classList.contains("sidebar-open")).toBe(false);
    expect(screen.queryByTestId("sidebar-overlay")).toBeNull();

    // Open the drawer: app gains sidebar-open, the scrim overlay mounts.
    await user.click(toggle);
    expect(app().classList.contains("sidebar-open")).toBe(true);
    expect(screen.getByTestId("sidebar-overlay")).toBeTruthy();

    // Escape closes the drawer (window keydown listener).
    fireEvent.keyDown(window, { key: "Escape" });
    expect(app().classList.contains("sidebar-open")).toBe(false);
    expect(screen.queryByTestId("sidebar-overlay")).toBeNull();

    // Reopen, then close by clicking the scrim overlay.
    await user.click(screen.getByRole("button", { name: "打开侧栏" }));
    expect(app().classList.contains("sidebar-open")).toBe(true);
    fireEvent.click(screen.getByTestId("sidebar-overlay"));
    expect(app().classList.contains("sidebar-open")).toBe(false);
    expect(screen.queryByTestId("sidebar-overlay")).toBeNull();

    // Reopen, then clicking a session closes the drawer and loads the session.
    await user.click(screen.getByRole("button", { name: "打开侧栏" }));
    expect(app().classList.contains("sidebar-open")).toBe(true);
    await user.click(screen.getByText("会话A"));
    await screen.findByText("hi");
    expect(app().classList.contains("sidebar-open")).toBe(false);

    // Reopen, then "新会话" also closes the drawer and starts a fresh session.
    await user.click(screen.getByRole("button", { name: "打开侧栏" }));
    expect(app().classList.contains("sidebar-open")).toBe(true);
    await user.click(screen.getByRole("button", { name: /新会话/ }));
    expect(app().classList.contains("sidebar-open")).toBe(false);
  });

  it("项目置顶标记与项目折叠/展开功能正常", async () => {
    const user = userEvent.setup();
    const s1 = { ...session("s1", "会话A"), root: "/workspace/projA" };
    m.fetchSessions.mockResolvedValue([s1]);
    m.fetchWorkspaces.mockResolvedValue({
      projects: [{ root: "/workspace/projA", secondary: [], name: "Project A", pinned: true }],
    });

    render(<App />);
    await screen.findByText("会话A");

    // 置顶图标/标记存在
    expect(screen.getByLabelText("已置顶")).toBeTruthy();

    // 默认展开，会话可见
    expect(screen.getByText("会话A")).toBeTruthy();

    // 点击折叠按钮
    const collapseToggle = screen.getByRole("button", { name: "折叠项目" });
    await user.click(collapseToggle);

    // 会话被折叠隐藏
    expect(screen.queryByText("会话A")).toBeNull();

    // 再次点击展开
    const expandToggle = screen.getByRole("button", { name: "展开项目" });
    await user.click(expandToggle);

    // 会话重新显示
    expect(screen.getByText("会话A")).toBeTruthy();
  });
});
