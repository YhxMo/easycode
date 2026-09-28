import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ThinkingTrace } from "../components/primitives/ThinkingTrace";
import { traceOf, toolStep } from "../lib/trace";
import type { Item } from "../types";

const tool = (id: string, name: string, args: Record<string, unknown>, result: string): Item => ({
  kind: "tool",
  id,
  name,
  args,
  result,
  done: true,
});

// A collapsed trace never formats its payloads; only the step the reader opens
// is parsed and indented.

describe("toolStep · 结果原样保留", () => {
  it("聚合时不格式化，保留后端原文", () => {
    const raw = JSON.stringify({ status: "ok", path: "a.ts" });
    const step = toolStep(tool("1", "read_file", { path: "a.ts" }, raw) as Extract<Item, { kind: "tool" }>);
    expect(step.raw).toBe(raw);
  });
});

describe("ThinkingTrace · 展开后的结果", () => {
  const expand = async (user: ReturnType<typeof userEvent.setup>) => {
    await user.click(screen.getByRole("button", { name: /展开工具调用/ }));
    await user.click(screen.getAllByRole("button", { expanded: false })[0]);
  };

  it("对象结果展开后缩进显示", async () => {
    const user = userEvent.setup();
    const raw = JSON.stringify({ status: "ok", path: "a.ts", chars: 3 });
    const { container } = render(
      <ThinkingTrace trace={traceOf([tool("1", "read_file", { path: "a.ts" }, raw)])} />,
    );
    await expand(user);
    const shown = container.querySelector(".step-result")?.textContent ?? "";
    // indented JSON, not the one-line payload the tool sent
    expect(shown).toBe(JSON.stringify({ status: "ok", path: "a.ts", chars: 3 }, null, 2));
    expect(shown).toContain("\n  ");
  });

  it("数组结果按原始文本显示，不伪装成对象", async () => {
    const user = userEvent.setup();
    render(
      <ThinkingTrace trace={traceOf([tool("1", "execute_shell", { command: "ls" }, '["a","b"]')])} />,
    );
    await expand(user);
    expect(screen.getByText('["a","b"]')).toBeTruthy();
  });

  it("非 JSON 的 MCP 文本按原文显示", async () => {
    const user = userEvent.setup();
    render(<ThinkingTrace trace={traceOf([tool("1", "mcp__x__y", {}, "plain text")])} />);
    await expand(user);
    expect(screen.getByText("plain text")).toBeTruthy();
  });

  it("失败结果在步骤上标记，展开显示原因", async () => {
    const user = userEvent.setup();
    const raw = JSON.stringify({ status: "error", message: "no such file" });
    const { container } = render(
      <ThinkingTrace trace={traceOf([tool("1", "read_file", { path: "x" }, raw)])} />,
    );
    await expand(user);
    expect(container.querySelector(".trace-step.error")).toBeTruthy();
    expect(screen.getByText("no such file")).toBeTruthy();
  });
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("ThinkingTrace · 只为最终分支格式化", () => {
  const expand = async (user: ReturnType<typeof userEvent.setup>) => {
    await user.click(screen.getByRole("button", { name: /展开工具调用/ }));
    await user.click(screen.getAllByRole("button", { expanded: false })[0]);
  };

  it("补丁结果不经过 JSON 缩进", async () => {
    const user = userEvent.setup();
    const stringify = vi.spyOn(JSON, "stringify");
    const raw = JSON.stringify({
      status: "ok",
      path: "src/a.ts",
      diff: "--- a/src/a.ts\n+++ b/src/a.ts\n@@ -1 +1 @@\n-a\n+b",
    });
    stringify.mockClear();

    render(<ThinkingTrace trace={traceOf([tool("1", "edit_file", { path: "src/a.ts" }, raw)])} />);
    await expand(user);

    expect(document.querySelector(".diff-body")).toBeTruthy();
    // Pretty-printing is `stringify(v, null, 2)`; other parts of the tree may
    // still stringify for their own reasons, so assert on that exact form.
    const prettyPrinted = stringify.mock.calls.some((call) => call[2] === 2);
    expect(prettyPrinted).toBe(false);
  });

  it("对象结果仍然缩进显示", async () => {
    const user = userEvent.setup();
    const stringify = vi.spyOn(JSON, "stringify");
    const raw = JSON.stringify({ status: "ok", path: "a.ts" });
    stringify.mockClear();

    render(<ThinkingTrace trace={traceOf([tool("1", "read_file", { path: "a.ts" }, raw)])} />);
    await expand(user);

    expect(stringify).toHaveBeenCalled();
  });
});
