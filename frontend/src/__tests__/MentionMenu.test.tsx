import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import App from "../App";
import * as api from "../api";
import { composerField, deferred, detail, primeApiMock, session, sidebarRow } from "./helpers";

// The composer's @-mention flow: fetch the workspace listing, show what the
// query is doing (searching / failed / empty), and insert the picked path back
// into the input. While the menu is up it owns Enter, so a query that has not
// answered yet can never turn into a sent message.
vi.mock("../api", async () => (await import("./helpers")).apiMock);

const m = vi.mocked(api);

const FILES = [
  { path: "src/app.ts", name: "app.ts", dir: "src", root: "/p", absolute_path: "/p/src/app.ts" },
  {
    path: "src/web/routes.py",
    name: "routes.py",
    dir: "src/web",
    root: "/p",
    absolute_path: "/p/src/web/routes.py",
  },
];

beforeEach(() => {
  primeApiMock(m);
  m.fetchSessions.mockResolvedValue([session("s1", "会话A")]);
  m.fetchSession.mockResolvedValue(detail("s1", "会话A", []));
  m.fetchModels.mockResolvedValue({
    default: "deepseek-v4flash",
    models: {},
    providers: {},
    limits: {},
  });
  m.fetchFiles.mockResolvedValue({ files: FILES, total: 2 });
});

async function openSession() {
  const user = userEvent.setup();
  render(<App />);
  await screen.findByText("会话A");
  await user.click(sidebarRow("会话A"));
  return { user, box: composerField() };
}

describe("App · @ 文件引用", () => {
  it("输入 @ 后列出工作区文件，点击插入路径", async () => {
    const { user, box } = await openSession();
    await user.type(box, "看下 @app");

    await screen.findByText("app.ts");
    expect(m.fetchFiles).toHaveBeenCalledWith("s1", undefined, "app");

    await user.click(screen.getByText("app.ts"));
    // A trailing space keeps the next word from running into the file name.
    expect(box.value).toBe("看下 @/p/src/app.ts ");
  });

  it("回车插入当前高亮的文件", async () => {
    const { user, box } = await openSession();
    await user.type(box, "@src");
    await screen.findByText("routes.py");

    await user.keyboard("{Enter}");
    expect(box.value).toBe("@/p/src/app.ts ");
    // the menu closes once a path is picked
    expect(screen.queryByText("routes.py")).toBeNull();
  });

  it("空查询只提示输入文件名，不列出整个工作区", async () => {
    const { box } = await openSession();

    await userEvent.type(box, "@");

    expect(screen.getByText("输入文件名搜索")).toBeTruthy();
    expect(m.fetchFiles).not.toHaveBeenCalled();
    expect(screen.queryByText("app.ts")).toBeNull();
  });

  it("查询未完成时显示查找中，回车不发送整条消息", async () => {
    const pending = deferred<{ files: typeof FILES; total: number }>();
    m.fetchFiles.mockReturnValue(pending.promise);
    const { user, box } = await openSession();

    await user.type(box, "看看 @a");
    await screen.findByText("正在查找文件…");

    await user.keyboard("{Enter}");
    expect(m.streamChat).not.toHaveBeenCalled();
    expect(box.value).toBe("看看 @a");

    pending.resolve({ files: FILES, total: FILES.length });
    await screen.findByText("app.ts");
  });

  it("查询失败显示原因并可重试，不冒充空结果", async () => {
    m.fetchFiles.mockRejectedValueOnce(new Error("路径越界"));
    const { user, box } = await openSession();

    await user.type(box, "@a");
    await screen.findByText(/路径越界/);
    expect(screen.queryByText(/没有匹配/)).toBeNull();

    await user.keyboard("{Enter}");
    expect(m.streamChat).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "重试" }));
    await screen.findByText("app.ts");
  });

  it("无匹配时不发送，Escape 关掉菜单后回车才恢复发送", async () => {
    m.fetchFiles.mockResolvedValue({ files: [], total: 0 });
    const { user, box } = await openSession();

    await user.type(box, "@没有这个文件");
    await screen.findByText("没有匹配「没有这个文件」的文件");

    await user.keyboard("{Enter}");
    expect(m.streamChat).not.toHaveBeenCalled();

    await user.keyboard("{Escape}");
    await user.keyboard("{Enter}");
    await waitFor(() => expect(m.streamChat).toHaveBeenCalledTimes(1));
  });

  it("输入法组词中的回车只确认候选", async () => {
    const { user, box } = await openSession();
    await user.type(box, "@a");
    await screen.findByText("app.ts");

    // keyCode 229 is what Chrome reports while an IME is composing.
    box.dispatchEvent(
      new KeyboardEvent("keydown", { key: "Enter", keyCode: 229, bubbles: true, cancelable: true }),
    );
    expect(m.streamChat).not.toHaveBeenCalled();
    expect(box.value).toBe("@a");
  });

  it("Escape 只关闭文件菜单，不移除已输入的内容", async () => {
    const { user, box } = await openSession();
    await user.type(box, "@a");
    await screen.findByText("app.ts");

    await user.keyboard("{Escape}");
    expect(screen.queryByText("app.ts")).toBeNull();
    expect(box.value).toBe("@a");
  });

  it("含空格的路径按引号形式插入，仍是一个引用 token", async () => {
    m.fetchFiles.mockResolvedValue({
      files: [
        {
          path: "c.py",
          name: "c.py",
          dir: "",
          root: "/p",
          absolute_path: "/a b/c.py",
        },
      ],
      total: 1,
    });
    const { user, box } = await openSession();

    await user.type(box, "@c.py");
    await screen.findByText("c.py");
    await user.keyboard("{Enter}");

    expect(box.value).toBe('@"/a b/c.py" ');
  });

  it("草稿会话按草稿目录查询", async () => {
    const user = userEvent.setup();
    m.fetchSessions.mockResolvedValue([]);
    render(<App />);
    const box = composerField();

    await user.type(box, "@a");
    await screen.findByText("app.ts");
    expect(m.fetchFiles).toHaveBeenCalledWith(null, { root: null, secondary: [] }, "a");
  });
});
