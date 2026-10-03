import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ChatMessages } from "../features/chat/ChatMessages";
import { applyChatEvent } from "../features/chat/chatStream";
import { traceOf } from "../features/chat/trace";
import type { Item } from "../types";
import { tool } from "./helpers";

vi.mock("../features/chat/trace", async (original) => {
  const mod = await original<typeof import("../features/chat/trace")>();
  return { ...mod, traceOf: vi.fn(mod.traceOf) };
});

const props = {
  busy: true,
  currentModelName: "fake/model",
  onDecide: vi.fn(),
  onOpenTasks: vi.fn(),
  onContinue: vi.fn(),
};

beforeEach(() => vi.clearAllMocks());

describe("ChatMessages · 工具轨迹复用", () => {
  it("文本连续增长不重建历史轨迹，保留展开状态", async () => {
    const user = userEvent.setup();
    const raw = JSON.stringify({ status: "ok", content: "工具内容" });
    let items: Item[] = [
      { kind: "user", text: "go" },
      tool("1", "read_file", { path: "a.ts" }, raw),
      { kind: "assistant", text: "文本" },
    ];
    const view = render(<ChatMessages {...props} items={items} />);
    await user.click(screen.getByRole("button", { name: "展开工具调用" }));
    await user.click(screen.getByRole("button", { name: /读取\s*a\.ts/ }));
    vi.mocked(traceOf).mockClear();
    for (let i = 0; i < 5; i++) {
      items = applyChatEvent(items, { type: "text", content: `${i}` });
      view.rerender(<ChatMessages {...props} items={items} />);
    }
    expect(traceOf).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "收起工具调用" }).getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByText(/工具内容/)).toBeTruthy();
    expect(screen.getByText("文本01234")).toBeTruthy();
  });

  it("工具返回后更新状态，停止时更新仍在执行的步骤", () => {
    let items: Item[] = [
      { kind: "user", text: "go" },
      tool("1", "read_file", { path: "a.ts" }),
    ];
    const view = render(<ChatMessages {...props} items={items} />);
    expect(view.container.querySelector(".trace.running")).toBeTruthy();
    vi.mocked(traceOf).mockClear();
    items = applyChatEvent(items, {
      type: "tool_result",
      tool_call: { id: "1", name: "read_file", arguments: { path: "a.ts" } },
      result: JSON.stringify({ status: "error", message: "missing" }),
    });
    view.rerender(<ChatMessages {...props} items={items} />);
    expect(traceOf).toHaveBeenCalledTimes(1);
    expect(vi.mocked(traceOf).mock.results[0].value.steps[0].status).toBe("error");
    expect(view.container.querySelector(".trace.running")).toBeNull();
    items = [...items, tool("2", "execute_shell", { command: "wait" })];
    view.rerender(<ChatMessages {...props} items={items} />);
    expect(view.container.querySelector(".trace.running")).toBeTruthy();
    items = applyChatEvent(items, { type: "cancelled" });
    view.rerender(<ChatMessages {...props} items={items} />);
    expect(view.container.querySelector(".trace.running")).toBeNull();
    expect(screen.getByText("已停止本轮，已执行的操作保留。")).toBeTruthy();
  });
});
