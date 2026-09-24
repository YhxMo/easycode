import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import App from "../App";
import * as api from "../api";
import { detail, primeApiMock, session, sidebarRow } from "./helpers";

// Concurrency contract: a turn owns its own conversation slot. Switching the
// foreground away neither interrupts it nor lets its output leak into another
// conversation.
vi.mock("../api", async () => (await import("./helpers")).apiMock);

const m = vi.mocked(api);

/** Captures every started stream so a test can drive them independently. */
function captureStreams() {
  const calls: Array<{ onEvent: (e: api.ChatEvent) => void; resolve: () => void }> = [];
  m.streamChat.mockImplementation((_sid, _msg, cb) => {
    let resolve!: () => void;
    const done = new Promise<void>((res) => {
      resolve = res;
    });
    calls.push({ onEvent: cb, resolve });
    return done;
  });
  return calls;
}

beforeEach(() => {
  primeApiMock(m);
  m.fetchSessions.mockResolvedValue([session("A", "会话A"), session("B", "会话B")]);
  m.fetchModels.mockResolvedValue({
    default: "deepseek-v4flash",
    models: {},
    providers: {},
    limits: {},
  });
  m.fetchSession.mockImplementation((id: string) =>
    Promise.resolve(detail(id, id === "A" ? "会话A" : "会话B", [])),
  );
});

describe("App · 并发流", () => {
  it("切到其他会话不会中断后台流，切回来能看到它的输出", async () => {
    const user = userEvent.setup();
    const streams = captureStreams();

    render(<App />);
    await screen.findByText("会话A");
    await user.click(sidebarRow("会话A"));
    await user.type(screen.getByRole("textbox"), "first");
    await user.click(screen.getByRole("button", { name: /发送消息/ }));
    await waitFor(() => expect(streams.length).toBe(1));

    // Leaving the conversation must not cancel the turn.
    await user.click(sidebarRow("会话B"));
    expect(m.cancelSessionChat).not.toHaveBeenCalled();

    // The background turn keeps producing output while B is in the foreground.
    await act(async () => streams[0].onEvent({ type: "text", content: "后台仍在输出" }));
    expect(screen.queryByText("后台仍在输出")).toBeNull();

    await user.click(sidebarRow("会话A"));
    // Switching back re-reads the session detail, but a live turn owns its
    // items: the streamed reply must survive the reload.
    await screen.findByText("后台仍在输出");
    expect(m.streamChat).toHaveBeenCalledTimes(1);
  });

  it("两个会话可以同时输出，互不污染对方的视图", async () => {
    const user = userEvent.setup();
    const streams = captureStreams();

    render(<App />);
    await screen.findByText("会话A");
    await user.click(sidebarRow("会话A"));
    await user.type(screen.getByRole("textbox"), "first");
    await user.click(screen.getByRole("button", { name: /发送消息/ }));
    await waitFor(() => expect(streams.length).toBe(1));

    // A second conversation starts its own turn while the first is still open.
    await user.click(sidebarRow("会话B"));
    await user.type(screen.getByRole("textbox"), "second");
    await user.click(screen.getByRole("button", { name: /发送消息/ }));
    await waitFor(() => expect(streams.length).toBe(2));

    await act(async () => streams[0].onEvent({ type: "text", content: "只属于A" }));
    await act(async () => streams[1].onEvent({ type: "text", content: "只属于B" }));

    expect(screen.getByText("只属于B")).toBeTruthy();
    expect(screen.queryByText("只属于A")).toBeNull();
    // Both turns are still running: the foreground keeps its stop affordance.
    expect(screen.getByRole("button", { name: /停止生成/ })).toBeTruthy();
  });

  it("前台会话结束后，后台会话仍保持运行状态", async () => {
    const user = userEvent.setup();
    const streams = captureStreams();

    render(<App />);
    await screen.findByText("会话A");
    await user.click(sidebarRow("会话A"));
    await user.type(screen.getByRole("textbox"), "first");
    await user.click(screen.getByRole("button", { name: /发送消息/ }));
    await waitFor(() => expect(streams.length).toBe(1));

    await user.click(sidebarRow("会话B"));
    await user.type(screen.getByRole("textbox"), "second");
    await user.click(screen.getByRole("button", { name: /发送消息/ }));
    await waitFor(() => expect(streams.length).toBe(2));

    // The foreground turn ends; the background one must not be torn down with it.
    await act(async () => streams[1].resolve());
    await user.click(sidebarRow("会话A"));
    expect(screen.getByRole("button", { name: /停止生成/ })).toBeTruthy();

    await act(async () => streams[0].resolve());
    await waitFor(() => expect(screen.getByRole("button", { name: /发送消息/ })).toBeTruthy());
  });
});
