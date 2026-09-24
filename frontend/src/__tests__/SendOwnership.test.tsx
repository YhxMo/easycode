import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import App from "../App";
import * as api from "../api";
import { primeApiMock, session } from "./helpers";

// New streams are identified by their SSE session event. Navigation abandons old events.
vi.mock("../api", async () => (await import("./helpers")).apiMock);

const m = vi.mocked(api);

/**
 * A streamChat mock that captures `onEvent` and fails the test if a second
 * stream is started (each send should spawn exactly one stream).
 */
function controllableStream() {
  let onEvent: ((e: api.ChatEvent) => void) | undefined;
  let resolve!: () => void;
  const done = new Promise<void>((res) => {
    resolve = res;
  });
  m.streamChat.mockImplementation((_sid, _msg, cb, _opts) => {
    onEvent = cb;
    return done;
  });
  return {
    get: () => onEvent,
    /** Resolve the in-flight stream so send()'s finally block runs. */
    finish: () =>
      act(async () => {
        resolve();
      }),
  };
}

/** The foreground conversation title rendered in the chat header. */
function headerTitle(): string {
  return document.querySelector(".chat-context strong")?.textContent ?? "";
}

beforeEach(() => {
  primeApiMock(m);
  m.fetchSessions.mockResolvedValue([session("A", "会话A")]);
  m.fetchModels.mockResolvedValue({ default: "deepseek-v4flash", models: {}, providers: {}, limits: {} });
  m.fetchSession.mockResolvedValue({ ...session("A", "会话A"), messages: [] });
});

describe("App · 会话归属", () => {
  it("流进行中点「新会话」后，旧流的 finally 不得把前台 currentId 劫持回已完成的会话", async () => {
    const user = userEvent.setup();
    const stream = controllableStream();

    render(<App />);
    // Sidebar shows the existing session; foreground is still the blank
    // new session (currentId === null -> header "新会话").
    await screen.findByText("会话A");
    expect(headerTitle()).toBe("新会话");

    // Send from the new session (sessionId = null); stream stays in flight and
    // crucially has NOT emitted a "session" event yet.
    await user.type(screen.getByRole("textbox"), "hello");
    await user.click(screen.getByRole("button", { name: /发送消息/ }));
    await waitFor(() => expect(stream.get()).toBeTruthy());

    // Navigation invalidates the old stream even before its session event arrives.
    await user.click(screen.getByRole("button", { name: /新会话/ }));
    expect(headerTitle()).toBe("新会话");

    await act(async () => stream.get()?.({ type: "session", session_id: "A" }));
    await stream.finish();

    // The completed (old) stream must refresh the list but must NOT take over
    // the view: the user's blank session stays the foreground owner.
    await waitFor(() => expect(headerTitle()).toBe("新会话"));
    expect(document.querySelector(".session-item.active")).toBeNull();
  });

  it("新会话选择完全访问后，session 事件后仍保持，下一轮发送 permission_mode=allow-all", async () => {
    const user = userEvent.setup();
    m.fetchSessions.mockResolvedValue([]);
    const stream = controllableStream();

    render(<App />);
    await screen.findByRole("button", { name: /请求批准/ });

    await user.click(screen.getByRole("button", { name: /请求批准/ }));
    await user.click(screen.getByRole("menuitemradio", { name: /完全访问/ }));

    await user.type(screen.getByRole("textbox"), "first");
    await user.click(screen.getByRole("button", { name: /发送消息/ }));
    await waitFor(() => expect(stream.get()).toBeTruthy());

    await act(async () => stream.get()?.({ type: "session", session_id: "A" }));
    await stream.finish();

    // The created session inherits the draft's permission selection.
    await waitFor(() => expect(screen.getByRole("button", { name: /完全访问/ })).toBeTruthy());

    await user.type(screen.getByRole("textbox"), "second");
    await user.click(screen.getByRole("button", { name: /发送消息/ }));

    await waitFor(() => expect(m.streamChat).toHaveBeenCalledTimes(2));
    expect(m.streamChat).toHaveBeenLastCalledWith(
      "A",
      "second",
      expect.any(Function),
      expect.objectContaining({ permission_mode: "allow-all" }),
    );
  });

  it("新会话选择的次目录在 session 事件后仍显示", async () => {
    const user = userEvent.setup();
    m.fetchSessions.mockResolvedValue([{ ...session("old", "旧会话"), root: "/p" }]);
    m.fetchWorkspaces.mockResolvedValue({
      projects: [{ root: "/p", secondary: ["/s1", "/s2"] }],
    });
    const stream = controllableStream();

    render(<App />);
    await screen.findByText("旧会话");
    await user.click(screen.getByRole("button", { name: "在此项目下新建会话" }));
    expect(document.querySelector(".sec-toggle small")?.textContent).toContain("已连接 2 个目录");

    await user.type(screen.getByRole("textbox"), "go");
    await user.click(screen.getByRole("button", { name: /发送消息/ }));
    await waitFor(() => expect(stream.get()).toBeTruthy());

    await act(async () => stream.get()?.({ type: "session", session_id: "A" }));
    await stream.finish();

    await waitFor(() =>
      expect(document.querySelector(".sec-toggle small")?.textContent).toContain(
        "已连接 2 个目录",
      ),
    );
  });

  it("使用 SSE 返回的会话 id，而不是列表中的最新会话", async () => {
    const user = userEvent.setup();
    const stream = controllableStream();

    render(<App />);
    await screen.findByText("会话A");
    expect(headerTitle()).toBe("新会话");

    await user.type(screen.getByRole("textbox"), "bye");
    await user.click(screen.getByRole("button", { name: /发送消息/ }));
    await waitFor(() => expect(stream.get()).toBeTruthy());

    m.fetchSessions.mockResolvedValue([session("B", "另一个会话"), session("A", "会话A")]);
    await act(async () => stream.get()?.({ type: "session", session_id: "A" }));
    await stream.finish();

    await waitFor(() => expect(headerTitle()).toBe("会话A"));
    expect(document.querySelector(".session-item.active")?.textContent).toContain("会话A");
  });

  it("旧流延迟结束不清除新流的忙碌与停止状态", async () => {
    const user = userEvent.setup();
    m.fetchSessions.mockResolvedValue([session("A", "会话A"), session("B", "会话B")]);
    m.fetchSession.mockImplementation((id: string) =>
      Promise.resolve({ ...session(id, id === "A" ? "会话A" : "会话B"), messages: [] }),
    );
    const pending: Array<{ resolve: () => void }> = [];
    m.streamChat.mockImplementation(() => {
      let resolve!: () => void;
      const p = new Promise<void>((r) => {
        resolve = r;
      });
      pending.push({ resolve });
      return p;
    });

    render(<App />);
    await screen.findByText("会话A");
    await user.click(screen.getByText("会话A"));
    await user.type(screen.getByRole("textbox"), "first");
    await user.click(screen.getByRole("button", { name: /发送消息/ }));
    await waitFor(() => expect(pending.length).toBe(1));

    // Switch to B and start a second stream before the first settles.
    await user.click(screen.getByText("会话B"));
    await user.type(screen.getByRole("textbox"), "second");
    await user.click(screen.getByRole("button", { name: /发送消息/ }));
    await waitFor(() => expect(pending.length).toBe(2));

    // The stale first stream settles late; it must not clear B's busy state.
    await act(async () => {
      pending[0].resolve();
    });

    expect(screen.getByRole("button", { name: /停止生成/ })).toBeTruthy();
    expect(screen.queryByRole("button", { name: /发送消息/ })).toBeNull();
  });
});
