import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { CodeBlock } from "../components/primitives/CodeBlock";
import { DiffView } from "../components/primitives/DiffView";

const PATCH = [
  "--- a/src/app.ts",
  "+++ b/src/app.ts",
  "@@ -1,3 +1,3 @@",
  " const a = 1;",
  "-const b = 2;",
  "+const b = 3;",
].join("\n");

describe("CodeBlock", () => {
  it("源码块显示行号，不出现 Diff 开关", () => {
    render(<CodeBlock code={"const a = 1;\nconst b = 2;"} lang="ts" />);
    expect(screen.getByText("ts")).toBeTruthy();
    expect(screen.getByText("const a = 1;")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Diff" })).toBeNull();
  });

  it("补丁默认按 Diff 渲染，可切回源码", async () => {
    const user = userEvent.setup();
    render(<CodeBlock code={PATCH} lang="diff" />);
    // diff view: file header with +/- counts
    expect(screen.getByText("+1")).toBeTruthy();
    expect(screen.getByText("−1")).toBeTruthy();

    await user.click(screen.getByRole("button", { name: "Code" }));
    expect(document.querySelector(".code-body")).toBeTruthy();
    expect(document.querySelector(".diff-body")).toBeNull();
  });

  it("复制按钮写入剪贴板并给出反馈", async () => {
    const user = userEvent.setup();
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    render(<CodeBlock code="const a = 1;" />);
    await user.click(screen.getByRole("button", { name: "复制代码" }));
    expect(writeText).toHaveBeenCalledWith("const a = 1;");
    expect(await screen.findByText("已复制")).toBeTruthy();
  });
});

describe("DiffView", () => {
  it("按行分类渲染，并显示文件与统计", () => {
    render(<DiffView path="src/app.ts" diff={PATCH} />);
    expect(screen.getByText("src/app.ts")).toBeTruthy();
    expect(document.querySelector(".diff-line.del")?.textContent).toBe("-const b = 2;");
    expect(document.querySelector(".diff-line.add")?.textContent).toBe("+const b = 3;");
    expect(document.querySelector(".diff-line.hunk")).toBeTruthy();
  });
});

describe("CodeBlock · 行号与空行", () => {
  const lineTexts = () =>
    Array.from(document.querySelectorAll(".code-line")).map(
      (line) => line.querySelector(".code-text")?.textContent,
    );

  it("空字符串仍是一行，空行用空格占位", () => {
    render(<CodeBlock code="" />);
    expect(lineTexts()).toEqual([" "]);
  });

  it("末尾的一个换行不算额外的一行", () => {
    render(<CodeBlock code={"a\nb\n"} />);
    expect(lineTexts()).toEqual(["a", "b"]);
  });

  it("末尾两个换行会多出一个空行", () => {
    render(<CodeBlock code={"a\nb\n\n"} />);
    expect(lineTexts()).toEqual(["a", "b", " "]);
  });

  it("中间的空行照样占一行，行号连续", () => {
    render(<CodeBlock code={"a\n\nb"} />);
    expect(lineTexts()).toEqual(["a", " ", "b"]);
    expect(
      Array.from(document.querySelectorAll(".code-gutter")).map((g) => g.textContent),
    ).toEqual(["1", "2", "3"]);
  });

  it("按 Diff 渲染时不算源码的行", () => {
    render(<CodeBlock code={PATCH} lang="diff" />);
    expect(document.querySelector(".code-body")).toBeNull();
    expect(document.querySelectorAll(".code-line")).toHaveLength(0);
  });

  it("复制的是原始文本，不是去掉了末尾换行的显示文本", async () => {
    const user = userEvent.setup();
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    render(<CodeBlock code={"a\n"} />);
    await user.click(screen.getByRole("button", { name: "复制代码" }));
    expect(writeText).toHaveBeenCalledWith("a\n");
  });
});
