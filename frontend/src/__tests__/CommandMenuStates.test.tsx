import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import App from "../App";
import * as api from "../api";
import { composerField, detail, primeApiMock, session, sidebarRow } from "./helpers";

// The `/` menu is shown in every state it can be in — loading, no match, no
// commands at all, and a failed load — and a picked entry carries its id, which
// is what lets the server expand another project's command in this session.
vi.mock("../api", async () => (await import("./helpers")).apiMock);

const m = vi.mocked(api);

const entry = (over: Partial<api.CommandInfo> = {}): api.CommandInfo => ({
  id: "project:/b:deploy",
  name: "deploy",
  description: "部署脚本",
  kind: "template",
  source: "project",
  source_label: "B 项目",
  ...over,
});

async function openSession(user: ReturnType<typeof userEvent.setup>) {
  render(<App />);
  await screen.findByText("会话A");
  await user.click(sidebarRow("会话A"));
  await user.type(composerField(), "/");
}

beforeEach(() => {
  primeApiMock(m);
  m.fetchSessions.mockResolvedValue([session("A", "会话A")]);
  m.fetchModels.mockResolvedValue({ default: "m", models: {}, providers: {}, limits: {} });
  m.fetchSession.mockResolvedValue(detail("A", "会话A", []));
});

describe("App · 命令菜单状态", () => {
  it("加载中也会显示菜单", async () => {
    const user = userEvent.setup();
    m.fetchCommands.mockReturnValue(new Promise(() => {}));

    await openSession(user);

    expect(await screen.findByText("正在加载命令…")).toBeTruthy();
  });

  it("没有登记任何命令时说明列表是空的", async () => {
    const user = userEvent.setup();
    m.fetchCommands.mockResolvedValue({ commands: [] });

    await openSession(user);

    expect(await screen.findByText("还没有可用的命令或 Skill")).toBeTruthy();
  });

  it("查询没有匹配时菜单不消失", async () => {
    const user = userEvent.setup();
    m.fetchCommands.mockResolvedValue({ commands: [entry()] });

    await openSession(user);
    await screen.findByText("/deploy");
    await user.type(composerField(), "zzz");

    expect(await screen.findByText("没有匹配的命令")).toBeTruthy();
  });

  it("加载失败时给出原因，重试后恢复", async () => {
    const user = userEvent.setup();
    m.fetchCommands.mockRejectedValueOnce(new Error("网络断了"));
    m.fetchCommands.mockResolvedValue({ commands: [entry()] });

    await openSession(user);

    expect(await screen.findByText("命令加载失败：网络断了")).toBeTruthy();
    await user.click(screen.getByRole("button", { name: "重试" }));
    expect(await screen.findByText("/deploy")).toBeTruthy();
  });

  it("从菜单选中的命令随消息带上自己的标识", async () => {
    const user = userEvent.setup();
    m.fetchCommands.mockResolvedValue({ commands: [entry()] });

    await openSession(user);
    await user.click(await screen.findByText("/deploy"));
    expect((composerField() as HTMLTextAreaElement).value).toBe("/deploy ");

    await user.type(composerField(), "now");
    await user.click(screen.getByRole("button", { name: /发送消息/ }));

    await waitFor(() => expect(m.streamChat).toHaveBeenCalledTimes(1));
    expect(m.streamChat).toHaveBeenLastCalledWith(
      "A",
      "/deploy now",
      expect.any(Function),
      expect.objectContaining({ command_id: "project:/b:deploy" }),
    );
  });

  it("把选中的命令改写掉后，发送不再带上它", async () => {
    const user = userEvent.setup();
    m.fetchCommands.mockResolvedValue({ commands: [entry()] });

    await openSession(user);
    await user.click(await screen.findByText("/deploy"));
    await user.clear(composerField());
    await user.type(composerField(), "/deployx");
    await user.click(screen.getByRole("button", { name: /发送消息/ }));

    await waitFor(() => expect(m.streamChat).toHaveBeenCalledTimes(1));
    expect(m.streamChat.mock.calls[0][3]).not.toHaveProperty("command_id");
  });
});
