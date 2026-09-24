import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import App from "../App";
import * as api from "../api";
import { detail, primeApiMock, session, sidebarRow } from "./helpers";

// The composer's @-mention flow: fetch the workspace listing, filter locally in
// the menu, and insert the picked path back into the input.
vi.mock("../api", async () => (await import("./helpers")).apiMock);

const m = vi.mocked(api);

const FILES = [
  { path: "src/app.ts", name: "app.ts", dir: "src", root: "/p" },
  { path: "src/web/routes.py", name: "routes.py", dir: "src/web", root: "/p" },
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
  const box = screen.getByRole("textbox") as HTMLTextAreaElement;
  return { user, box };
}

describe("App · @ 文件引用", () => {
  it("输入 @ 后列出工作区文件，点击插入路径", async () => {
    const { user, box } = await openSession();
    await user.type(box, "看下 @app");

    await screen.findByText("app.ts");
    expect(m.fetchFiles).toHaveBeenCalledWith("s1", undefined, "app");

    await user.click(screen.getByText("app.ts"));
    expect(box.value).toBe("看下 @src/app.ts");
  });

  it("回车插入当前高亮的文件", async () => {
    const { user, box } = await openSession();
    await user.type(box, "@");
    await screen.findByText("routes.py");

    await user.keyboard("{Enter}");
    expect(box.value).toBe("@src/app.ts");
    // the menu closes once a path is picked
    expect(screen.queryByText("routes.py")).toBeNull();
  });

  it("Escape 只关闭文件菜单，不移除已输入的内容", async () => {
    const { user, box } = await openSession();
    await user.type(box, "@a");
    await screen.findByText("app.ts");

    await user.keyboard("{Escape}");
    expect(screen.queryByText("app.ts")).toBeNull();
    expect(box.value).toBe("@a");
  });

  it("草稿会话按草稿目录查询", async () => {
    const user = userEvent.setup();
    m.fetchSessions.mockResolvedValue([]);
    render(<App />);
    const box = await screen.findByRole("textbox");

    await user.type(box, "@");
    await screen.findByText("app.ts");
    expect(m.fetchFiles).toHaveBeenCalledWith(null, { root: null, secondary: [] }, "");
  });
});
