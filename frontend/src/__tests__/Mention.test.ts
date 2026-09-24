import { describe, expect, it } from "vitest";
import { applyMention, mentionToken } from "../lib/mention";

describe("mentionToken", () => {
  it("识别光标所在位置的 @ token", () => {
    expect(mentionToken("看下 @src/ap", 10)).toEqual({ start: 3, query: "src/ap" });
  });

  it("行首的 @ 也算", () => {
    expect(mentionToken("@a", 2)).toEqual({ start: 0, query: "a" });
  });

  it("光标刚打完 @ 时是空查询", () => {
    expect(mentionToken("see @", 5)).toEqual({ start: 4, query: "" });
  });

  it("空白之后不再是同一个 token", () => {
    expect(mentionToken("@a b", 4)).toBeNull();
  });

  it("词中的 @ 不算（邮箱等）", () => {
    expect(mentionToken("a@b", 3)).toBeNull();
  });

  it("光标在 @ 之前时不识别", () => {
    expect(mentionToken("see @a", 3)).toBeNull();
  });

  it("取光标前的最后一个 @", () => {
    expect(mentionToken("@a @b", 5)).toEqual({ start: 3, query: "b" });
  });

  it("越界的光标位置按输入长度处理", () => {
    expect(mentionToken("@ab", 99)).toEqual({ start: 0, query: "ab" });
  });
});

describe("applyMention", () => {
  it("替换 token 并把光标放在路径之后", () => {
    const result = applyMention("看下 @src/ap", { start: 3, query: "src/ap" }, "src/app.ts");
    expect(result.value).toBe("看下 @src/app.ts");
    expect(result.caret).toBe(14);
  });

  it("后面还有内容时不留下多余空格", () => {
    const result = applyMention("see @src/ap now", { start: 4, query: "src/ap" }, "src/app.ts");
    expect(result.value).toBe("see @src/app.ts now");
    expect(result.caret).toBe(15);
  });

  it("紧跟文字时补一个分隔空格", () => {
    const result = applyMention("see @src/ap!", { start: 4, query: "src/ap" }, "src/app.ts");
    expect(result.value).toBe("see @src/app.ts !");
  });
});
