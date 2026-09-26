import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import App from "../App";
import * as api from "../api";
import { detail, primeApiMock, session, sidebarRow } from "./helpers";

// A conversation that has not started a turn yet may still be pointed at
// another project directory — the server owns the move and answers with the
// summary and project list it produced. Once a turn has run the directories are
// fixed: the card turns into a read-only detail. The sidebar names projects by
// their configured name, falling back to the directory's own name.
vi.mock("../api", async () => (await import("./helpers")).apiMock);

const m = vi.mocked(api);

const PROJECTS: api.WorkspaceProject[] = [
  { root: "/repoA", secondary: [], name: "项目甲" },
  { root: "/repoB", secondary: ["/secB"] },
];

const card = () => document.querySelector(".project-picker .dir-card") as HTMLElement;
const newHint = () => document.querySelector(".new-btn-hint")?.textContent ?? "";

/** Open the sidebar's main-directory card. */
async function openCard(user: ReturnType<typeof userEvent.setup>) {
  await user.click(card().querySelector(".dir-card-main") as HTMLElement);
}

beforeEach(() => {
  primeApiMock(m);
  m.fetchModels.mockResolvedValue({ default: "m", models: {}, providers: {}, limits: {} });
  m.fetchWorkspaces.mockResolvedValue({ default: "/ws", projects: PROJECTS });
});

describe("App · 会话主目录", () => {
  it("空白会话可以直接切换主目录，切换后次目录与项目名都跟着服务端答案更新", async () => {
    const user = userEvent.setup();
    const blank = { ...session("A", "会话A"), root: "/repoA", started: false };
    m.fetchSessions.mockResolvedValue([blank]);
    m.fetchSession.mockResolvedValue({ ...blank, messages: [] });
    m.setSessionWorkspace.mockResolvedValue({
      session: { ...blank, root: "/repoB", secondary_roots: ["/secB"] },
      projects: PROJECTS,
    });

    render(<App />);
    await screen.findByText("会话A");
    await user.click(sidebarRow("会话A"));
    await waitFor(() => expect(card().textContent).toContain("项目甲"));

    await openCard(user);
    await user.click(screen.getByRole("option", { name: "repoB" }));

    // The server decides the move: it is asked for the session, without a
    // secondary list, so the target project's own binding is what applies.
    await waitFor(() => expect(m.setSessionWorkspace).toHaveBeenCalledWith("A", "/repoB"));
    await waitFor(() => expect(card().textContent).toContain("/repoB"));
    // The secondary directory of the project it moved to, and its own name.
    expect(document.querySelector(".secondary-editor .dir-card small")?.textContent).toContain(
      "已连接 1 个目录",
    );
    expect(document.querySelector(".session-item.active .session-title")?.textContent).toBe("会话A");
  });

  it("切换失败时保留原来的目录，并说明原因", async () => {
    const user = userEvent.setup();
    const blank = { ...session("A", "会话A"), root: "/repoA", started: false };
    m.fetchSessions.mockResolvedValue([blank]);
    m.fetchSession.mockResolvedValue({ ...blank, messages: [] });
    m.setSessionWorkspace.mockRejectedValue(new Error("会话已经开始"));

    render(<App />);
    await screen.findByText("会话A");
    await user.click(sidebarRow("会话A"));
    await waitFor(() => expect(card().textContent).toContain("项目甲"));

    await openCard(user);
    await user.click(screen.getByRole("option", { name: "repoB" }));

    await screen.findByText("会话已经开始");
    // Nothing moved: the card still names the project it was on.
    expect(card().textContent).toContain("项目甲");
  });

  it("已经开始的会话只显示只读详情", async () => {
    const user = userEvent.setup();
    const started = { ...session("A", "会话A"), root: "/repoA" };
    m.fetchSessions.mockResolvedValue([started]);
    m.fetchSession.mockResolvedValue({
      ...detail("A", "会话A", [{ role: "user", content: "改一下" }]),
      root: "/repoA",
    });

    render(<App />);
    await screen.findByText("会话A");
    await user.click(sidebarRow("会话A"));
    await openCard(user);

    expect(screen.getByText("会话工作目录")).toBeTruthy();
    expect(screen.getByText("已有会话的工作目录不可修改。")).toBeTruthy();
    expect(screen.queryByRole("listbox")).toBeNull();
    expect(m.setSessionWorkspace).not.toHaveBeenCalled();
  });

  it("新会话入口显示配置的项目名，未配置时回退目录名", async () => {
    const user = userEvent.setup();
    m.fetchSessions.mockResolvedValue([]);
    m.setSessionWorkspace.mockResolvedValue({
      session: { ...session("N", "新会话"), root: "/repoB" },
      projects: PROJECTS,
    });

    render(<App />);
    await waitFor(() => expect(newHint()).toBe("ws"));

    await openCard(user);
    await user.click(screen.getByRole("option", { name: "repoB" }));
    // 项目乙 has no configured name: the folder's own name is what is left.
    await waitFor(() => expect(newHint()).toBe("repoB"));

    await openCard(user);
    await user.click(screen.getByRole("option", { name: /项目甲/ }));
    await waitFor(() => expect(newHint()).toBe("项目甲"));
  });
});
