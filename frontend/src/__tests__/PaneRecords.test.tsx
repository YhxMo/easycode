import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import App from "../App";
import * as api from "../api";
import { detail, primeApiMock, session, sidebarRow } from "./helpers";

// What the pane shows comes from the session's own records: they outlive both a
// refresh and the compaction that drops tool results from the message history,
// and the working tree's uncommitted changes are reported beside them as a
// separate fact.
vi.mock("../api", async () => (await import("./helpers")).apiMock);

const m = vi.mocked(api);

const readRecord = {
  id: "t1",
  name: "read_file",
  args: { path: "src/app.ts" },
  result: JSON.stringify({
    status: "ok",
    path: "src/app.ts",
    absolute_path: "/ws/src/app.ts",
    total_lines: 12,
    lines: 12,
    content: "1: 内容",
  }),
};
const editRecord = {
  id: "t2",
  name: "edit_file",
  args: { path: "src/app.ts" },
  result: JSON.stringify({
    status: "ok",
    path: "src/app.ts",
    absolute_path: "/ws/src/app.ts",
    diff: "--- a/src/app.ts\n+++ b/src/app.ts\n+新增\n",
  }),
};

async function openPane(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole("button", { name: "展开面板" }));
  await screen.findByRole("tab", { name: "上下文" });
}

/** Open the change section's summary line, which is what starts collapsed. */
async function openChanges(user: ReturnType<typeof userEvent.setup>) {
  await user.click(tab("变更"));
  await user.click(await screen.findByRole("button", { name: /个文件/ }));
}

const tab = (name: string) => screen.getByRole("tab", { name });

beforeEach(() => {
  primeApiMock(m);
  m.fetchSessions.mockResolvedValue([session("A", "会话A")]);
  m.fetchModels.mockResolvedValue({ default: "m", models: {}, providers: {}, limits: {} });
  m.fetchFileContent.mockResolvedValue({
    path: "src/app.ts",
    text: "1: 内容",
    start_line: 1,
    total_lines: 1,
    truncated: false,
  });
});

describe("App · 面板记录", () => {
  it("历史里已经没有工具结果时，记录仍然给出上下文与 diff", async () => {
    const user = userEvent.setup();
    // A compaction left stubs behind: the messages still name the calls, but
    // only the session's records describe what they actually did.
    m.fetchSession.mockResolvedValue({
      ...detail("A", "会话A", [
        { role: "user", content: "改一下 app.ts" },
        {
          role: "assistant",
          tool_calls: [
            { id: "t1", function: { name: "read_file", arguments: '{"path":"src/app.ts"}' } },
            { id: "t2", function: { name: "edit_file", arguments: '{"path":"src/app.ts"}' } },
          ],
        },
        { role: "tool", tool_call_id: "t1", content: '{"status": "ok", "path": "src/app.ts"}' },
        { role: "tool", tool_call_id: "t2", content: '{"status": "ok", "path": "src/app.ts"}' },
        { role: "assistant", content: "改好了" },
      ]),
      artifacts: [readRecord, editRecord],
    });

    render(<App />);
    await screen.findByText("会话A");
    await user.click(sidebarRow("会话A"));
    await openPane(user);

    // Newest first: the edit card is the one the reader wants.
    await user.click(tab("上下文"));
    await screen.findByText("app.ts");
    expect(screen.getByText("12/12 行")).toBeTruthy();
    expect(screen.getByText("1: 内容")).toBeTruthy();

    await openChanges(user);
    expect(screen.getByText("会话操作记录")).toBeTruthy();
    // The file's counts are on its row; the diff itself is one click below.
    await user.click(screen.getByRole("button", { name: /src\/app\.ts/ }));
    expect(screen.getByText(/\+新增/)).toBeTruthy();

    await user.click(tab("文件"));
    const row = await screen.findByRole("button", { name: /app\.ts/ });
    expect(row.textContent).toContain("读 1");
    expect(row.textContent).toContain("改 1");
  });

  it("变更分区把工作区未提交改动与会话操作记录分开列出", async () => {
    const user = userEvent.setup();
    m.fetchSession.mockResolvedValue({
      ...detail("A", "会话A", [{ role: "user", content: "改一下" }]),
      artifacts: [editRecord],
    });
    m.fetchGitChanges.mockResolvedValue({
      repos: [{ root: "/ws", name: "ws" }],
      files: [
        {
          path: "notes.md",
          absolute_path: "/ws/notes.md",
          repo: "/ws",
          untracked: true,
          staged: "",
          unstaged: "",
          exists: true,
        },
      ],
      truncated: false,
    });

    render(<App />);
    await screen.findByText("会话A");
    await user.click(sidebarRow("会话A"));
    await openPane(user);
    await openChanges(user);

    // The session's own record is there...
    expect(await screen.findByRole("button", { name: /src\/app\.ts/ })).toBeTruthy();
    // ...next to the working tree's state, which this session did not cause.
    expect(screen.getByText("当前工作区未提交改动")).toBeTruthy();
    await waitFor(() => expect(screen.getByText("未跟踪")).toBeTruthy());
    expect(m.fetchGitChanges).toHaveBeenCalledWith("A");
  });

  it("目录不在 Git 仓库里时只展示会话操作记录", async () => {
    const user = userEvent.setup();
    m.fetchSession.mockResolvedValue({
      ...detail("A", "会话A", [{ role: "user", content: "改一下" }]),
      artifacts: [editRecord],
    });
    m.fetchGitChanges.mockResolvedValue({ repos: [], files: [], truncated: false });

    render(<App />);
    await screen.findByText("会话A");
    await user.click(sidebarRow("会话A"));
    await openPane(user);

    // The summary falls back to what this conversation changed: with no
    // repository there is no working-tree answer, and the record must not
    // disappear because of it.
    await user.click(tab("变更"));
    expect(screen.getByText("会话改动")).toBeTruthy();
    await user.click(screen.getByRole("button", { name: /个文件/ }));
    expect(await screen.findByText("会话所在目录不在 Git 仓库中。")).toBeTruthy();
    expect(screen.getByRole("button", { name: /src\/app\.ts/ })).toBeTruthy();
  });

  it("搜索命中默认只列一部分，可以展开全部", async () => {
    const user = userEvent.setup();
    const matches = Array.from({ length: 8 }, (_, i) => ({
      file: `src/f${i}.ts`,
      line: i + 1,
      text: `hit ${i}`,
    }));
    m.fetchSession.mockResolvedValue({
      ...detail("A", "会话A", []),
      artifacts: [
        {
          id: "t1",
          name: "grep",
          args: { pattern: "hit" },
          result: JSON.stringify({ status: "ok", matches, truncated: false }),
        },
      ],
    });

    render(<App />);
    await screen.findByText("会话A");
    await user.click(sidebarRow("会话A"));
    await openPane(user);
    await user.click(tab("上下文"));

    expect(await screen.findByText("还有 3 个 · 展开全部")).toBeTruthy();
    expect(screen.queryByText("src/f7.ts:8")).toBeNull();

    await user.click(screen.getByText("还有 3 个 · 展开全部"));
    expect(await screen.findByText("src/f7.ts:8")).toBeTruthy();
    expect(screen.getByText("收起")).toBeTruthy();
  });
});
