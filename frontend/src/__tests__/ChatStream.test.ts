import { describe, expect, it } from "vitest";
import { applyChatEvent } from "../chatStream";
import type { Item } from "../types";

describe("interrupted turns", () => {
  const completed: Item = {
    kind: "tool", id: "saved", name: "write_file", args: {},
    done: true, result: '{"status":"ok"}',
  };
  const running: Item = { kind: "tool", id: "running", name: "execute_shell", args: {}, done: false };

  it("preserves completed operations and stops the pending tool indicator", () => {
    const items = applyChatEvent([completed, running], { type: "cancelled" });
    expect(items[0]).toEqual(completed);
    expect(items[1]).toMatchObject({ done: true });
    if (items[1].kind !== "tool") throw new Error("missing tool");
    expect(JSON.parse(items[1].result!)).toMatchObject({ status: "error" });
    expect(items.at(-1)).toEqual({ kind: "notice", text: "已停止本轮，已执行的操作保留。" });
  });

  it("shows the provider error without leaving a tool running in the view", () => {
    const items = applyChatEvent([running], { type: "error", error: "connection closed" });
    expect(items[0]).toMatchObject({ done: true });
    expect(items.at(-1)).toEqual({ kind: "error", text: "connection closed" });
  });
});
