import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { ApprovalSheet } from "../ApprovalSheet";

describe("ApprovalSheet", () => {
  it("关闭态：容器 inert + aria-hidden，且所有操作按钮不在键盘 Tab 序（不可聚焦）", () => {
    render(
      <ApprovalSheet
        open={false}
        position={1}
        total={1}
        name="execute_shell"
        onDecide={vi.fn()}
        onDismiss={vi.fn()}
      />,
    );
    // Closed sheet is inert + aria-hidden, so RTL excludes it from the
    // accessibility tree by default — query it with `hidden: true` (UI-5).
    const sheet = screen.getByRole("alertdialog", { hidden: true });
    // closed => inert + hidden from AT
    expect(sheet.getAttribute("inert")).not.toBeNull();
    expect(sheet.getAttribute("aria-hidden")).toBe("true");
    // no button is keyboard reachable while hidden (UI-5)
    for (const name of ["收起", "拒绝", "始终允许", "允许一次"]) {
      const btn = screen.getByRole("button", { name, hidden: true });
      expect(btn.tabIndex).toBe(-1);
    }
  });

  it("打开态：不 inert，操作按钮恢复可聚焦", () => {
    render(
      <ApprovalSheet
        open={true}
        position={1}
        total={2}
        name="read_file"
        onDecide={vi.fn()}
        onDismiss={vi.fn()}
      />,
    );
    const sheet = screen.getByRole("alertdialog");
    expect(sheet.getAttribute("inert")).toBeNull();
    expect(sheet.getAttribute("aria-hidden")).toBeNull();
    for (const name of ["拒绝", "始终允许", "允许一次"]) {
      const btn = screen.getByRole("button", { name });
      expect(btn.tabIndex).toBe(0);
    }
  });

  it("展示动态队列序号（position/total 非写死 1）", () => {
    render(
      <ApprovalSheet
        open={true}
        position={2}
        total={3}
        name="edit_file"
        onDecide={vi.fn()}
        onDismiss={vi.fn()}
      />,
    );
    expect(screen.getByText("2/3 个问题")).toBeTruthy();
  });

  it("点击操作按钮以正确参数触发 onDecide", async () => {
    const onDecide = vi.fn();
    const onDismiss = vi.fn();
    render(
      <ApprovalSheet
        open={true}
        position={1}
        total={1}
        name="execute_shell"
        onDecide={onDecide}
        onDismiss={onDismiss}
      />,
    );
    screen.getByRole("button", { name: "拒绝" }).click();
    expect(onDecide).toHaveBeenCalledWith(false, false);
    screen.getByRole("button", { name: "始终允许" }).click();
    expect(onDecide).toHaveBeenCalledWith(true, true);
    screen.getByRole("button", { name: "允许一次" }).click();
    expect(onDecide).toHaveBeenCalledWith(true, false);
    screen.getByRole("button", { name: "收起" }).click();
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });
});
