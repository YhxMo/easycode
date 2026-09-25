import { describe, expect, it } from "vitest";
import { MAX_HITS, paneData } from "../lib/pane";
import type { Item } from "../types";
import fixtures from "./fixtures/toolResults.json";

// The samples come from the real Python tools; tests/test_tool_result_contract.py
// regenerates them and fails when the shapes drift, so these cards assert
// against structures the backend actually produces.
const readOk = JSON.stringify(fixtures.read_file.ok);
const readError = JSON.stringify(fixtures.read_file.error);
const readOutside = JSON.stringify(fixtures.read_file.outside);
const grepOk = JSON.stringify(fixtures.grep.ok);
const grepSecondary = JSON.stringify(fixtures.grep.secondary);
const grepEmpty = JSON.stringify(fixtures.grep.empty);
const globOk = JSON.stringify(fixtures.glob.ok);
const globSecondary = JSON.stringify(fixtures.glob.secondary);
const globEmpty = JSON.stringify(fixtures.glob.empty);
const editApplied = JSON.stringify(fixtures.edit_file.applied);
const editDryRun = JSON.stringify(fixtures.edit_file.dry_run);
const editError = JSON.stringify(fixtures.edit_file.error);

const tool = (
  id: string,
  name: string,
  args: Record<string, unknown>,
  result?: string,
  done = true,
): Item => ({ kind: "tool", id, name, args, result, done });

describe("paneData · 读取", () => {
  it("文件卡片读取真实结果，并带上唯一目标", () => {
    const pane = paneData([tool("1", "read_file", { path: "src/app.ts" }, readOk)]);
    expect(pane.context).toHaveLength(1);
    const card = pane.context[0];
    expect(card.title).toBe("app.ts");
    // `chars` counts the tool's own formatting (line numbers + footer), so the
    // card shows the file's line counts instead.
    expect(card.meta).toBe("2/2 行");
    expect(card.status).toBe("ok");
    // the preview target is the explicit file, not the root-relative display path
    expect(card.previewTarget).toBe("/tmp/ws/src/app.ts");
    // which root the relative path belongs to
    expect(card.rootLabel).toBe("ws");
  });

  it("同一相对路径在不同目录下靠所属根目录区分", () => {
    const secondary = JSON.stringify({
      status: "ok",
      path: "src/app.ts",
      absolute_path: "/tmp/extra/src/app.ts",
      in_allowed: true,
      start_line: 1,
      end_line: 1,
      total_lines: 1,
      truncated: false,
      lines: 1,
      chars: 8,
      content: "1: x\n\n(End of file - total 1 lines)",
    });
    const pane = paneData([
      tool("1", "read_file", { path: "src/app.ts" }, readOk),
      tool("2", "read_file", { path: "/tmp/extra/src/app.ts" }, secondary),
    ]);
    expect(pane.context.map((c) => [c.title, c.rootLabel])).toEqual([
      ["app.ts", "ws"],
      ["app.ts", "extra"],
    ]);
  });

  it("工作区外的读取保留摘录，但不提供注定失败的预览入口", () => {
    const pane = paneData([
      tool("1", "read_file", { path: "/tmp/other/note.txt" }, readOutside),
    ]);
    const card = pane.context[0];
    expect(card.status).toBe("ok");
    expect(card.previewTarget).toBeUndefined();
    expect(card.previewBlocked).toContain("工作区之外");
    expect(card.excerpt).toContain("external note");
    // no root to name: the file belongs to none of this session's roots
    expect(card.rootLabel).toBeUndefined();
  });

  it("没有 absolute_path 的历史结果回退到绝对工具参数", () => {
    const result = JSON.stringify({ status: "ok", path: "src/app.ts", chars: 3, content: "abc" });
    const pane = paneData([tool("1", "read_file", { path: "/ws/src/app.ts" }, result)]);
    expect(pane.context[0].previewTarget).toBe("/ws/src/app.ts");
  });

  it("失败结果标记为错误，并且不提供可能打开错文件的入口", () => {
    const pane = paneData([tool("1", "read_file", { path: "src/nope.ts" }, readError)]);
    const card = pane.context[0];
    expect(card.status).toBe("error");
    expect(card.meta).toBe("失败");
    expect(card.excerpt).toBe("not a file: src/nope.ts");
    expect(card.previewBlocked).toBeUndefined();
  });

  it("失败的外部读取不会因为路径是绝对的而报成无法预览", () => {
    const pane = paneData([
      tool("1", "read_file", { path: "/tmp/other/nope.txt" }, readError),
    ]);
    expect(pane.context[0].status).toBe("error");
    expect(pane.context[0].previewBlocked).toBeUndefined();
  });

  it("还没返回结果的调用不算已获得上下文", () => {
    const pane = paneData([tool("1", "read_file", { path: "src/app.ts" }, undefined, false)]);
    expect(pane.context).toHaveLength(0);
  });

  it("结果不是 JSON 时不影响其他卡片", () => {
    const pane = paneData([tool("1", "read_file", { path: "a.ts" }, "not json")]);
    expect(pane.context).toHaveLength(1);
    expect(pane.context[0].meta).toBe("");
    expect(pane.context[0].status).toBe("empty");
  });
});

