import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import App from "../App";
import * as api from "../api";
import { activeTitle, composerField, deferred, detail, primeApiMock, session, sidebarRow } from "./helpers";

// The App is exercised purely against a mocked ./api. No real backend, no
// network, no ~/.easycode data (easycode-audit rule 3). SSE is driven by
// capturing the `onEvent` callback that App forwards to `streamChat`, so we can
// replay browser-like events deterministically.
vi.mock("../api", async () => (await import("./helpers")).apiMock);

const m = vi.mocked(api);

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
  primeApiMock(m);
  m.fetchSessions.mockResolvedValue([]);
  m.fetchModels.mockResolvedValue({ default: "deepseek-v4flash", models: {}, providers: {}, limits: {} });
});

describe("App", () => {
  it("权限改动后发送请求使用最新 currentPermission", async () => {
    const user = userEvent.setup();
    m.fetchSessions.mockResolvedValue([session("s1", "会话A")]);
    m.fetchSession.mockResolvedValue(detail("s1", "会话A", [{ role: "user", content: "hi" }]));

    render(<App />);
    await screen.findByText("会话A");
    await user.click(sidebarRow("会话A"));
    await screen.findByText("hi"); // currentSession loaded, permission_mode -> ask

    // Flip permission to allow-all through the real picker UI.
    await user.click(screen.getByRole("button", { name: /请求批准/ }));
    await user.click(screen.getByRole("menuitemradio", { name: /完全访问/ }));

    await user.type(composerField(), "hello");
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

    await user.click(sidebarRow("会话A")); // open #1 (token 1)
    await user.click(sidebarRow("会话B")); // open #2 (token 2, newest)

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
    await user.click(sidebarRow("会话A"));
    await screen.findByText("A-初始");
    await user.type(composerField(), "hello");
    await user.click(screen.getByRole("button", { name: /发送消息/ }));
    await waitFor(() => expect(stream.get()).toBeTruthy());

    // Switch to s2; the stale stream's callback must be ignored afterwards.
    await user.click(sidebarRow("会话B"));
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
    await user.click(sidebarRow("会话A"));
    await screen.findByText("hi");
    await user.type(composerField(), "do it");
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
    await screen.findByText("需要批准");
    expect(m.fetchSession).toHaveBeenCalledTimes(1);

    // Re-open the current session while its turn is streaming.
    await user.click(document.querySelector(".session-item") as HTMLElement);

    // The stale disk snapshot must not replace this turn's items.
    await waitFor(() => expect(screen.getByText("需要批准")).toBeTruthy());
    expect(m.fetchSession).toHaveBeenCalledTimes(1);
  });

  it("历史审批不参与本轮序号", async () => {
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
    await user.click(sidebarRow("会话A"));
    await user.type(composerField(), "new task");
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
    await act(async () => {
      stream.get()?.({
        type: "approval_required",
        approval_id: "ap2",
        tool_call: { id: "t2", name: "read_file", arguments: {} },
        reason: "r2",
      });
    });

    // Only this turn's two requests are counted, not the restored history one.
    await screen.findByText("1/2");
    expect(screen.getByText("2/2")).toBeTruthy();
    expect(screen.queryByText("3/3")).toBeNull();
  });

  it("任务清单事件在消息流显示摘要，可在面板查看详情", async () => {
    const user = userEvent.setup();
    m.fetchSessions.mockResolvedValue([session("s1", "会话A")]);
    m.fetchSession.mockResolvedValue(detail("s1", "会话A", []));
    const stream = captureStream();

    render(<App />);
    await screen.findByText("会话A");
    await user.click(sidebarRow("会话A"));
    await user.type(composerField(), "go");
    await user.click(screen.getByRole("button", { name: /发送消息/ }));
    await waitFor(() => expect(stream.get()).toBeTruthy());

    await act(async () => {
      stream.get()?.({
        type: "todo",
        todos: [
          { text: "读代码", status: "completed" },
          { text: "改代码", status: "in_progress" },
        ],
      });
    });

    // the stream carries a compact pointer, and the pane opens on the list
    const summary = await screen.findByRole("button", { name: /任务清单/ });
    expect(summary.textContent).toContain("1/2");
    const pane = await screen.findByRole("complementary", { name: "任务面板" });
    expect(pane.textContent).toContain("读代码");
    expect(pane.textContent).toContain("改代码");
    // the summary is the way back to the list when the pane is closed
    await user.click(screen.getByRole("button", { name: "收起面板" }));
    expect(document.querySelector(".right-pane")).toBeNull();
    await user.click(summary);
    expect(document.querySelector(".right-pane")).toBeTruthy();
  });

  it("重开会话时恢复已保存的任务清单", async () => {
    const user = userEvent.setup();
    m.fetchSessions.mockResolvedValue([session("s1", "会话A")]);
    m.fetchSession.mockResolvedValue({
      ...detail("s1", "会话A", [{ role: "user", content: "hi" }]),
      todos: [{ text: "遗留步骤", status: "pending" }],
    });

    render(<App />);
    await screen.findByText("会话A");
    await user.click(sidebarRow("会话A"));

    expect(await screen.findByText("任务清单")).toBeTruthy();
    await user.click(screen.getByRole("button", { name: /任务清单/ }));
    expect(await screen.findByText("遗留步骤")).toBeTruthy();
    // 恢复出来的清单不是“当前进度”
    expect(screen.getByText("上次清单")).toBeTruthy();
  });

  it("本轮更新过清单就不标为上次清单", async () => {
    const user = userEvent.setup();
    m.fetchSessions.mockResolvedValue([session("s1", "会话A")]);
    m.fetchSession.mockResolvedValue(detail("s1", "会话A", []));
    const stream = captureStream();

    render(<App />);
    await screen.findByText("会话A");
    await user.click(sidebarRow("会话A"));
    await user.type(composerField(), "go");
    await user.click(screen.getByRole("button", { name: /发送消息/ }));
    await waitFor(() => expect(stream.get()).toBeTruthy());

    await act(async () => {
      stream.get()?.({ type: "todo", todos: [{ text: "本轮步骤", status: "in_progress" }] });
    });

    await screen.findByText("本轮步骤");
    expect(screen.queryByText("上次清单")).toBeNull();
  });

  it("置顶会话后出现在置顶分区", async () => {
    const user = userEvent.setup();
    m.fetchSessions.mockResolvedValue([session("s1", "会话A")]);
    m.pinSession.mockResolvedValue({ ok: true });

    render(<App />);
    await screen.findByText("会话A");
    expect(screen.queryByText("置顶")).toBeNull();

    // the server is the source of truth: the list comes back pinned
    m.fetchSessions.mockResolvedValue([{ ...session("s1", "会话A"), pinned: true, pinned_at: "2026-02-01T00:00:00Z" }]);
    await user.click(screen.getByRole("button", { name: /置顶会话/ }));

    await waitFor(() => expect(m.pinSession).toHaveBeenCalledWith("s1", true));
    expect(await screen.findByText("置顶")).toBeTruthy();
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
    const box = await composerField();
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
      await user.click(sidebarRow("会话A"));
      await user.type(composerField(), "hi");
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
      fireEvent.change(composerField(), { target: { value: "x" } });

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
    await user.click(sidebarRow("会话A"));
    await user.type(composerField(), "run");
    await user.click(screen.getByRole("button", { name: /发送消息/ }));

    await screen.findByText("net");
    // The interrupted tool must not leave the trace spinning.
    expect(document.querySelector(".trace.running")).toBeNull();
    await user.click(screen.getByRole("button", { name: /展开工具调用/ }));
    expect(document.querySelector(".trace-step.running")).toBeNull();
    expect(document.querySelector(".trace-step.error")).toBeTruthy();
  });

  it("后端 error 后连接中断不重复追加错误行", async () => {
    const user = userEvent.setup();
    m.fetchSessions.mockResolvedValue([session("s1", "会话A")]);
    m.fetchSession.mockResolvedValue(detail("s1", "会话A", []));
    m.streamChat.mockImplementation(async (_sid, _msg, cb) => {
      cb({ type: "tool_start", tool_call: { id: "t1", name: "execute_shell", arguments: {} } });
      cb({ type: "error", error: "provider exploded" });
      throw new Error("连接中断：本轮未正常结束，已保留已生成内容。");
    });

    render(<App />);
    await screen.findByText("会话A");
    await user.click(sidebarRow("会话A"));
    await user.type(composerField(), "run");
    await user.click(screen.getByRole("button", { name: /发送消息/ }));

    await screen.findByText("provider exploded");
    // The backend error is the single terminal report; the later connection
    // failure must not append a second error row.
    expect(document.querySelectorAll(".msg.error").length).toBe(1);
    expect(document.querySelector(".trace.running")).toBeNull();
  });

  it("多审批显示动态 position/total", async () => {
    const user = userEvent.setup();
    m.fetchSessions.mockResolvedValue([session("s1", "会话A")]);
    m.fetchSession.mockResolvedValue(detail("s1", "会话A", []));
    const stream = captureStream();

    render(<App />);
    await screen.findByText("会话A");
    await user.click(sidebarRow("会话A"));
    await user.type(composerField(), "do it");
    await user.click(screen.getByRole("button", { name: /发送消息/ }));
    await waitFor(() => expect(stream.get()).toBeTruthy());

    const emit = (e: api.ChatEvent) =>
      act(async () => {
        stream.get()?.(e);
      });

    await emit({
      type: "approval_required",
      approval_id: "ap1",
      tool_call: { id: "t1", name: "execute_shell", arguments: {} },
      reason: "r1",
    });
    await emit({
      type: "approval_required",
      approval_id: "ap2",
      tool_call: { id: "t2", name: "read_file", arguments: {} },
      reason: "r2",
    });

    // Each request carries its own position in the turn's queue.
    await screen.findByText("1/2");
    expect(screen.getByText("2/2")).toBeTruthy();

    // Answering the first collapses it to a verdict; the second stays pending.
    await user.click(screen.getAllByRole("button", { name: "拒绝" })[0]);
    await screen.findByText("已拒绝请求");
    expect(screen.getByText("2/2")).toBeTruthy();
    expect(screen.getAllByRole("button", { name: "允许一次" })).toHaveLength(1);
  });

  it("打开会话后按会话范围刷新命令（DEC-C8）", async () => {
    const user = userEvent.setup();
    m.fetchSessions.mockResolvedValue([session("s1", "会话A")]);
    m.fetchSession.mockResolvedValue(detail("s1", "会话A", []));

    render(<App />);
    await screen.findByText("会话A");

    await user.click(sidebarRow("会话A"));

    await waitFor(() => expect(m.fetchCommands).toHaveBeenLastCalledWith("s1", undefined));
  });

  it("命令范围快速切换时旧响应不覆盖新范围", async () => {
    const user = userEvent.setup();
    m.fetchSessions.mockResolvedValue([session("s1", "会话A"), session("s2", "会话B")]);
    m.fetchSession.mockImplementation((id: string) =>
      Promise.resolve(id === "s1" ? detail("s1", "会话A", []) : detail("s2", "会话B", [])),
    );
    const pending: Array<{
      sessionId: string | null;
      resolve: (v: { commands: api.CommandInfo[] }) => void;
    }> = [];
    m.fetchCommands.mockImplementation(
      (sessionId?: string | null) =>
        new Promise<{ commands: api.CommandInfo[] }>((resolve) => {
          pending.push({ sessionId: sessionId ?? null, resolve });
        }),
    );

    render(<App />);
    await screen.findByText("会话A");
    await screen.findByText("会话B");
    await user.click(sidebarRow("会话A"));
    await user.click(sidebarRow("会话B"));

    const callFor = (id: string) => pending.filter((p) => p.sessionId === id).at(-1)!;
    await waitFor(() => expect(callFor("s2")).toBeTruthy());

    // B (current scope) resolves first, then A's stale response arrives late.
    await act(async () => {
      callFor("s2").resolve({
        commands: [{ name: "only-b", description: "", kind: "template" }],
      });
    });
    await act(async () => {
      callFor("s1").resolve({
        commands: [{ name: "only-a", description: "", kind: "template" }],
      });
    });

    await user.type(composerField(), "/");
    await screen.findByText("/only-b");
    expect(screen.queryByText("/only-a")).toBeNull();
  });

  it("新会话发送显式携带次目录列表（空列表表示明确不使用）", async () => {
    const user = userEvent.setup();

    render(<App />);
    await screen.findByRole("button", { name: /请求批准/ });
    await user.type(composerField(), "hello");
    await user.click(screen.getByRole("button", { name: /发送消息/ }));

    await waitFor(() => expect(m.streamChat).toHaveBeenCalledTimes(1));
    expect(m.streamChat).toHaveBeenCalledWith(
      null,
      "hello",
      expect.any(Function),
      expect.objectContaining({ secondary_roots: [] }),
    );
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

  // 回归：命令/提及/权限浮层都开在聊天卡片里，卡片带 overflow: hidden，窗口一矮
  // 顶部就被裁掉。composer 把自己上方剩下的空间发布成 --popover-room，浮层用它
  // 限制高度；jsdom 不排版，所以这里只验证发布的值确实来自测量结果。
  it("composer 发布 --popover-room，等于上方可用空间（留出边距与间隙）", async () => {
    render(<App />);
    await screen.findByRole("button", { name: /请求批准/ });

    const box = document.querySelector(".composer") as HTMLElement;
    expect(box).toBeTruthy();
    box.getBoundingClientRect = () => ({ top: 300 }) as DOMRect;
    fireEvent(window, new Event("resize"));

    expect(box.style.getPropertyValue("--popover-room")).toBe("278px");
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
    await user.click(sidebarRow("会话A"));
    await screen.findByText("hi");
    expect(app().classList.contains("sidebar-open")).toBe(false);

    // Reopen, then "新会话" also closes the drawer and starts a fresh session.
    await user.click(screen.getByRole("button", { name: "打开侧栏" }));
    expect(app().classList.contains("sidebar-open")).toBe(true);
    await user.click(screen.getByRole("button", { name: /新会话/ }));
    expect(app().classList.contains("sidebar-open")).toBe(false);
  });

  it("A 的权限响应迟到时不改写 B 的权限，B 发送使用 B 的权限", async () => {
    const user = userEvent.setup();
    m.fetchSessions.mockResolvedValue([session("s1", "会话A"), session("s2", "会话B")]);
    m.fetchSession.mockImplementation((id: string) =>
      Promise.resolve(
        id === "s1"
          ? detail("s1", "会话A", [{ role: "user", content: "A-内容" }])
          : detail("s2", "会话B", [{ role: "user", content: "B-内容" }]),
      ),
    );
    const perm = deferred<{ id: string; permission_mode: string }>();
    m.setSessionPermission.mockReturnValue(perm.promise);

    render(<App />);
    await screen.findByText("会话A");
    await user.click(sidebarRow("会话A"));
    await screen.findByText("A-内容");

    await user.click(screen.getByRole("button", { name: /请求批准/ }));
    await user.click(screen.getByRole("menuitemradio", { name: /完全访问/ }));
    await waitFor(() => expect(m.setSessionPermission).toHaveBeenCalledWith("s1", "allow-all"));

    // Switch to B while A's permission request is still pending.
    await user.click(sidebarRow("会话B"));
    await screen.findByText("B-内容");

    await act(async () => {
      perm.resolve({ id: "s1", permission_mode: "allow-all" });
    });

    // The late A response must not flip B to allow-all.
    await waitFor(() => expect(screen.getByRole("button", { name: /请求批准/ })).toBeTruthy());

    await user.type(composerField(), "hello");
    await user.click(screen.getByRole("button", { name: /发送消息/ }));
    await waitFor(() => expect(m.streamChat).toHaveBeenCalledTimes(1));
    expect(m.streamChat).toHaveBeenCalledWith(
      "s2",
      "hello",
      expect.any(Function),
      expect.objectContaining({ permission_mode: "ask" }),
    );
  });

  it("权限请求未确认前禁止发送，确认后使用新权限", async () => {
    const user = userEvent.setup();
    m.fetchSessions.mockResolvedValue([session("s1", "会话A")]);
    m.fetchSession.mockResolvedValue(detail("s1", "会话A", [{ role: "user", content: "hi" }]));
    const perm = deferred<{ id: string; permission_mode: string }>();
    m.setSessionPermission.mockReturnValue(perm.promise);

    render(<App />);
    await screen.findByText("会话A");
    await user.click(sidebarRow("会话A"));
    await screen.findByText("hi");

    await user.click(screen.getByRole("button", { name: /请求批准/ }));
    await user.click(screen.getByRole("menuitemradio", { name: /完全访问/ }));
    await waitFor(() => expect(m.setSessionPermission).toHaveBeenCalledWith("s1", "allow-all"));

    // The view still shows the confirmed mode and sending is blocked until the
    // server confirms: a turn must not write the old mode back.
    expect(screen.getByRole("button", { name: /请求批准/ })).toBeTruthy();
    await user.type(composerField(), "hello");
    const sendBtn = screen.getByRole("button", { name: /发送消息/ }) as HTMLButtonElement;
    expect(sendBtn.disabled).toBe(true);
    await user.click(sendBtn);
    expect(m.streamChat).not.toHaveBeenCalled();

    await act(async () => {
      perm.resolve({ id: "s1", permission_mode: "allow-all" });
    });

    await waitFor(() => expect(screen.getByRole("button", { name: /完全访问/ })).toBeTruthy());
    await user.click(screen.getByRole("button", { name: /发送消息/ }));
    await waitFor(() => expect(m.streamChat).toHaveBeenCalledTimes(1));
    expect(m.streamChat).toHaveBeenCalledWith(
      "s1",
      "hello",
      expect.any(Function),
      expect.objectContaining({ permission_mode: "allow-all" }),
    );
  });

  it("权限修改失败时保留原确认值并提示错误", async () => {
    const user = userEvent.setup();
    m.fetchSessions.mockResolvedValue([session("s1", "会话A")]);
    m.fetchSession.mockResolvedValue(detail("s1", "会话A", [{ role: "user", content: "hi" }]));
    m.setSessionPermission.mockRejectedValue(new Error("boom"));

    render(<App />);
    await screen.findByText("会话A");
    await user.click(sidebarRow("会话A"));
    await screen.findByText("hi");

    await user.click(screen.getByRole("button", { name: /请求批准/ }));
    await user.click(screen.getByRole("menuitemradio", { name: /完全访问/ }));

    await waitFor(() => {
      expect(document.querySelector(".app-toast")?.textContent).toContain("修改权限失败");
    });
    // No optimistic flip: the confirmed "ask" mode stays in place.
    expect(screen.getByRole("button", { name: /请求批准/ })).toBeTruthy();
  });

  it("B 详情未返回前禁止发送，加载完成后使用 B 的权限", async () => {
    const user = userEvent.setup();
    m.fetchSessions.mockResolvedValue([session("s1", "会话A"), session("s2", "会话B")]);
    const d1 = deferred<api.SessionDetail>();
    const d2 = deferred<api.SessionDetail>();
    m.fetchSession.mockImplementation((id: string) => (id === "s1" ? d1.promise : d2.promise));

    render(<App />);
    await screen.findByText("会话A");
    await user.click(sidebarRow("会话A"));
    await act(async () => {
      d1.resolve({ ...detail("s1", "会话A", []), permission_mode: "allow-all" });
    });
    await waitFor(() => expect(screen.getByRole("button", { name: /完全访问/ })).toBeTruthy());

    // Open B; its detail has not arrived yet.
    await user.click(sidebarRow("会话B"));
    await screen.findByText("正在加载会话…");

    await user.type(composerField(), "hello");
    const sendBtn = screen.getByRole("button", { name: /发送消息/ }) as HTMLButtonElement;
    expect(sendBtn.disabled).toBe(true);
    await user.click(sendBtn);
    expect(m.streamChat).not.toHaveBeenCalled();

    await act(async () => {
      d2.resolve(detail("s2", "会话B", [{ role: "user", content: "B-初始" }]));
    });
    await screen.findByText("B-初始");
    expect(screen.queryByText("正在加载会话…")).toBeNull();

    await user.click(screen.getByRole("button", { name: /发送消息/ }));
    await waitFor(() => expect(m.streamChat).toHaveBeenCalledTimes(1));
    expect(m.streamChat).toHaveBeenCalledWith(
      "s2",
      "hello",
      expect.any(Function),
      expect.objectContaining({ permission_mode: "ask" }),
    );
  });

  it("会话详情加载失败时禁止发送，重试成功后恢复", async () => {
    const user = userEvent.setup();
    m.fetchSessions.mockResolvedValue([session("s1", "会话A")]);
    m.fetchSession
      .mockRejectedValueOnce(new Error("boom"))
      .mockResolvedValue(detail("s1", "会话A", [{ role: "user", content: "A-内容" }]));

    render(<App />);
    await screen.findByText("会话A");
    await user.click(sidebarRow("会话A"));

    await screen.findByText(/会话加载失败/);
    await user.type(composerField(), "hello");
    await user.click(screen.getByRole("button", { name: /发送消息/ }));
    expect(m.streamChat).not.toHaveBeenCalled();

    // The failure carries its own retry: no guessing which click retries.
    await user.click(screen.getByRole("button", { name: "重试" }));
    await screen.findByText("A-内容");

    await user.click(screen.getByRole("button", { name: /发送消息/ }));
    await waitFor(() => expect(m.streamChat).toHaveBeenCalledTimes(1));
  });

  it("次目录保存期间切换会话，旧结果不覆盖新会话", async () => {
    const user = userEvent.setup();
    m.fetchSessions.mockResolvedValue([
      { ...session("s1", "会话A"), root: "/pa" },
      { ...session("s2", "会话B"), root: "/pb" },
    ]);
    m.fetchSession.mockImplementation((id: string) =>
      Promise.resolve(
        id === "s1"
          ? { ...detail("s1", "会话A", []), secondary_roots: ["/s1a"] }
          : { ...detail("s2", "会话B", []), secondary_roots: ["/s2a"] },
      ),
    );
    const save = deferred<{ root: string | null; secondary: string[]; projects: [] }>();
    m.saveProject.mockReturnValue(save.promise);

    render(<App />);
    await screen.findByText("会话A");
    await user.click(sidebarRow("会话A"));
    await waitFor(() =>
      expect(document.querySelector(".secondary-editor .dir-card small")?.textContent).toContain("已连接 1 个目录"),
    );

    await user.click(screen.getByRole("button", { name: /次目录/ }));
    await user.click(screen.getByRole("button", { name: "移除次目录 s1a" }));

    // Switch to B while A's save is in flight.
    await user.click(sidebarRow("会话B"));
    await waitFor(() =>
      expect(document.querySelector(".secondary-editor .dir-card small")?.textContent).toContain("已连接 1 个目录"),
    );

    await act(async () => {
      save.resolve({ root: "/pa", secondary: [], projects: [] });
    });

    // A's stale save result must not blank B's secondary list.
    expect(document.querySelector(".secondary-editor .dir-card small")?.textContent).toContain("已连接 1 个目录");
  });

  it("会话加载期间次目录移除按钮不可用，不会调用保存", async () => {
    const user = userEvent.setup();
    m.fetchSessions.mockResolvedValue([
      { ...session("s1", "会话A"), root: "/pa" },
      { ...session("s2", "会话B"), root: "/pb" },
    ]);
    const dB = deferred<api.SessionDetail>();
    m.fetchSession.mockImplementation((id: string) =>
      id === "s1"
        ? Promise.resolve({ ...detail("s1", "会话A", []), secondary_roots: ["/s1a"] })
        : dB.promise,
    );

    render(<App />);
    await screen.findByText("会话A");
    await user.click(sidebarRow("会话A"));
    await waitFor(() =>
      expect(document.querySelector(".secondary-editor .dir-card small")?.textContent).toContain("已连接 1 个目录"),
    );
    await user.click(screen.getByRole("button", { name: /次目录/ }));

    // Switch to B while its detail is still in flight: the editor shows A's
    // stale list but must not allow a save against the unconfirmed view.
    await user.click(sidebarRow("会话B"));
    await screen.findByText("正在加载会话…");

    const remove = screen.getByRole("button", { name: "移除次目录 s1a" }) as HTMLButtonElement;
    expect(remove.disabled).toBe(true);
    fireEvent.click(remove);
    expect(m.saveProject).not.toHaveBeenCalled();
  });

  it("次目录保存返回前 A→B→A，旧响应不覆盖重新加载的 A", async () => {
    const user = userEvent.setup();
    m.fetchSessions.mockResolvedValue([
      { ...session("s1", "会话A"), root: "/pa" },
      { ...session("s2", "会话B"), root: "/pb" },
    ]);
    m.fetchSession.mockImplementation((id: string) =>
      Promise.resolve(
        id === "s1"
          ? { ...detail("s1", "会话A", []), secondary_roots: ["/s1a"] }
          : { ...detail("s2", "会话B", []), secondary_roots: ["/s2a"] },
      ),
    );
    const save = deferred<{ root: string | null; secondary: string[]; projects: [] }>();
    m.saveProject.mockReturnValue(save.promise);

    render(<App />);
    await screen.findByText("会话A");
    await user.click(sidebarRow("会话A"));
    await waitFor(() =>
      expect(document.querySelector(".secondary-editor .dir-card small")?.textContent).toContain("已连接 1 个目录"),
    );
    await user.click(screen.getByRole("button", { name: /次目录/ }));
    await user.click(screen.getByRole("button", { name: "移除次目录 s1a" }));
    await waitFor(() => expect(m.saveProject).toHaveBeenCalledTimes(1));

    // A -> B -> A while A's save is still in flight: the view version changed
    // twice, so the old response must be dropped even though the session id
    // matches the current view again.
    await user.click(sidebarRow("会话B"));
    await waitFor(() =>
      expect(document.querySelector(".secondary-editor .dir-card small")?.textContent).toContain("已连接 1 个目录"),
    );
    await user.click(sidebarRow("会话A"));
    await waitFor(() =>
      expect(document.querySelector(".secondary-editor .dir-card small")?.textContent).toContain("已连接 1 个目录"),
    );

    await act(async () => {
      save.resolve({ root: "/pa", secondary: [], projects: [] });
    });

    // A's freshly reloaded list stays intact.
    expect(document.querySelector(".secondary-editor .dir-card small")?.textContent).toContain("已连接 1 个目录");
  });

  it("移除当前会话所属项目后清空会话视图", async () => {
    const user = userEvent.setup();
    const s1 = { ...session("s1", "会话A"), root: "/workspace/projA" };
    m.fetchSessions.mockResolvedValue([s1]);
    m.fetchWorkspaces.mockResolvedValue({
      projects: [{ root: "/workspace/projA", secondary: [], name: "Project A" }],
    });
    m.fetchSession.mockResolvedValue(detail("s1", "会话A", [{ role: "user", content: "hi" }]));
    m.removeProject.mockResolvedValue({ deleted_sessions: 1, projects: [] });

    render(<App />);
    await screen.findByText("会话A");
    await user.click(sidebarRow("会话A"));
    await screen.findByText("hi");

    fireEvent.click(document.querySelector(".group-more-btn") as HTMLElement);
    await user.click(screen.getByRole("menuitem", { name: /移除项目/ }));
    await user.click(screen.getByRole("button", { name: "确认移除" }));

    await waitFor(() => expect(m.removeProject).toHaveBeenCalledWith("/workspace/projA"));
    // The open session was deleted with the project; the view returns to a
    // blank new session instead of showing a session that no longer exists.
    await waitFor(() =>
      expect(activeTitle()).toBe("新会话"),
    );
    expect(document.querySelector(".session-item.active")).toBeNull();
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
    expect(sidebarRow("会话A")).toBeTruthy();

    // 点击折叠按钮
    const collapseToggle = screen.getByRole("button", { name: "折叠项目" });
    await user.click(collapseToggle);

    // 会话被折叠隐藏
    expect(screen.queryByText("会话A")).toBeNull();

    // 再次点击展开
    const expandToggle = screen.getByRole("button", { name: "展开项目" });
    await user.click(expandToggle);

    // 会话重新显示
    expect(sidebarRow("会话A")).toBeTruthy();
  });
});


it("草稿切换项目后丢弃旧次目录保存结果", async () => {
  const user = userEvent.setup();
  const projects = [{ root: "/A", secondary: ["/alpha"] }, { root: "/B", secondary: ["/beta"] }];
  m.fetchWorkspaces.mockResolvedValue({ projects });
  const save = deferred<Awaited<ReturnType<typeof api.saveProject>>>();
  m.saveProject.mockReturnValue(save.promise);
  render(<App />);
  await waitFor(() => expect(m.fetchWorkspaces).toHaveBeenCalled());
  await user.click(document.querySelector(".project-picker .dir-card-main")!);
  await user.click(await screen.findByRole("option", { name: /A/ }));
  await user.click(screen.getByRole("button", { name: /次目录/ }));
  await user.click(await screen.findByRole("button", { name: "移除次目录 alpha" }));
  await user.click(document.querySelector(".project-picker .dir-card-main")!);
  await user.click(screen.getByRole("option", { name: /B/ }));
  await screen.findByRole("button", { name: "移除次目录 beta" });
  await act(async () => save.resolve({ root: "/A", secondary: [], projects }));
  expect(screen.getByRole("button", { name: "移除次目录 beta" })).toBeTruthy();
  await user.type(composerField(), "hi");
  await user.click(screen.getByRole("button", { name: "发送消息" }));
  expect(m.streamChat).toHaveBeenCalledWith(null, "hi", expect.any(Function),
    expect.objectContaining({ root: "/B", secondary_roots: ["/beta"] }));
});

it("移除项目期间切换会话后保留新视图", async () => {
  const user = userEvent.setup();
  m.fetchSessions.mockResolvedValue([
    { ...session("s1", "会话A"), root: "/A" }, { ...session("s2", "会话B"), root: "/B" },
  ]);
  m.fetchWorkspaces.mockResolvedValue({ projects: [
    { root: "/A", secondary: [] }, { root: "/B", secondary: [] },
  ] });
  m.fetchSession.mockImplementation(async (id) => detail(id, id, [
    { role: "user", content: id === "s1" ? "A content" : "B content" },
  ]));
  const deleted = deferred<Awaited<ReturnType<typeof api.removeProject>>>();
  m.removeProject.mockReturnValue(deleted.promise);
  render(<App />);
  await user.click(await screen.findByText("会话A"));
  await screen.findByText("A content");
  fireEvent.click(document.querySelector(".group-more-btn")!);
  await user.click(screen.getByRole("menuitem", { name: /移除项目/ }));
  await user.click(screen.getByRole("button", { name: "确认移除" }));
  await user.click(screen.getByRole("button", { name: "取消" }));
  await user.click(sidebarRow("会话B"));
  await screen.findByText("B content");
  await act(async () => deleted.resolve({ deleted_sessions: 1, projects: [] }));
  expect(screen.getByText("B content")).toBeTruthy();
});
