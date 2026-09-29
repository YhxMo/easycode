import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import App from "../app/App";
import * as api from "../api";
import {
  commandsInfo,
  composerField,
  detail,
  editable,
  primeApiMock,
  session,
  sidebarRow,
} from "./helpers";

// `/mcp:<service> <task>` from the menu and from the keyboard: which service a
// message asks for, which project answers for it, and what happens to the
// selection when the conversation, the draft or the configuration moves.
vi.mock("../api", async () => (await import("./helpers")).apiMock);

const m = vi.mocked(api);

const DEMO = {
  id: "mcp:%2Fws%2Fa:demo",
  name: "mcp:demo",
  description: "使用 demo 服务完成任务",
  kind: "mcp" as const,
  argument_hint: "任务描述",
  source: "project" as const,
  source_label: "A · 项目",
  mcp_server: "demo",
  project_root: "/ws/a",
};

/** The same service name in another project: a different entry, a different id. */
const OTHER_PROJECT: api.CommandInfo = {
  ...DEMO,
  id: "mcp:%2Fws%2Fb:demo",
  source_label: "B · 项目",
  project_root: "/ws/b",
};

/** A conversation whose recorded prompt was an MCP command. */
function sentAsCommand(id: string, title: string) {
  return {
    ...detail(id, title, [
      { role: "user", content: "/mcp:demo 第一版任务", turn_id: `${id}-t1` },
      { role: "assistant", content: "原来的回答", turn_id: `${id}-t1` },
    ]),
    turns: [{ id: `${id}-t1`, status: "completed" as const, command_id: DEMO.id }],
    revision: 1,
  };
}

/** Capture the stream so the test sees exactly what a send carried. */
function captureStream() {
  const calls: { message: string; options: api.ChatOptions | undefined }[] = [];
  let onEvent: ((e: api.ChatEvent) => void) | undefined;
  m.streamChat.mockImplementation((_sid, message, cb, opts) => {
    calls.push({ message, options: opts });
    onEvent = cb;
    return Promise.resolve();
  });
  return {
    calls,
    emit: (e: api.ChatEvent) => act(async () => onEvent?.(e)),
  };
}

const editButtons = () =>
  screen.queryAllByRole("button", { name: "编辑并重新发送这条消息" });
const sendButton = () => screen.getByRole("button", { name: /发送消息/ });

beforeEach(() => {
  primeApiMock(m);
  localStorage.clear();
  m.fetchSessions.mockResolvedValue([session("A", "会话A")]);
  m.fetchModels.mockResolvedValue({ default: "m", models: {}, providers: {}, limits: {} });
  m.fetchSession.mockResolvedValue(editable("A", "会话A"));
  m.fetchCommands.mockResolvedValue(commandsInfo([DEMO]));
});

async function openSession(title = "会话A") {
  const user = userEvent.setup();
  render(<App />);
  await screen.findByText(title);
  await user.click(sidebarRow(title));
  await screen.findByText("原来的回答");
  return user;
}

describe("MCP 命令 · 选择与发送", () => {
  it("从菜单选中只填草稿，发送时才带上服务标识", async () => {
    const user = await openSession();
    const stream = captureStream();

    await user.type(composerField(), "/mcp:");
    await user.click(await screen.findByText("/mcp:demo"));

    // Picking fills the composer; nothing is sent until the user says so.
    expect(composerField().value).toBe("/mcp:demo ");
    expect(m.streamChat).not.toHaveBeenCalled();

    await user.type(composerField(), "把 2 和 3 相加");
    await user.click(sendButton());

    await waitFor(() => expect(stream.calls).toHaveLength(1));
    expect(stream.calls[0].message).toBe("/mcp:demo 把 2 和 3 相加");
    expect(stream.calls[0].options?.command_id).toBe(DEMO.id);
  });

  it("手输同样发送，不带菜单选择也能表达服务", async () => {
    const user = await openSession();
    const stream = captureStream();

    await user.type(composerField(), "/mcp:demo 统计一下");
    await user.click(sendButton());

    await waitFor(() => expect(stream.calls).toHaveLength(1));
    expect(stream.calls[0].message).toBe("/mcp:demo 统计一下");
    // Hand-typed text carries no id: the server resolves the name itself.
    expect(stream.calls[0].options?.command_id).toBeUndefined();
  });

  it("选择后再改写成别的服务，不再带上原条目的标识", async () => {
    const user = await openSession();
    const stream = captureStream();

    await user.type(composerField(), "/mcp:");
    await user.click(await screen.findByText("/mcp:demo"));
    await user.clear(composerField());
    await user.type(composerField(), "/mcp:other 做点别的");
    await user.click(sendButton());

    await waitFor(() => expect(stream.calls).toHaveLength(1));
    expect(stream.calls[0].options?.command_id).toBeUndefined();
  });
});

