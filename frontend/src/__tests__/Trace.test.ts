import { describe, expect, it } from "vitest";
import { currentTurn } from "../features/chat/chatStream";
import { toolStep, traceOf } from "../features/chat/trace";
import type { Item } from "../types";
import { tool } from "./helpers";

const ok = JSON.stringify({ status: "ok", path: "a.ts" });

describe("traceOf", () => {
  it("聚合本回合的工具调用并统计读取与搜索", () => {
    const trace = traceOf([
      { kind: "user", text: "go" },
      tool("1", "read_file", { path: "a.ts" }, ok),
      tool("2", "grep", { pattern: "x" }, ok),
      tool("3", "glob", { pattern: "*.ts" }, ok),
    ]);
    expect(trace.steps).toHaveLength(3);
    expect(trace.reads).toBe(2);
    expect(trace.searches).toBe(1);
    expect(trace.running).toBe(false);
  });

  it("未结束的工具算运行中", () => {
    const trace = traceOf([{ kind: "user", text: "go" }, tool("1", "read_file", { path: "a.ts" })]);
    expect(trace.running).toBe(true);
    expect(trace.steps[0].status).toBe("running");
  });

  it("结果为 error 的工具标记为失败", () => {
    const trace = traceOf([
      tool("1", "execute_shell", { command: "boom" }, JSON.stringify({ status: "error", message: "no" })),
    ]);
    expect(trace.steps[0].status).toBe("error");
    expect(trace.running).toBe(false);
  });

  it("非 JSON 结果按完成处理，并按工具名给出中文标签", () => {
    const step = toolStep({
      kind: "tool",
      id: "1",
      name: "execute_shell",
      args: { command: "ls -la" },
      result: "plain text",
      done: true,
    });
    expect(step.status).toBe("done");
    expect(step.label).toBe("Shell");
    expect(step.detail).toBe("ls -la");
  });

  it("未知工具沿用原始名称，不伪造标签", () => {
    const step = toolStep({ kind: "tool", id: "1", name: "mcp__x__y", args: {}, done: true });
    expect(step.label).toBe("mcp__x__y");
    expect(step.detail).toBe("");
  });

  it("只统计最后一个用户消息之后的工具", () => {
    const items: Item[] = [
      tool("old", "read_file", { path: "old.ts" }, ok),
      { kind: "user", text: "again" },
      tool("new", "read_file", { path: "new.ts" }, ok),
    ];
    expect(traceOf(currentTurn(items)).steps.map((s) => s.id)).toEqual(["new"]);
  });
});
