import { act, fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import * as api from "../api";
import type { SessionSummary, WorkspaceProject, WorkspacesInfo } from "../api";
import { Sidebar } from "../components/Sidebar";
import { groupSessions } from "../lib/sessionGroups";
import type { StreamActivityMap } from "../useChatStream";
import { primeApiMock } from "./helpers";

vi.mock("../api", async () => (await import("./helpers")).apiMock);

const m = vi.mocked(api);

function s(id: string, root: string | null, extra: Partial<SessionSummary> = {}): SessionSummary {
  return {
    id,
    title: id,
    created_at: "2026-01-01T00:00:00Z",
    model_alias: "m",
    permission_mode: "ask",
    ...(root ? { root } : {}),
    ...extra,
  };
}

const project = (root: string, extra: Partial<WorkspaceProject> = {}): WorkspaceProject => ({
  root,
  secondary: [],
  ...extra,
});

function renderSidebar(
  sessions: SessionSummary[],
  projects: WorkspaceProject[] = [],
  activity: StreamActivityMap = {},
  workspaces: WorkspacesInfo = { projects },
) {
  const groups = groupSessions(sessions, workspaces.projects ?? []);
  const props = {
    currentId: null,
    activity,
    groups,
    archived: [],
    showArchived: false,
    collapsedSections: {},
    collapsedProjects: {},
    busy: false,
    sessionBlocked: false,
    viewToken: { current: 0 },
    workspaces,
    workspacesLoaded: true,
    chosenRoot: null,
    currentRoot: null,
    secondary: [],
    rootEditable: true,
    onNewSession: vi.fn(),
    newSessionLabel: "默认工作区",
    onOpenSession: vi.fn(),
    onToggleSection: vi.fn(),
    onToggleCollapsed: vi.fn(),
    onNewChatInProject: vi.fn(),
    onProjectAction: vi.fn(),
    onSetChosenRoot: vi.fn(),
    onSetSecondary: vi.fn(),
    onProjects: vi.fn(),
    onSessionWorkspace: vi.fn(),
    onToggleArchived: vi.fn(),
    onDeleteSession: vi.fn(),
    onTogglePin: vi.fn(),
    onRestoreSession: vi.fn(),
    onOpenMcp: vi.fn(),
    onError: vi.fn(),
  };
  const view = render(<Sidebar {...props} />);
  /** Re-render as the server's next session list would: the view derives
   *  everything from the list it is given. */
  const withSessions = (next: SessionSummary[], nextProjects: WorkspaceProject[] = []) =>
    view.rerender(
      <Sidebar
        {...props}
        groups={groupSessions(next, nextProjects)}
        workspaces={{ projects: nextProjects }}
      />,
    );
  return { ...props, view, withSessions };
}

/** The rows of one top-level section, found by its heading. */
function sectionRows(label: string): HTMLElement[] {
  const head = [...document.querySelectorAll<HTMLElement>(".side-head")].find(
    (el) => el.querySelector(".side-head-label")?.textContent === label,
  );
  if (!head) throw new Error(`没有找到分区: ${label}`);
  const body = head.parentElement?.querySelector(".side-body");
  return [...(body?.querySelectorAll<HTMLElement>(".session-item") ?? [])];
}

const rowsTitled = (title: string) =>
  [...document.querySelectorAll<HTMLElement>(".session-item")].filter(
    (el) => el.querySelector(".session-title")?.textContent === title,
  );

beforeEach(() => {
  window.localStorage.clear();
  primeApiMock(m);
});

describe("侧栏 · 置顶只显示一次", () => {
  it("置顶会话只在置顶分区出现，项目分区不再重复", () => {
    renderSidebar(
      [s("A", "/p/x", { pinned: true, pinned_at: "2026-02-01T00:00:00Z" }), s("B", "/p/x")],
      [project("/p/x")],
    );
    expect(sectionRows("置顶").map((r) => r.textContent)).toEqual(["A"]);
    expect(sectionRows("项目").map((r) => r.querySelector(".session-title")?.textContent)).toEqual([
      "B",
    ]);
    expect(rowsTitled("A")).toHaveLength(1);
  });

  it("无根目录的置顶会话不进入最近分区", () => {
    renderSidebar([s("A", null, { pinned: true, pinned_at: "2026-02-01T00:00:00Z" }), s("B", null)]);
    expect(sectionRows("置顶")).toHaveLength(1);
    expect(sectionRows("最近").map((r) => r.querySelector(".session-title")?.textContent)).toEqual([
      "B",
    ]);
  });

  it("项目成员全部置顶时项目分区给出空态，成员表仍保留运行态判断", () => {
    const { onProjectAction } = renderSidebar(
      [s("A", "/p/x", { pinned: true, pinned_at: "2026-02-01T00:00:00Z" })],
      [project("/p/x")],
      { A: { busy: true, approvals: 0 } },
    );
    // No row is listed, but the project still knows it has one running member.
    expect(rowsTitled("A")).toHaveLength(1);
    expect(document.querySelector(".project-empty")?.textContent).toBe("会话已全部置顶");
    fireEvent.click(document.querySelector(".group-more-btn") as HTMLElement);
    const archive = screen.getByRole("menuitem", { name: "归档聊天" }) as HTMLButtonElement;
    expect(archive.disabled).toBe(true);
    expect(onProjectAction).not.toHaveBeenCalled();
  });

  it("没有会话的项目显示未开始提示", () => {
    renderSidebar([], [project("/p/empty")]);
    expect(document.querySelector(".project-empty")?.textContent).toBe("还没有会话");
  });

  it("取消置顶后按服务端返回的列表归位", () => {
    const { onTogglePin, withSessions } = renderSidebar(
      [s("A", "/p/x", { pinned: true, pinned_at: "2026-02-01T00:00:00Z" })],
      [project("/p/x")],
    );
    const pin = rowsTitled("A")[0].querySelector(".session-pin") as HTMLButtonElement;
    fireEvent.click(pin);
    // The view asks the server and changes nothing on its own: the row is still
    // where it was until the new list arrives.
    expect(onTogglePin).toHaveBeenCalledTimes(1);
    expect(onTogglePin.mock.calls[0][0].id).toBe("A");
    expect(rowsTitled("A")).toHaveLength(1);

    act(() => {
      withSessions([s("A", "/p/x")], [project("/p/x")]);
    });
    expect(rowsTitled("A")).toHaveLength(1);
    const projectBody = document.querySelectorAll(".project-group")[0];
    expect(projectBody.querySelector(".session-title")?.textContent).toBe("A");
  });
});

describe("侧栏 · 图钉两态", () => {
  it("已置顶为实心并按下的语义，未置顶为描边", () => {
    renderSidebar(
      [s("A", "/p/x", { pinned: true, pinned_at: "2026-02-01T00:00:00Z" }), s("B", "/p/x")],
      [project("/p/x")],
    );
    const pinned = rowsTitled("A")[0].querySelector(".session-pin") as HTMLButtonElement;
    const plain = rowsTitled("B")[0].querySelector(".session-pin") as HTMLButtonElement;
    expect(pinned.classList.contains("on")).toBe(true);
    expect(pinned.getAttribute("aria-pressed")).toBe("true");
    // The filled shape is the pin's body; the stem stays an outline.
    expect(pinned.querySelector(".session-pin-cap")).toBeTruthy();
    expect(plain.classList.contains("on")).toBe(false);
    expect(plain.getAttribute("aria-pressed")).toBe("false");
  });

  it("点击图钉不打开会话", () => {
    const { onOpenSession } = renderSidebar(
      [s("A", "/p/x", { pinned: true, pinned_at: "2026-02-01T00:00:00Z" })],
      [project("/p/x")],
    );
    fireEvent.click(rowsTitled("A")[0].querySelector(".session-pin") as HTMLElement);
    expect(onOpenSession).not.toHaveBeenCalled();
  });
});

describe("侧栏 · 项目提示", () => {
  it("置顶行不再带 title，改用浮层说明所属项目与路径", () => {
    vi.useFakeTimers();
    try {
      renderSidebar(
        [s("A", "/p/x", { pinned: true, pinned_at: "2026-02-01T00:00:00Z" })],
        [project("/p/x", { name: "项目 X" })],
      );
      const open = rowsTitled("A")[0].querySelector(".session-open") as HTMLElement;
      expect(open.getAttribute("title")).toBeNull();
      const anchor = open;
      act(() => {
        fireEvent.pointerEnter(anchor);
        vi.advanceTimersByTime(499);
      });
      expect(document.querySelector(".session-hint")).toBeNull();
      act(() => {
        vi.advanceTimersByTime(1);
      });
      const hint = document.querySelector(".session-hint") as HTMLElement;
      expect(hint).toBeTruthy();
      expect(hint.getAttribute("role")).toBe("tooltip");
      expect(hint.textContent).toContain("A");
      expect(hint.textContent).toContain("项目 X");
      expect(hint.textContent).toContain("/p/x");
      // The description is announced from the trigger, not from the layer.
      expect(anchor.getAttribute("aria-describedby")).toBe(hint.id);

      // Leaving before the delay elapses shows nothing at all.
      act(() => {
        fireEvent.pointerLeave(anchor);
      });
      expect(document.querySelector(".session-hint")).toBeNull();
      expect(anchor.getAttribute("aria-describedby")).toBeNull();
      act(() => {
        vi.advanceTimersByTime(2000);
      });
      expect(document.querySelector(".session-hint")).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });

  it("键盘聚焦立即显示，Escape 关闭且不打开会话", () => {
    const { onOpenSession } = renderSidebar(
      [s("A", "/p/x", { pinned: true, pinned_at: "2026-02-01T00:00:00Z" })],
      [project("/p/x")],
    );
    const anchor = rowsTitled("A")[0].querySelector(".session-open") as HTMLElement;
    act(() => {
      anchor.focus();
    });
    expect(document.querySelector(".session-hint")).toBeTruthy();
    act(() => {
      fireEvent.keyDown(anchor, { key: "Escape" });
    });
    expect(document.querySelector(".session-hint")).toBeNull();
    expect(onOpenSession).not.toHaveBeenCalled();
  });

  it("滚动与窗口尺寸变化都关闭浮层", () => {
    renderSidebar(
      [s("A", "/p/x", { pinned: true, pinned_at: "2026-02-01T00:00:00Z" })],
      [project("/p/x")],
    );
    const anchor = rowsTitled("A")[0].querySelector(".session-open") as HTMLElement;
    act(() => {
      anchor.focus();
    });
    expect(document.querySelector(".session-hint")).toBeTruthy();
    act(() => {
      window.dispatchEvent(new Event("scroll", { bubbles: true }));
    });
    expect(document.querySelector(".session-hint")).toBeNull();

    // The row is still focused, so returning to it takes a fresh focus event:
    // the hint is shown, then a resize ends it the same way a scroll did.
    act(() => {
      anchor.blur();
      anchor.focus();
    });
    expect(document.querySelector(".session-hint")).toBeTruthy();
    fireEvent.resize(window);
    expect(document.querySelector(".session-hint")).toBeNull();
  });

  it("普通行保留标题提示，不带浮层", () => {
    renderSidebar([s("B", "/p/x")], [project("/p/x")]);
    const open = rowsTitled("B")[0].querySelector(".session-open") as HTMLElement;
    expect(open.getAttribute("title")).toBe("B");
    act(() => {
      open.focus();
    });
    expect(document.querySelector(".session-hint")).toBeNull();
  });

  it("工作区列表未加载时说明正在读取，不把未知路径当成无项目", () => {
    renderSidebar(
      [s("A", null, { pinned: true, pinned_at: "2026-02-01T00:00:00Z" })],
      [],
      {},
      { projects: [] },
    );
    const anchor = rowsTitled("A")[0].querySelector(".session-open") as HTMLElement;
    act(() => {
      anchor.focus();
    });
    // The list is empty but loaded here: the hint falls back to the default
    // project's own name rather than claiming the row has no project.
    expect(document.querySelector(".session-hint")?.textContent).toContain("默认工作区");
  });
});
