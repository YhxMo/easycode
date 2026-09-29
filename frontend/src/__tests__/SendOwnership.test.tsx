import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import App from "../app/App";
import * as api from "../api";
import {
  activeTitle,
  composerField,
  controllableStream,
  primeApiMock,
  session,
  sidebarRow,
} from "./helpers";

// New streams are identified by their SSE session event. Navigation abandons old events.
vi.mock("../api", async () => (await import("./helpers")).apiMock);

const m = vi.mocked(api);

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
    const created = session("N", "新会话");
    m.createSession.mockResolvedValue(created);
    m.fetchSessions.mockResolvedValue([session("A", "会话A"), created]);
    m.fetchSession.mockImplementation((id: string) =>
      Promise.resolve({ ...session(id, id === "N" ? "新会话" : "会话A"), messages: [] }),
    );

    render(<App />);
    // Sidebar shows the existing session; foreground is still the start page.
    await screen.findByText("会话A");
    expect(activeTitle()).toBe("新会话");

    // Send from the start page (sessionId = null); the stream stays in flight
    // and crucially has NOT emitted a "session" event yet.
    await user.type(composerField(), "hello");
    await user.click(screen.getByRole("button", { name: /发送消息/ }));
    await waitFor(() => expect(stream.get()).toBeTruthy());

    // 「新会话」opens a real, still empty conversation of its own.
    await user.click(screen.getByRole("button", { name: "新建会话" }));
    await waitFor(() => expect(m.createSession).toHaveBeenCalled());
    expect(activeTitle()).toBe("新会话");

    await act(async () => stream.get()?.({ type: "session", session_id: "A" }));
    await stream.finish();

    // The completed (old) stream must refresh the list but must NOT take over
    // the view: the conversation the user just opened owns the foreground.
    await waitFor(() => expect(activeTitle()).toBe("新会话"));
    expect(document.querySelector(".session-item.active")?.textContent).toContain("新会话");
  });

  it("新会话选择完全访问后，session 事件后仍保持，下一轮发送 permission_mode=allow-all", async () => {
    const user = userEvent.setup();
    m.fetchSessions.mockResolvedValue([]);
    const stream = controllableStream();

    render(<App />);
    await screen.findByRole("button", { name: /请求批准/ });

    await user.click(screen.getByRole("button", { name: /请求批准/ }));
    await user.click(screen.getByRole("menuitemradio", { name: /完全访问/ }));
    await user.click(screen.getByRole("button", { name: "确认完全访问" }));

    await user.type(composerField(), "first");
    await user.click(screen.getByRole("button", { name: /发送消息/ }));
    await waitFor(() => expect(stream.get()).toBeTruthy());

    await act(async () => stream.get()?.({ type: "session", session_id: "A" }));
    await stream.finish();

    // The created session inherits the draft's permission selection.
    await waitFor(() => expect(screen.getByRole("button", { name: /完全访问/ })).toBeTruthy());

    await user.type(composerField(), "second");
    await user.click(screen.getByRole("button", { name: /发送消息/ }));

    await waitFor(() => expect(m.streamChat).toHaveBeenCalledTimes(2));
    expect(m.streamChat).toHaveBeenLastCalledWith(
      "A",
      "second",
      expect.any(Function),
      expect.objectContaining({ permission_mode: "allow-all" }),
    );
  });

  it("项目行新建的会话继承该项目的次目录，并在回合开始后仍显示", async () => {
    const user = userEvent.setup();
    const created = {
      ...session("N", "新会话"),
      root: "/p",
      secondary_roots: ["/s1", "/s2"],
    };
    m.fetchSessions.mockResolvedValue([{ ...session("old", "旧会话"), root: "/p" }, created]);
    m.fetchWorkspaces.mockResolvedValue({
      projects: [{ root: "/p", secondary: ["/s1", "/s2"] }],
    });
    m.fetchSession.mockImplementation((id: string) =>
      Promise.resolve(id === "N" ? { ...created, messages: [] } : { ...session(id, "旧会话"), messages: [] }),
    );
    m.createSession.mockResolvedValue(created);
    const stream = controllableStream();

    render(<App />);
    await screen.findByText("旧会话");
    await user.click(screen.getByRole("button", { name: "在此项目下新建会话" }));
    // The new conversation asks for this row's project; the server binds that
    // project's secondary directories, which is what comes back.
    await waitFor(() => expect(m.createSession).toHaveBeenCalledWith("/p", expect.any(String)));
    await waitFor(() =>
      expect(document.querySelector(".secondary-editor .dir-card small")?.textContent).toContain(
        "已连接 2 个目录",
      ),
    );

    await user.type(composerField(), "go");
    await user.click(screen.getByRole("button", { name: /发送消息/ }));
    await waitFor(() => expect(stream.get()).toBeTruthy());

    await act(async () => stream.get()?.({ type: "session", session_id: "N" }));
    await stream.finish();

    await waitFor(() =>
      expect(document.querySelector(".secondary-editor .dir-card small")?.textContent).toContain(
        "已连接 2 个目录",
      ),
    );
  });

  it("使用 SSE 返回的会话 id，而不是列表中的最新会话", async () => {
    const user = userEvent.setup();
    const stream = controllableStream();

    render(<App />);
    await screen.findByText("会话A");
    expect(activeTitle()).toBe("新会话");

    await user.type(composerField(), "bye");
    await user.click(screen.getByRole("button", { name: /发送消息/ }));
    await waitFor(() => expect(stream.get()).toBeTruthy());

    m.fetchSessions.mockResolvedValue([session("B", "另一个会话"), session("A", "会话A")]);
    await act(async () => stream.get()?.({ type: "session", session_id: "A" }));
    await stream.finish();

    await waitFor(() => expect(activeTitle()).toBe("会话A"));
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
    await user.click(sidebarRow("会话A"));
    await user.type(composerField(), "first");
    await user.click(screen.getByRole("button", { name: /发送消息/ }));
    await waitFor(() => expect(pending.length).toBe(1));

    // Switch to B and start a second stream before the first settles.
    await user.click(sidebarRow("会话B"));
    await user.type(composerField(), "second");
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
