import { describe, expect, it } from "vitest";
import { applyChatEvent, expirePending } from "../chatStream";
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

describe("expirePending", () => {
  it("只把 pending 审批置为过期", () => {
    const items: Item[] = [
      { kind: "approval", id: "a", toolCallId: "t1", name: "x", args: {}, state: "pending" },
      { kind: "approval", id: "b", toolCallId: "t2", name: "y", args: {}, state: "approved" },
      { kind: "user", text: "hi" },
    ];
    const out = expirePending(items);
    expect(out[0]).toMatchObject({ state: "expired" });
    expect(out[1]).toMatchObject({ state: "approved" });
    expect(out[2]).toEqual(items[2]);
  });
});

describe("done 收尾", () => {
  it("有工具调用也有文字回复（review 在最后）时不追加未回复提示", () => {
    const items: Item[] = [
      { kind: "user", text: "go" },
      { kind: "tool", id: "t1", name: "write_file", args: {}, done: true, result: "ok" },
      { kind: "assistant", text: "ok" },
      { kind: "review", text: "{}" },
    ];
    const out = applyChatEvent(items, { type: "done" });
    expect(out.some((it) => it.kind === "notice")).toBe(false);
    expect(out).toEqual(items);
  });

  it("error 之后的 done 不再追加矛盾的完成提示", () => {
    const items: Item[] = [
      { kind: "user", text: "go" },
      { kind: "tool", id: "t1", name: "execute_shell", args: {}, done: true, result: "ok" },
    ];
    const afterError = applyChatEvent(items, { type: "error", error: "boom" });
    const afterDone = applyChatEvent(afterError, { type: "done" });
    expect(afterDone.at(-1)).toEqual({ kind: "error", text: "boom" });
  });
});
