import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useState } from "react";
import type { McpInfo } from "../api";
import * as api from "../api";
import { ExtensionsSettings } from "../ExtensionsSettings";

vi.mock("../api", async () => (await import("./helpers")).apiMock);

const m = vi.mocked(api);

const PROJECT = "/ws/proj";

function info(): McpInfo {
  return {
    root: PROJECT,
    projects: [{ root: PROJECT, name: "proj" }],
    scopes: [
      { scope: "personal", root: null, path: "/home/u/.easycode/mcp.json", exists: false },
      { scope: "project", root: PROJECT, name: "proj", path: `${PROJECT}/easycode.config.json`, exists: false },
    ],
    servers: [],
    errors: [],
  };
}

/** The dialog as the app opens it: mounted by its trigger, gone on close. */
function Harness({
  initialTab = "skills",
  sessionId = null,
}: {
  initialTab?: "skills" | "mcp";
  sessionId?: string | null;
}) {
  const [open, setOpen] = useState(false);
  const [changed, setChanged] = useState(0);
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>
        打开扩展
      </button>
      <span data-testid="changed">{changed}</span>
      {open && (
        <ExtensionsSettings
          root={PROJECT}
          sessionId={sessionId}
          initialTab={initialTab}
          projectName="项目 P"
          projectPath={PROJECT}
          onClose={() => setOpen(false)}
          onChanged={() => setChanged((n) => n + 1)}
          onToast={vi.fn()}
        />
      )}
    </>
  );
}

const openDialog = async (user: ReturnType<typeof userEvent.setup>) =>
  user.click(screen.getByRole("button", { name: "打开扩展" }));

beforeEach(() => {
  vi.clearAllMocks();
  m.fetchSkills.mockResolvedValue({
    root: PROJECT,
    enabled: true,
    install_roots: { personal: "/home/u/.easycode/skills", project: `${PROJECT}/.easycode/skills` },
    skills: [],
    errors: [],
  });
  m.fetchMcp.mockResolvedValue(info());
  m.fetchMcpStatus.mockResolvedValue({ started: false, servers: [] });
});

describe("扩展弹窗 · 结构", () => {
  it("只有一个 dialog，并写明目标项目与路径", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await openDialog(user);
    expect(screen.getAllByRole("dialog")).toHaveLength(1);
    expect(screen.getByRole("dialog").getAttribute("aria-label")).toBe("扩展");
    expect(screen.getByText("项目 P")).toBeTruthy();
    expect(screen.getByText(PROJECT)).toBeTruthy();
  });

  it("页签成对设置 aria 关联，方向键切换并聚焦", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await openDialog(user);
    const skillsTab = screen.getByRole("tab", { name: "Skills" });
    const mcpTab = screen.getByRole("tab", { name: "MCP 服务" });
    expect(skillsTab.getAttribute("aria-selected")).toBe("true");
    expect(skillsTab.getAttribute("aria-controls")).toBe(
      screen.getByRole("tabpanel", { hidden: false }).id,
    );

    skillsTab.focus();
    await user.keyboard("{ArrowRight}");
    expect(mcpTab.getAttribute("aria-selected")).toBe("true");
    expect(document.activeElement).toBe(mcpTab);
    expect(screen.getByRole("tabpanel", { hidden: false }).getAttribute("aria-labelledby")).toBe(
      mcpTab.id,
    );

    // Wrapping in both directions: the selection never leaves the tablist.
    await user.keyboard("{ArrowRight}");
    expect(skillsTab.getAttribute("aria-selected")).toBe("true");
    expect(document.activeElement).toBe(skillsTab);
    await user.keyboard("{ArrowLeft}");
    expect(mcpTab.getAttribute("aria-selected")).toBe("true");
    expect(document.activeElement).toBe(mcpTab);
  });

  it("隐藏页签的控件不在焦点集合里", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await openDialog(user);
    // The MCP panel is mounted but hidden; its footer buttons are not rendered
    // at all, and nothing inside it may be reached by Tab.
    const hidden = document.querySelectorAll("[hidden] [tabindex]");
    expect(hidden).toHaveLength(0);
    const card = document.querySelector(".modal") as HTMLElement;
    const focusables = [...card.querySelectorAll<HTMLElement>("button, input, select, textarea")];
    expect(focusables.every((el) => !el.closest("[hidden]"))).toBe(true);
  });
});

