import { describe, expect, it } from "vitest";
import { paneData } from "../lib/pane";
import type { Item } from "../types";

const tool = (id: string, name: string, args: Record<string, unknown>, result?: string): Item => ({
  kind: "tool",
  id,
  name,
  args,
  result,
  done: true,
});

const json = (value: unknown) => JSON.stringify(value);

describe("paneData", () => {
  it("读取文件生成上下文卡片，带字符数", () => {
    const pane = paneData([
      tool("1", "read_file", { path: "src/app.ts" }, json({ status: "ok", path: "src/app.ts", chars: 42, content: "export const a = 1;" })),
    ]);
    expect(pane.context).toHaveLength(1);
    expect(pane.context[0].source).toBe("src/app.ts");
    expect(pane.context[0].title).toBe("app.ts");
    expect(pane.context[0].meta).toBe("42 字符");
    expect(pane.context[0].excerpt).toBe("export const a = 1;");
  });

  it("缺少 chars 时按内容长度展示", () => {
    const pane = paneData([
      tool("1", "read_file", { path: "a.ts" }, json({ status: "ok", path: "a.ts", content: "abcd" })),
    ]);
    expect(pane.context[0].meta).toBe("4 字符");
  });

  it("grep 生成搜索卡片，展示匹配数与首条命中", () => {
    const pane = paneData([
      tool(
        "1",
        "grep",
        { pattern: "flush" },
        json({ status: "ok", matches: [{ path: "src/bridge.py", line: 225, text: "flush()" }] }),
      ),
    ]);
    expect(pane.context[0].kind).toBe("search");
    expect(pane.context[0].source).toBe("flush");
    expect(pane.context[0].meta).toBe("1 处匹配");
    expect(pane.context[0].excerpt).toBe("src/bridge.py:225 flush()");
  });

  it("编辑生成变更卡片，没有 diff 的编辑不进入变更", () => {
    const pane = paneData([
      tool("1", "edit_file", { path: "a.ts" }, json({ status: "ok", path: "a.ts", diff: "@@ -1 +1 @@" })),
      tool("2", "write_file", { path: "b.ts" }, json({ status: "error", message: "nope" })),
    ]);
    expect(pane.changes).toHaveLength(1);
    expect(pane.changes[0].path).toBe("a.ts");
  });

  it("只统计本回合（最后一个用户消息之后）", () => {
    const items: Item[] = [
      tool("old", "read_file", { path: "old.ts" }, json({ status: "ok", path: "old.ts" })),
      { kind: "user", text: "again" },
      tool("new", "read_file", { path: "new.ts" }, json({ status: "ok", path: "new.ts" })),
    ];
    const pane = paneData(items.slice(items.findIndex((it) => it.kind === "user")));
    expect(pane.context.map((c) => c.source)).toEqual(["new.ts"]);
  });

  it("结果不是 JSON 时不影响其他卡片", () => {
    const pane = paneData([tool("1", "read_file", { path: "a.ts" }, "not json")]);
    expect(pane.context).toHaveLength(1);
    expect(pane.context[0].meta).toBe("0 字符");
  });
});
