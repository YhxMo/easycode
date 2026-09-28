import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import App from "../App";
import * as api from "../api";
import { detail, primeApiMock, session, sidebarRow } from "./helpers";

// The change section opens as a headline — what the working tree holds against
// its HEAD, or this conversation's own record when there is no repository — and
// the files with their diffs are one click below it. The reader's choice is
// kept per conversation, so switching tabs never reopens or recloses it.
vi.mock("../api", async () => (await import("./helpers")).apiMock);

const m = vi.mocked(api);

const editRecord = {
  id: "t1",
  name: "edit_file",
  args: { path: "src/app.ts" },
  result: JSON.stringify({
    status: "ok",
    path: "src/app.ts",
    absolute_path: "/ws/src/app.ts",
    diff: "--- a/src/app.ts\n+++ b/src/app.ts\n+新增行\n",
  }),
};

const gitChanges: api.GitChanges = {
  repos: [{ root: "/ws", name: "ws" }],
  // The stats request carries no per-file diff: the panel asks for one when a
  // row is opened (see the mocked fetchGitFileDiff below).
  files: [
    {
      path: "notes.md",
      absolute_path: "/ws/notes.md",
      repo: "/ws",
      untracked: true,
      staged: "",
      unstaged: "",
      exists: true,
      added: 4,
      removed: 0,
      binary: false,
      diff: null,
      diff_note: null,
    },
    {
      path: "logo.png",
      absolute_path: "/ws/logo.png",
      repo: "/ws",
      untracked: false,
      staged: "M",
      unstaged: "",
      exists: true,
      added: 0,
      removed: 0,
      binary: true,
      diff: null,
      diff_note: "二进制文件",
    },
    {
      path: "gone.txt",
      absolute_path: "/ws/gone.txt",
      repo: "/ws",
      untracked: false,
      staged: "",
      unstaged: "D",
      exists: false,
      added: 0,
      removed: 7,
      binary: false,
      diff: null,
      diff_note: "文件已删除",
    },
  ],
  added: 4,
  removed: 7,
  truncated: false,
};

async function openPane(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole("button", { name: "展开面板" }));
  await user.click(await screen.findByRole("tab", { name: "变更" }));
}

/** The section's headline button, which carries the file count. */
const summary = () => screen.getByRole("button", { name: /个文件/ });

beforeEach(() => {
  primeApiMock(m);
  m.fetchModels.mockResolvedValue({ default: "m", models: {}, providers: {}, limits: {} });
  m.fetchGitChanges.mockResolvedValue(gitChanges);
  m.fetchGitFileDiff.mockResolvedValue({ diff: "+第一行\n+第二行\n", diff_note: null });
});

describe("App · 变更面板", () => {
  it("默认只显示当前工作区相对 HEAD 的增删统计，点击后按文件给出统计与 diff", async () => {
    const user = userEvent.setup();
    m.fetchSessions.mockResolvedValue([session("A", "会话A")]);
    m.fetchSession.mockResolvedValue({
      ...detail("A", "会话A", [{ role: "user", content: "改一下" }]),
      artifacts: [editRecord],
    });

    render(<App />);
    await screen.findByText("会话A");
    await user.click(sidebarRow("会话A"));
    await openPane(user);

    // Collapsed: the counts of the working tree, and no file rows yet.
    const line = await screen.findByRole("button", { name: /个文件/ });
    await waitFor(() => expect(line.textContent).toContain("+4"));
    expect(line.textContent).toContain("−7");
    expect(line.textContent).toContain("当前工作区改动");
    expect(screen.queryByText("notes.md")).toBeNull();

    await user.click(line);
    expect(screen.getByText("会话操作记录")).toBeTruthy();
    expect(screen.getByText("当前工作区未提交改动")).toBeTruthy();
    // Both sources keep their own rows: the session's edit, and the tree's files.
    expect(screen.getByRole("button", { name: /src\/app\.ts/ })).toBeTruthy();
    expect(screen.getByRole("button", { name: /notes\.md/ })).toBeTruthy();

    // A file's diff is one more click, and only that file's — fetched then,
    // not with the stats.
    expect(m.fetchGitChanges).toHaveBeenCalledWith("A", false);
    expect(m.fetchGitFileDiff).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: /notes\.md/ }));
    await waitFor(() => expect(screen.getByText("+第一行")).toBeTruthy());
    expect(m.fetchGitFileDiff).toHaveBeenCalledWith("A", "/ws", "notes.md");
    expect(screen.queryByText("+新增行")).toBeNull();

    await user.click(screen.getByRole("button", { name: /src\/app\.ts/ }));
    expect(screen.getByText("+新增行")).toBeTruthy();
  });

  it("二进制与已删除的文件保留统计行，并说明为什么没有 diff", async () => {
    const user = userEvent.setup();
    m.fetchSessions.mockResolvedValue([session("A", "会话A")]);
    m.fetchSession.mockResolvedValue(detail("A", "会话A", [{ role: "user", content: "看看" }]));

    render(<App />);
    await screen.findByText("会话A");
    await user.click(sidebarRow("会话A"));
    await openPane(user);
    await user.click(await screen.findByRole("button", { name: /个文件/ }));

    // Neither row can be opened: they say what they know instead of showing an
    // empty preview.
    expect(screen.getByText("二进制文件")).toBeTruthy();
    expect(screen.getByText("文件已删除")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /logo\.png/ })).toBeNull();
    expect(document.querySelectorAll(".change-row.static").length).toBe(2);
  });

  it("每个会话各自记住变更是否展开", async () => {
    const user = userEvent.setup();
    m.fetchSessions.mockResolvedValue([session("A", "会话A"), session("B", "会话B")]);
    m.fetchSession.mockImplementation(async (id: string) => ({
      ...detail(id, id === "A" ? "会话A" : "会话B", [{ role: "user", content: "hi" }]),
      artifacts: [editRecord],
    }));

    render(<App />);
    await screen.findByText("会话A");
    await user.click(sidebarRow("会话A"));
    await openPane(user);
    await user.click(await screen.findByRole("button", { name: /个文件/ }));
    expect(screen.getByRole("button", { name: /src\/app\.ts/ })).toBeTruthy();

    // Another conversation opens collapsed, as it was left.
    await user.click(sidebarRow("会话B"));
    await waitFor(() => expect(screen.queryByRole("button", { name: /src\/app\.ts/ })).toBeNull());
    expect(summary()).toBeTruthy();

    // Going back finds the first conversation exactly as it was left.
    await user.click(sidebarRow("会话A"));
    expect(await screen.findByRole("button", { name: /src\/app\.ts/ })).toBeTruthy();
  });
});
