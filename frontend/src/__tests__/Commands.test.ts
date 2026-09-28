import { describe, expect, it } from "vitest";
import { activeCommandId, filterCommands } from "../features/composer/commands";
import type { CommandInfo } from "../api";

const picked = { id: "project:/b:deploy", name: "deploy" };

describe("activeCommandId", () => {
  it("文本仍是那条命令时带上它的标识", () => {
    expect(activeCommandId(picked, "/deploy")).toBe(picked.id);
    expect(activeCommandId(picked, "/deploy now")).toBe(picked.id);
    expect(activeCommandId(picked, "  /deploy ")).toBe(picked.id);
  });

  it("被改写成别的命令或普通文本时不再带上标识", () => {
    expect(activeCommandId(picked, "")).toBeNull();
    expect(activeCommandId(picked, "/deployx")).toBeNull();
    expect(activeCommandId(picked, "/other now")).toBeNull();
    expect(activeCommandId(picked, "先看看 /deploy")).toBeNull();
  });

  it("没有选择过条目时没有标识", () => {
    expect(activeCommandId(null, "/deploy now")).toBeNull();
  });
});

describe("filterCommands", () => {
  const commands: CommandInfo[] = [
    { id: "user::a", name: "a", description: "", kind: "skill" },
    { id: "user::b", name: "b", description: "部署脚本", kind: "template" },
  ];

  it("空查询列出全部，按 kind 分组排序", () => {
    expect(filterCommands(commands, "/").map((c) => c.name)).toEqual(["a", "b"]);
  });

  it("描述也能命中，且没有匹配时返回空列表", () => {
    expect(filterCommands(commands, "/部署").map((c) => c.name)).toEqual(["b"]);
    expect(filterCommands(commands, "/zzz")).toEqual([]);
  });
});