describe("MCP 命令 · 请求按项目隔离", () => {
  it("请求带上当前会话，切到另一个会话会重新读取", async () => {
    m.fetchSessions.mockResolvedValue([session("A", "会话A"), session("B", "会话B")]);
    m.fetchSession.mockImplementation(async (id: string) =>
      id === "A" ? editable("A", "会话A") : editable("B", "会话B", "B 的问题"),
    );
    const user = await openSession();

    await waitFor(() => expect(m.fetchCommands).toHaveBeenLastCalledWith("A", null));
    await user.click(sidebarRow("会话B"));
    await screen.findByText("B 的问题");

    await waitFor(() => expect(m.fetchCommands).toHaveBeenLastCalledWith("B", null));
  });

  it("上一个项目的晚到响应不会被当作当前项目的服务", async () => {
    m.fetchSessions.mockResolvedValue([session("A", "会话A"), session("B", "会话B")]);
    m.fetchSession.mockImplementation(async (id: string) =>
      id === "A" ? editable("A", "会话A") : editable("B", "会话B", "B 的问题"),
    );
    let resolveLate!: (value: api.CommandsInfo) => void;
    const late = new Promise<api.CommandsInfo>((res) => {
      resolveLate = res;
    });
    m.fetchCommands.mockImplementation((sessionId: string | null) =>
      sessionId === "A" ? late : Promise.resolve(commandsInfo([OTHER_PROJECT])),
    );

    const user = await openSession();
    await user.click(sidebarRow("会话B"));
    await screen.findByText("B 的问题");
    await user.type(composerField(), "/mcp:");

    // The first conversation's answer arrives only now; it described /ws/a.
    await act(async () => {
      resolveLate(commandsInfo([DEMO]));
    });

    await waitFor(() => expect(screen.getByText("/mcp:demo")).toBeTruthy());
    expect(screen.getByText("B · 项目")).toBeTruthy();
    expect(screen.queryByText("A · 项目")).toBeNull();
  });

  it("扩展设置变更后立刻重新读取命令", async () => {
    const user = await openSession();
    const empty = {
      root: null,
      projects: [],
      scopes: [],
      servers: [],
      errors: [],
    };
    m.fetchMcp.mockResolvedValue(empty);
    m.fetchMcpStatus.mockResolvedValue({ started: false, servers: [] });
    m.fetchSkills.mockResolvedValue({
      root: "/ws/a",
      enabled: true,
      install_roots: { personal: "/u/.easycode/skills", project: "/ws/a/.easycode/skills" },
      skills: [],
      errors: [],
    });
    m.saveMcpServer.mockResolvedValue(empty);
    const before = m.fetchCommands.mock.calls.length;

    // The dialog reports a save through onChanged, which bumps the revision the
    // list request depends on: a service added here is selectable without a
    // page reload.
    await user.click(screen.getByRole("button", { name: "扩展" }));
    await user.click(await screen.findByRole("tab", { name: "MCP 服务" }));
    await user.click(await screen.findByRole("button", { name: "添加服务" }));
    await user.type(screen.getByLabelText(/名称/), "demo");
    await user.type(screen.getByLabelText(/命令/), "npx");
    await user.click(screen.getByRole("button", { name: "保存" }));

    await waitFor(() => expect(m.fetchCommands.mock.calls.length).toBeGreaterThan(before));
  });
});

describe("MCP 命令 · 草稿随会话", () => {
  it("选择跟着自己的会话，切走再回来仍是它", async () => {
    m.fetchSessions.mockResolvedValue([session("A", "会话A"), session("B", "会话B")]);
    m.fetchSession.mockImplementation(async (id: string) =>
      id === "A" ? editable("A", "会话A") : editable("B", "会话B", "B 的问题"),
    );
    const user = await openSession();

    await user.type(composerField(), "/mcp:");
    await user.click(await screen.findByText("/mcp:demo"));

    await user.click(sidebarRow("会话B"));
    await screen.findByText("B 的问题");
    expect(composerField().value).toBe("");

    await user.click(sidebarRow("会话A"));
    await screen.findByText("原来的问题");
    await waitFor(() => expect(composerField().value).toBe("/mcp:demo "));
  });
});

describe("MCP 命令 · 编辑历史", () => {
  it("回车与按钮走同一条编辑重发路径，并保留来源标识", async () => {
    m.fetchSession.mockResolvedValue(sentAsCommand("A", "会话A"));
    const user = await openSession();
    const stream = captureStream();

    // The conversation came from disk, so the selection is rebuilt from what the
    // server recorded for that turn — not from whatever the composer holds.
    await user.click(editButtons()[0]);
    await waitFor(() => expect(composerField().value).toBe("/mcp:demo 第一版任务"));

    await user.clear(composerField());
    await user.type(composerField(), "/mcp:demo 第二版任务");
    await user.keyboard("{Enter}");

    await waitFor(() => expect(stream.calls).toHaveLength(1));
    expect(stream.calls[0].message).toBe("/mcp:demo 第二版任务");
    expect(stream.calls[0].options?.command_id).toBe(DEMO.id);
    expect(stream.calls[0].options?.edit_turn_id).toBe("A-t1");
  });

  it("编辑时改成另一个命令，标识重新解析而不是沿用旧条目", async () => {
    m.fetchSession.mockResolvedValue(sentAsCommand("A", "会话A"));
    const user = await openSession();
    const stream = captureStream();

    await user.click(editButtons()[0]);
    await waitFor(() => expect(composerField().value).toBe("/mcp:demo 第一版任务"));
    await user.clear(composerField());
    await user.type(composerField(), "/other 换个做法");
    await user.click(sendButton());

    await waitFor(() => expect(stream.calls).toHaveLength(1));
    expect(stream.calls[0].options?.command_id).toBeUndefined();
  });
});
