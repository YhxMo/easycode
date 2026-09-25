// 右侧面板在窄布局下是覆盖式抽屉：不自行打开、Escape 关闭、焦点回到开关。
import { act, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "../App";
import * as api from "../api";
import { composerField, detail, primeApiMock, session, sidebarRow } from "./helpers";

vi.mock("../api", async () => (await import("./helpers")).apiMock);

const m = vi.mocked(api);

/** jsdom 没有布局：把聊天区宽度和 ResizeObserver 交给测试控制。 */
function installChatWidth(width: number) {
  class ResizeObserverStub {
    constructor(private readonly cb: ResizeObserverCallback) {}
    observe() {
      this.cb([], this as unknown as ResizeObserver);
    }
    unobserve() {}
    disconnect() {}
  }
  vi.stubGlobal("ResizeObserver", ResizeObserverStub);
  Object.defineProperty(HTMLElement.prototype, "clientWidth", {
    configurable: true,
    get: () => width,
  });
}

const pane = () => document.querySelector(".right-pane");
const paneToggle = () => screen.getByRole("button", { name: /面板/ });

beforeEach(() => {
  primeApiMock(m);
  m.fetchSessions.mockResolvedValue([session("s1", "会话A")]);
  m.fetchSession.mockResolvedValue(detail("s1", "会话A", []));
  m.fetchModels.mockResolvedValue({ default: "d", models: {}, providers: {}, limits: {} });
});

afterEach(() => {
  vi.unstubAllGlobals();
  delete (HTMLElement.prototype as { clientWidth?: number }).clientWidth;
});

/** A turn that reads a file, which is what makes the pane want to open. */
async function readOneFile(onEvent: (e: api.ChatEvent) => void) {
  const tool_call = { id: "t1", name: "read_file", arguments: { path: "src/app.ts" } };
  await act(async () => onEvent({ type: "tool_start", tool_call }));
  await act(async () =>
    onEvent({
      type: "tool_result",
      tool_call,
      result: JSON.stringify({
        status: "ok",
        path: "src/app.ts",
        absolute_path: "/p/src/app.ts",
        in_allowed: true,
        start_line: 1,
        end_line: 1,
        total_lines: 1,
        truncated: false,
        lines: 1,
        chars: 3,
        content: "1: x",
      }),
    }),
  );
}

describe("右侧面板 · 窄布局抽屉", () => {
  it("窄布局下不会自行打开面板盖住回复，手动展开后 Escape 关闭并交还焦点", async () => {
    installChatWidth(950 - 1);
    const user = userEvent.setup();
    let onEvent: ((e: api.ChatEvent) => void) | undefined;
    m.streamChat.mockImplementation((_sid, _msg, cb) => {
      onEvent = cb;
      return new Promise<void>(() => {});
    });
    render(<App />);
    await screen.findByText("会话A");
    await user.click(sidebarRow("会话A"));
    await user.type(composerField(), "看看这个文件");
    await user.click(screen.getByRole("button", { name: /发送消息/ }));
    await readOneFile(onEvent!);

    // 这一轮确实产生了上下文，但窄布局下面板不自己弹出
    expect(pane()).toBeNull();

    await user.click(paneToggle());
    expect(pane()).not.toBeNull();
    // 抽屉盖住了对话，键盘就该落在它自己的控件上
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "关闭面板" }));

    fireEvent.keyDown(document.body, { key: "Escape" });
    expect(pane()).toBeNull();
    expect(document.activeElement).toBe(paneToggle());
    expect(screen.getByRole("button", { name: "展开面板" })).toBeTruthy();
  });

  it("宽布局并排显示，Escape 不关面板", async () => {
    installChatWidth(1200);
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText("会话A");
    await user.click(sidebarRow("会话A"));

    await user.click(paneToggle());
    expect(pane()).not.toBeNull();

    fireEvent.keyDown(document.body, { key: "Escape" });
    expect(pane()).not.toBeNull();
  });
});
