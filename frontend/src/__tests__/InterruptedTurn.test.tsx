// 被服务端终止的回合：错误留在正确的位置，并提供只填草稿的继续入口。
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import App from "../App";
import * as api from "../api";
import { composerField, detail, primeApiMock, session, sidebarRow } from "./helpers";

vi.mock("../api", async () => (await import("./helpers")).apiMock);

const m = vi.mocked(api);

beforeEach(() => {
  primeApiMock(m);
  m.fetchSessions.mockResolvedValue([session("s1", "会话A")]);
  m.fetchSession.mockResolvedValue(detail("s1", "会话A", []));
  m.fetchModels.mockResolvedValue({ default: "d", models: {}, providers: {}, limits: {} });
});

/** `follows(a, b)` — a 出现在 b 之后。 */
function follows(later: HTMLElement, earlier: HTMLElement) {
  return Boolean(later.compareDocumentPosition(earlier) & Node.DOCUMENT_POSITION_PRECEDING);
}

describe("App · 未完成的回合", () => {
  it("触发轮数上限后给出继续入口，只填草稿不自动发送", async () => {
    const user = userEvent.setup();
    m.fetchSessions.mockResolvedValue([]);
    m.streamChat.mockImplementation(async (_sid, _msg, onEvent) => {
      onEvent({
        type: "error",
        error: "已达到本轮的模型调用轮数上限（max_tool_iterations=3）",
        code: "tool_iteration_limit",
      });
    });

    render(<App />);
    const box = composerField();
    await user.type(box, "做个多步任务");
    await user.click(screen.getByRole("button", { name: /发送消息/ }));

    const entry = await screen.findByRole("button", { name: "继续未完成任务" });
    await user.click(entry);

    expect(composerField().value).toContain("继续完成上一轮未完成的任务");
    expect(composerField().value).toContain("核对工作区");
    // 只填草稿：没有第二次请求
    expect(m.streamChat).toHaveBeenCalledTimes(1);
  });

  it("已经写了草稿时不覆盖用户输入", async () => {
    const user = userEvent.setup();
    m.fetchSessions.mockResolvedValue([]);
    m.streamChat.mockImplementation(async (_sid, _msg, onEvent) => {
      onEvent({ type: "error", error: "连接失败", code: "provider_error" });
    });
    render(<App />);
    await user.type(composerField(), "先问个问题");
    await user.click(screen.getByRole("button", { name: /发送消息/ }));

    await user.type(composerField(), "我在写的下一句");
    await user.click(await screen.findByRole("button", { name: "继续未完成任务" }));

    expect(composerField().value).toBe("我在写的下一句");
  });

  it("普通错误（无错误代码）不提供继续入口", async () => {
    const user = userEvent.setup();
    m.fetchSessions.mockResolvedValue([]);
    m.streamChat.mockImplementation(async (_sid, _msg, onEvent) => {
      onEvent({ type: "error", error: "连接中断" });
    });
    render(<App />);
    await user.type(composerField(), "hi");
    await user.click(screen.getByRole("button", { name: /发送消息/ }));

    await screen.findByText("连接中断");
    expect(screen.queryByRole("button", { name: "继续未完成任务" })).toBeNull();
  });

  it("恢复会话时错误回到它结束的那个回合", async () => {
    const user = userEvent.setup();
    m.fetchSession.mockResolvedValue({
      ...detail("s1", "会话A", [
        { role: "user", content: "第一条" },
        { role: "assistant", content: "第一次回复" },
        { role: "user", content: "第二条" },
        { role: "assistant", content: "第二次回复" },
      ]),
      user_times: ["2026-01-01T00:00:01Z", "2026-01-01T00:00:02Z"],
      turn_failures: [
        { time: "2026-01-01T00:00:02Z", message: "达到轮数上限", code: "tool_iteration_limit" },
      ],
    });

    render(<App />);
    await screen.findByText("会话A");
    await user.click(sidebarRow("会话A"));

    const error = await screen.findByText("达到轮数上限");
    // 第一轮的回复仍在错误之前，第二条提问仍在错误之前
    expect(follows(error, screen.getByText("第二次回复"))).toBe(true);
    expect(follows(error, screen.getByText("第二条"))).toBe(true);
    // 它不属于第一轮
    expect(follows(error, screen.getByText("第一次回复"))).toBe(true);
    // 恢复出来的错误同样可以继续
    expect(screen.getByRole("button", { name: "继续未完成任务" })).toBeTruthy();
  });

  it("旧会话没有失败记录时照常恢复", async () => {
    const user = userEvent.setup();
    m.fetchSession.mockResolvedValue({
      ...detail("s1", "会话A", [{ role: "user", content: "旧消息" }]),
      user_times: ["2026-01-01T00:00:01Z"],
    });

    render(<App />);
    await screen.findByText("会话A");
    await user.click(sidebarRow("会话A"));

    await screen.findByText("旧消息");
    expect(screen.queryByRole("button", { name: "继续未完成任务" })).toBeNull();
  });
});
