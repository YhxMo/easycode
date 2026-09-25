import { act, fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ProjectMenu } from "../ProjectMenu";

// The ProjectMenu computes a `position: fixed` coordinate
// once on open and never re-calibrates. Scrolling an inner overflow container or
// resizing the window leaves the popup floating away from its trigger. The fix
// closes the menu on any window scroll (capture: also bubbles from inner
// scrollers) or resize — more robust than re-computing the coordinates.
describe("ProjectMenu · 滚动/尺寸漂移", () => {
  it("菜单打开后 window resize，菜单关闭", () => {
    render(<ProjectMenu pinned={false} running={false} onAction={vi.fn()} />);

    // Menu starts closed.
    expect(document.querySelector(".project-action-menu")).toBeNull();

    // Open the menu via its "•••" trigger.
    const trigger = document.querySelector(".group-more-btn") as HTMLElement;
    expect(trigger).toBeTruthy();
    fireEvent.click(trigger);
    expect(document.querySelector(".project-action-menu")).toBeTruthy();

    // Resizing the window must close the menu (coordinate drift guard).
    fireEvent.resize(window);
    expect(document.querySelector(".project-action-menu")).toBeNull();
  });

  it("菜单打开后 window scroll（capture 可捕获内层滚动容器）关闭菜单", () => {
    render(<ProjectMenu pinned={false} running={false} onAction={vi.fn()} />);

    const trigger = document.querySelector(".group-more-btn") as HTMLElement;
    fireEvent.click(trigger);
    expect(document.querySelector(".project-action-menu")).toBeTruthy();

    // A scroll event (fired at window with capture) closes the menu.
    act(() => {
      window.dispatchEvent(new Event("scroll", { bubbles: true }));
    });
    expect(document.querySelector(".project-action-menu")).toBeNull();
  });

  it("菜单点击菜单项触发 onAction 且关闭", () => {
    const onAction = vi.fn();
    render(<ProjectMenu pinned={false} running={false} onAction={onAction} />);

    const trigger = document.querySelector(".group-more-btn") as HTMLElement;
    fireEvent.click(trigger);

    // Click the "编辑" item.
    fireEvent.click(screen.getByRole("menuitem", { name: /编辑/ }));
    expect(onAction).toHaveBeenCalledWith("edit");
    expect(document.querySelector(".project-action-menu")).toBeNull();
  });
});

// A running conversation only blocks the project actions that genuinely need
// every session idle; the rest stay usable while another chat works.
describe("ProjectMenu · 并发范围", () => {
  it("有会话运行时只禁用归档，其余操作仍可点击", () => {
    const onAction = vi.fn();
    render(<ProjectMenu pinned={false} running onAction={onAction} />);
    fireEvent.click(document.querySelector(".group-more-btn") as HTMLElement);

    const archive = screen.getByRole("menuitem", { name: /归档聊天/ }) as HTMLButtonElement;
    expect(archive.disabled).toBe(true);
    expect(archive.title).toContain("正在运行");

    for (const name of [/置顶/, /编辑/, /在 Finder 中显示/, /创建永久工作树/, /移除项目/]) {
      expect((screen.getByRole("menuitem", { name }) as HTMLButtonElement).disabled).toBe(false);
    }

    fireEvent.click(screen.getByRole("menuitem", { name: /移除项目/ }));
    expect(onAction).toHaveBeenCalledWith("remove");
  });
});
