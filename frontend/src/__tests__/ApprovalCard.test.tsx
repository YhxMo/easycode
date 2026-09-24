import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ApprovalCard } from "../components/primitives/ApprovalCard";
import type { Item } from "../types";

type ApprovalItem = Extract<Item, { kind: "approval" }>;

function item(state: ApprovalItem["state"] = "pending"): ApprovalItem {
  return {
    kind: "approval",
    id: "ap1",
    toolCallId: "t1",
    name: "execute_shell",
    args: { command: "rm -rf build" },
    reason: "删除构建目录",
    scope: "rm -rf build",
    state,
  };
}

describe("ApprovalCard", () => {
  it("待批准时展示动作、原因与队列位置", () => {
    render(
      <ApprovalCard item={item()} position={1} total={3} onDecide={() => {}} />,
    );
    expect(screen.getByText("需要批准")).toBeTruthy();
    expect(screen.getByText("Shell")).toBeTruthy();
    expect(screen.getByText("rm -rf build")).toBeTruthy();
    expect(screen.getByText("删除构建目录")).toBeTruthy();
    expect(screen.getByText("1/3")).toBeTruthy();
  });

  it("只有一个请求时不显示队列计数", () => {
    render(<ApprovalCard item={item()} position={1} total={1} onDecide={() => {}} />);
    expect(screen.queryByText("1/1")).toBeNull();
  });

  it("三个动作以正确的参数触发 onDecide", async () => {
    const user = userEvent.setup();
    const cases: Array<[string, boolean, boolean]> = [
      ["允许一次", true, false],
      ["始终允许", true, true],
      ["拒绝", false, false],
    ];
    for (const [label, approve, always] of cases) {
      const onDecide = vi.fn();
      const view = render(
        <ApprovalCard item={item()} position={1} total={1} onDecide={onDecide} />,
      );
      await user.click(screen.getByRole("button", { name: label }));
      expect(onDecide).toHaveBeenLastCalledWith(approve, always);
      view.unmount();
    }
  });

  it("已决策后折叠为结论行，不再提供按钮", () => {
    render(<ApprovalCard item={item("denied")} position={1} total={1} onDecide={() => {}} />);
    expect(screen.getByText("已拒绝")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "允许一次" })).toBeNull();
    expect(screen.queryByText("需要批准")).toBeNull();
  });
});