describe("扩展弹窗 · 页签与请求", () => {
  it("MCP 页签只在第一次打开时读取配置", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await openDialog(user);
    // The skills tab is on screen: a project's servers are not read for a
    // reader who never looked at them.
    expect(m.fetchMcp).not.toHaveBeenCalled();
    await user.click(screen.getByRole("tab", { name: "MCP 服务" }));
    await waitFor(() => expect(m.fetchMcp).toHaveBeenCalledWith(PROJECT));
    await user.click(screen.getByRole("tab", { name: "Skills" }));
    await user.click(screen.getByRole("tab", { name: "MCP 服务" }));
    expect(m.fetchMcp).toHaveBeenCalledTimes(1);
  });

  it("连接状态只从传进来的那个会话读取", async () => {
    const user = userEvent.setup();
    render(<Harness initialTab="mcp" sessionId="s1" />);
    await openDialog(user);
    await waitFor(() => expect(m.fetchMcpStatus).toHaveBeenCalledWith("s1"));
  });

  it("没有匹配的会话时不读取任何连接状态", async () => {
    const user = userEvent.setup();
    render(<Harness initialTab="mcp" />);
    await openDialog(user);
    await waitFor(() => expect(m.fetchMcp).toHaveBeenCalled());
    expect(m.fetchMcpStatus).not.toHaveBeenCalled();
  });

  it("切换页签保留未提交的表单", async () => {
    const user = userEvent.setup();
    render(<Harness initialTab="mcp" />);
    await openDialog(user);
    await user.click(await screen.findByRole("button", { name: "添加服务" }));
    await user.type(screen.getByLabelText(/名称/), "draft-server");

    await user.click(screen.getByRole("tab", { name: "Skills" }));
    expect(document.querySelector(".ext-panel[hidden]")).toBeTruthy();
    await user.click(screen.getByRole("tab", { name: "MCP 服务" }));
    expect((screen.getByLabelText(/名称/) as HTMLInputElement).value).toBe("draft-server");
  });

  it("页签按钮随活动面板切换：隐藏面板不渲染 footer 按钮", async () => {
    const user = userEvent.setup();
    render(<Harness initialTab="mcp" />);
    await openDialog(user);
    await screen.findByRole("button", { name: "添加服务" });
    await user.click(screen.getByRole("tab", { name: "Skills" }));
    // One footer row, filled by the panel on screen only.
    expect(document.querySelectorAll(".modal-actions")).toHaveLength(1);
    await waitFor(() => expect(screen.queryByRole("button", { name: "添加服务" })).toBeNull());
    expect(screen.getByRole("button", { name: "关闭" })).toBeTruthy();
  });
});

describe("扩展弹窗 · 关闭", () => {
  it("关闭后焦点回到打开它的控件", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    const trigger = screen.getByRole("button", { name: "打开扩展" });
    await openDialog(user);
    await screen.findByRole("dialog");
    act(() => {
      fireEvent.keyDown(window, { key: "Escape" });
    });
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(document.activeElement).toBe(trigger);
  });

  it("关闭放弃未保存的输入，重新打开是一份新表单", async () => {
    const user = userEvent.setup();
    render(<Harness initialTab="mcp" />);
    await openDialog(user);
    await user.click(await screen.findByRole("button", { name: "添加服务" }));
    await user.type(screen.getByLabelText(/名称/), "typed-once");
    act(() => {
      fireEvent.keyDown(window, { key: "Escape" });
    });
    expect(screen.queryByRole("dialog")).toBeNull();

    await openDialog(user);
    await waitFor(() => expect(screen.getByRole("button", { name: "添加服务" })).toBeTruthy());
    expect(screen.queryByLabelText(/名称/)).toBeNull();
  });
});
