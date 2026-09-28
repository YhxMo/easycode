import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import App from "../app/App";
import * as api from "../api";
import { composerField, detail, primeApiMock, session, sidebarRow } from "./helpers";

// Scroll ownership: streaming text pulls the view down only while the reader is
// already at the bottom. Reading history above the fold must not be yanked away.
vi.mock("../api", async () => (await import("./helpers")).apiMock);

const m = vi.mocked(api);

/** jsdom has no layout: give the pane a real geometry and record scroll writes. */
function stubScroller(pane: HTMLElement, scrollHeight: number, clientHeight: number) {
  const writes: number[] = [];
  let top = scrollHeight - clientHeight;
  Object.defineProperty(pane, "scrollHeight", { configurable: true, get: () => scrollHeight });
  Object.defineProperty(pane, "clientHeight", { configurable: true, get: () => clientHeight });
  Object.defineProperty(pane, "scrollTop", {
    configurable: true,
    get: () => top,
    set: (value: number) => {
      top = value;
      writes.push(value);
    },
  });
  return { writes, position: () => top };
}

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
  m.fetchSessions.mockResolvedValue([session("A", "会话A")]);
  m.fetchSession.mockResolvedValue(detail("A", "会话A", []));
  m.fetchModels.mockResolvedValue({ default: "m", models: {}, providers: {}, limits: {} });
});

async function startTurn() {
  const user = userEvent.setup();
  const streams = captureStreams();
  render(<App />);
  await screen.findByText("会话A");
  await user.click(sidebarRow("会话A"));
  await user.type(composerField(), "go");
  await user.click(screen.getByRole("button", { name: /发送消息/ }));
  await waitFor(() => expect(streams.length).toBe(1));
  const pane = document.querySelector(".chat-main") as HTMLElement;
  return { user, streams, pane };
}

describe("App · 消息流跟随", () => {
  it("用户往上阅读时新内容不再把视图拉到底部", async () => {
    const { streams, pane } = await startTurn();
    const scroller = stubScroller(pane, 2000, 600);

    // The reader scrolls up into the history.
    pane.scrollTop = 200;
    scroller.writes.length = 0;
    await act(async () => {
      pane.dispatchEvent(new Event("scroll"));
    });

    await act(async () => streams[0].onEvent({ type: "text", content: "新的内容" }));
    await waitFor(() => expect(screen.getByText(/回到最新/)).toBeTruthy());
    expect(scroller.position()).toBe(200);
    expect(scroller.writes).toEqual([]);
  });

  it("回到最新按钮把视图带回底部并恢复跟随", async () => {
    const { user, streams, pane } = await startTurn();
    const scroller = stubScroller(pane, 2000, 600);
    pane.scrollTop = 200;
    await act(async () => {
      pane.dispatchEvent(new Event("scroll"));
    });
    await act(async () => streams[0].onEvent({ type: "text", content: "新的内容" }));

    await user.click(screen.getByRole("button", { name: /回到最新/ }));
    await waitFor(() => expect(scroller.position()).toBe(2000));
    expect(screen.queryByText(/回到最新/)).toBeNull();
    scroller.writes.length = 0;

    // Following again: the next chunk keeps the view at the bottom.
    await act(async () => streams[0].onEvent({ type: "text", content: "更多" }));
    await waitFor(() => expect(scroller.position()).toBe(2000));
  });

  it("发送自己的消息会回到跟随状态", async () => {
    const { user, streams, pane } = await startTurn();
    const scroller = stubScroller(pane, 2000, 600);
    pane.scrollTop = 100;
    await act(async () => {
      pane.dispatchEvent(new Event("scroll"));
    });
    await act(async () => streams[0].onEvent({ type: "text", content: "回复" }));
    await screen.findByText(/回到最新/);

    await act(async () => streams[0].resolve());
    await user.type(composerField(), "再来一轮");
    await user.click(screen.getByRole("button", { name: /发送消息/ }));

    await waitFor(() => expect(scroller.position()).toBe(2000));
    expect(screen.queryByText(/回到最新/)).toBeNull();
  });
});
