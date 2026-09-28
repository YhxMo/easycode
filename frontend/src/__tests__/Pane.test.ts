import { describe, expect, it, vi } from "vitest";
import {
  MAX_HITS,
  fileRows,
  gitStateLabel,
  paneData,
  sessionFiles,
  sessionToolItems,
} from "../features/pane/pane";
import type { GitFileState } from "../api";
import type { Item, ToolItem } from "../types";
import type { Mock } from "vitest";
import fixtures from "./fixtures/toolResults.json";

// ``paneData`` runs on every render of the activity pane, so it must not pay
// for a JSON parse on results it has no card for. Counting the parse is the
// only way to see that from the outside — the cards themselves look identical.
vi.mock("../lib/toolResult", async (orig) => {
  const mod = await orig<typeof import("../lib/toolResult")>();
  return { ...mod, parseResult: vi.fn(mod.parseResult) };
});
const parseCounter = async () =>
  (await import("../lib/toolResult")).parseResult as unknown as ReturnType<typeof vi.fn>;

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

  it("完全访问下工作区外读取同样可以预览", () => {
    // 预览与工具的边界一致：完全访问的读取能打开宿主机路径，面板也给出入口，
    // 不再显示「工作区之外」的说明。
    const pane = paneData(
      [tool("1", "read_file", { path: "/tmp/other/note.txt" }, readOutside)],
      { fullAccess: true },
    );
    const card = pane.context[0];
    expect(card.previewTarget).toBe("/tmp/other/note.txt");
    expect(card.previewBlocked).toBeUndefined();
    expect(card.excerpt).toContain("external note");
    // 同样的记录在沙箱模式下仍然只给摘录：一张卡片不会因为预览放行就改口。
    const sandboxed = paneData([
      tool("1", "read_file", { path: "/tmp/other/note.txt" }, readOutside),
    ]);
    expect(sandboxed.context[0].previewTarget).toBeUndefined();
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

describe("会话记录与实时改动", () => {
  const toolItem = (
    id: string,
    name: string,
    args: Record<string, unknown>,
    result: string,
  ): ToolItem => ({ kind: "tool", id, name, args, result, done: true });

  it("记录按调用顺序排在前面，还没记录的调用接在后面", () => {
    const records = [
      toolItem("1", "read_file", { path: "a.ts" }, readOk),
      toolItem("2", "read_file", { path: "b.ts" }, readOk),
    ];
    const live = [
      tool("1", "read_file", { path: "a.ts" }, readOk),
      tool("3", "read_file", { path: "c.ts" }, readOk),
    ];
    const merged = sessionToolItems(records, live);
    expect(merged.map((it) => it.id)).toEqual(["1", "2", "3"]);
  });

  it("历史里被裁剪的结果由会话记录替代", () => {
    // Compaction leaves a stub behind; the record still has the diff, which is
    // the only reason the change card can be rebuilt after a refresh.
    const trimmed = tool(
      "2",
      "edit_file",
      { path: "src/app.ts" },
      JSON.stringify({ status: "ok", path: "src/app.ts" }),
    );
    const record = toolItem(
      "2",
      "edit_file",
      { path: "src/app.ts" },
      JSON.stringify({
        status: "ok",
        path: "src/app.ts",
        diff: "+x",
        absolute_path: "/ws/src/app.ts",
      }),
    );
    const merged = sessionToolItems([record], [trimmed]);
    expect(merged).toHaveLength(1);
    expect(JSON.parse(merged[0].result ?? "").diff).toBe("+x");
  });

  it("会话涉及的文件按最近使用排序，同名的两个根各自成条", () => {
    const pane = paneData([
      tool("1", "read_file", { path: "src/app.ts" }, readOk),
      tool("2", "read_file", { path: "/tmp/extra/src/app.ts" }, JSON.stringify({
        status: "ok",
        path: "src/app.ts",
        absolute_path: "/tmp/extra/src/app.ts",
        total_lines: 1,
        lines: 1,
        content: "1: x",
      })),
    ]);
    const files = sessionFiles(pane.context, pane.changes);
    expect(files.map((f) => f.target)).toEqual([
      "/tmp/extra/src/app.ts",
      "/tmp/ws/src/app.ts",
    ]);
    expect(files.map((f) => f.rootLabel)).toEqual(["extra", "ws"]);
    expect(files[0].reads).toBe(1);
  });

  it("glob 列出的整棵树不算这次会话涉及的文件", () => {
    const pane = paneData([tool("1", "glob", { pattern: "*.md" }, globOk)]);
    expect(sessionFiles(pane.context, pane.changes)).toEqual([]);
  });

  it("修改过的文件进入文件列表，并带上是哪一次改的", () => {
    const pane = paneData([tool("1", "edit_file", { path: "src/app.ts" }, editApplied)]);
    const files = sessionFiles(pane.context, pane.changes);
    expect(files).toHaveLength(1);
    expect(files[0].changes).toBe(1);
    // The result's own target, not the relative argument it was called with.
    expect(files[0].target).toBe("/tmp/ws/src/app.ts");
  });

  it("工作区改动与会话操作分开：只有 git 改动的文件排在后面", () => {
    const pane = paneData([tool("1", "read_file", { path: "src/app.ts" }, readOk)]);
    const files = sessionFiles(pane.context, pane.changes);
    const dirty: GitFileState[] = [
      {
        path: "src/app.ts",
        absolute_path: "/tmp/ws/src/app.ts",
        repo: "/tmp/ws",
        untracked: false,
        staged: "M",
        unstaged: "",
        exists: true,
      },
      {
        path: "notes.md",
        absolute_path: "/tmp/ws/notes.md",
        repo: "/tmp/ws",
        untracked: true,
        staged: "",
        unstaged: "",
        exists: true,
      },
    ];
    const rows = fileRows(files, dirty);
    expect(rows.map((r) => r.label)).toEqual(["src/app.ts", "notes.md"]);
    expect(rows[0].meta).toBe("读 1");
    // The state comes from the working tree, never from this conversation.
    expect(rows[0].git).toBe("已暂存修改");
    expect(rows[1].meta).toBe("");
    expect(rows[1].git).toBe("未跟踪");
    expect(rows[1].rootLabel).toBe("ws");
  });

  it("删除的文件不给预览入口，但仍报出它被删了", () => {
    const rows = fileRows([], [
      {
        path: "gone.ts",
        absolute_path: "/tmp/ws/gone.ts",
        repo: "/tmp/ws",
        untracked: false,
        staged: "",
        unstaged: "D",
        exists: false,
      },
    ]);
    expect(rows[0].target).toBeUndefined();
    expect(rows[0].git).toBe("未暂存删除");
  });

  it("未提交状态按已暂存与未暂存分别说明", () => {
    const base: GitFileState = {
      path: "a.ts",
      absolute_path: "/ws/a.ts",
      repo: "/ws",
      untracked: false,
      staged: "",
      unstaged: "",
      exists: true,
    };
    expect(gitStateLabel({ ...base, staged: "M", unstaged: "M" })).toBe("已暂存修改 · 未暂存修改");
    expect(gitStateLabel({ ...base, staged: "A" })).toBe("已暂存新增");
    expect(gitStateLabel(base)).toBe("已修改");
  });
});

describe("paneData · 只解析自己有卡片的工具", () => {
  it("其他工具的结果既不进面板，也不做 JSON 解析", async () => {
    const counted = await parseCounter();
    counted.mockClear();

    const fileTools = [
      tool("1", "read_file", { path: "src/app.ts" }, readOk),
      tool("2", "grep", { pattern: "export" }, grepOk),
      tool("3", "glob", { pattern: "*.md" }, globOk),
      tool("4", "edit_file", { path: "src/app.ts" }, editApplied),
    ];
    const others = [
      tool("5", "execute_shell", { command: "ls" }, JSON.stringify({ status: "ok", stdout: "x" })),
      tool("6", "mcp__x__y", {}, "plain text"),
      tool("7", "task", { prompt: "go" }, JSON.stringify({ status: "ok", message: "done" })),
    ];

    const withoutOthers = paneData(fileTools);
    expect(counted.mock.calls).toHaveLength(fileTools.length);

    counted.mockClear();
    const withOthers = paneData([...fileTools, ...others]);

    // The extra results change nothing about what the pane shows...
    expect(withOthers).toEqual(withoutOthers);
    // ...and they are never parsed: each file tool costs exactly one parse.
    expect(counted.mock.calls).toHaveLength(fileTools.length);
  });

  it("未完成与未返回结果的调用不解析", async () => {
    const counted = await parseCounter();
    counted.mockClear();
    paneData([
      tool("1", "read_file", { path: "a.ts" }, undefined, false),
      tool("2", "read_file", { path: "b.ts" }, undefined, true),
    ]);
    const mock = counted as unknown as Mock;
    expect(mock).not.toHaveBeenCalled();
  });
});
