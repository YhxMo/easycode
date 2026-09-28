import { describe, expect, it } from "vitest";
import { applyMention, mentionRef, mentionToken } from "../features/composer/mention";

describe("mentionToken", () => {
  it("识别光标所在位置的 @ token", () => {
    expect(mentionToken("看下 @src/ap", 10)).toEqual({ start: 3, end: 10, query: "src/ap" });
  });

  it("行首的 @ 也算", () => {
    expect(mentionToken("@a", 2)).toEqual({ start: 0, end: 2, query: "a" });
  });

  it("光标刚打完 @ 时是空查询", () => {
    expect(mentionToken("see @", 5)).toEqual({ start: 4, end: 5, query: "" });
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
    expect(mentionToken("@a @b", 5)).toEqual({ start: 3, end: 5, query: "b" });
  });

  it("越界的光标位置按输入长度处理", () => {
    expect(mentionToken("@ab", 99)).toEqual({ start: 0, end: 3, query: "ab" });
  });

  it("引号形式可以包含空格，并延伸到闭合引号", () => {
    expect(mentionToken('@"/a b/c.py" 后面', 13)).toEqual({
      start: 0,
      end: 12,
      query: "/a b/c.py",
    });
  });

  it("引号还没闭合时取到光标", () => {
    expect(mentionToken('@"/a b', 6)).toEqual({ start: 0, end: 6, query: "/a b" });
  });
});

describe("mentionRef", () => {
  it("含空格的路径用引号包起来", () => {
    expect(mentionRef("/tmp/a b/c.py")).toBe('@"/tmp/a b/c.py"');
    expect(mentionRef("src/app.ts")).toBe("@src/app.ts");
  });
});

describe("applyMention", () => {
  it("替换 token 后补一个空格并落在空格之后", () => {
    const result = applyMention("看下 @src/ap", { start: 3, end: 10, query: "src/ap" }, "src/app.ts");
    expect(result.value).toBe("看下 @src/app.ts ");
    expect(result.caret).toBe(15);
  });

  it("后面已有空格时不重复添加", () => {
    const result = applyMention("see @src/ap now", { start: 4, end: 11, query: "src/ap" }, "src/app.ts");
    expect(result.value).toBe("see @src/app.ts now");
    expect(result.caret).toBe(15);
  });

  it("紧跟文字时补一个分隔空格", () => {
    const result = applyMention("see @src/ap!", { start: 4, end: 11, query: "src/ap" }, "src/app.ts");
    expect(result.value).toBe("see @src/app.ts !");
  });

  it("含空格的路径替换掉引号 token 后仍然是一个词", () => {
    const value = '看 @"/a b/c"';
    const token = mentionToken(value, value.length)!;
    const result = applyMention(value, token, "/a b/c.py");
    expect(result.value).toBe('看 @"/a b/c.py" ');
    expect(result.caret).toBe(result.value.length);
  });
});