describe("paneData · 搜索", () => {
  it("grep 按真实命中生成片段，每行指向它自己的文件", () => {
    const pane = paneData([tool("1", "grep", { pattern: "export" }, grepOk)]);
    const card = pane.context[0];
    expect(card.kind).toBe("search");
    // the pattern is a label, never the preview target
    expect(card.sourceLabel).toBe("export");
    expect(card.previewTarget).toBeUndefined();
    expect(card.meta).toBe("2 处匹配");
    expect(card.hits).toEqual([
      { target: "src/app.ts", label: "src/app.ts", text: "export const a = 1;", line: 1 },
      { target: "src/app.ts", label: "src/app.ts", text: "export const b = 2;", line: 2 },
    ]);
  });

  it("次目录命中用 root 拼出绝对目标，并标出所属目录", () => {
    const pane = paneData([tool("1", "grep", { pattern: "hello" }, grepSecondary)]);
    expect(pane.context[0].hits?.[0].target).toBe("/tmp/extra/note.md");
    expect(pane.context[0].hits?.[0].rootLabel).toBe("extra");
  });

  it("没有命中显示空结果，而不是搜索模式本身", () => {
    const pane = paneData([tool("1", "grep", { pattern: "nothing-here" }, grepEmpty)]);
    const card = pane.context[0];
    expect(card.status).toBe("empty");
    expect(card.meta).toBe("0 处匹配");
    expect(card.hits).toEqual([]);
    expect(card.previewTarget).toBeUndefined();
  });

  it("命中过多时只列出一部分", () => {
    const matches = Array.from({ length: MAX_HITS + 3 }, (_, i) => ({
      file: "src/app.ts",
      line: i + 1,
      text: `line ${i + 1}`,
    }));
    const pane = paneData([
      tool("1", "grep", { pattern: "x" }, JSON.stringify({ status: "ok", matches })),
    ]);
    expect(pane.context[0].hits).toHaveLength(MAX_HITS + 3);
    expect(pane.context[0].meta).toBe(`${MAX_HITS + 3} 处匹配`);
  });
});

describe("paneData · 文件列表", () => {
  it("glob 字符串结果属于主目录", () => {
    const pane = paneData([tool("1", "glob", { pattern: "*.md" }, globOk)]);
    const card = pane.context[0];
    expect(card.title).toBe("文件列表");
    expect(card.meta).toBe("2 个文件");
    expect(card.hits?.map((h) => h.target)).toEqual(["README.md", "src/app.ts"]);
  });

  it("glob 对象结果带上自己的 root", () => {
    const pane = paneData([tool("1", "glob", { pattern: "*.md" }, globSecondary)]);
    expect(pane.context[0].hits?.map((h) => h.target)).toEqual([
      "README.md",
      "/tmp/extra/note.md",
    ]);
  });

  it("没有匹配的文件列表是空结果", () => {
    const pane = paneData([tool("1", "glob", { pattern: "*.rs" }, globEmpty)]);
    expect(pane.context[0].status).toBe("empty");
    expect(pane.context[0].meta).toBe("0 个文件");
  });
});

describe("paneData · 变更", () => {
  it("已应用的编辑进入变更", () => {
    const pane = paneData([tool("1", "edit_file", { path: "src/app.ts" }, editApplied)]);
    expect(pane.changes).toHaveLength(1);
    expect(pane.changes[0].applied).toBe(true);
  });

  it("dry_run 只是补丁预览，标注为未应用", () => {
    const pane = paneData([tool("1", "edit_file", { path: "src/app.ts" }, editDryRun)]);
    expect(pane.changes).toHaveLength(1);
    expect(pane.changes[0].applied).toBe(false);
  });

  it("失败的编辑不显示成成功变更", () => {
    const pane = paneData([tool("1", "edit_file", { path: "src/app.ts" }, editError)]);
    expect(pane.changes).toHaveLength(0);
    const write = paneData([
      tool("2", "write_file", { path: "b.ts" }, JSON.stringify({ status: "error", message: "x" })),
    ]);
    expect(write.changes).toHaveLength(0);
  });
});
