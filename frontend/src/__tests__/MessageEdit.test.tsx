import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import App from "../App";
import * as api from "../api";
import { composerField, detail, primeApiMock, session, sidebarRow } from "./helpers";

vi.mock("../api", async () => (await import("./helpers")).apiMock);

const m = vi.mocked(api);

/** A conversation with one finished turn, ready to be edited. */
function editable(id: string, title: string, prompt = "原来的问题") {
  return {
    ...detail(id, title, [
      { role: "user", content: prompt, turn_id: `${id}-t1` },
      { role: "assistant", content: "原来的回答", turn_id: `${id}-t1` },
    ]),
    turns: [{ id: `${id}-t1`, status: "completed" as const }],
    revision: 1,
  };
}

/** Capture the stream so the test decides when the turn ends. */
function controllableStream() {
  let onEvent: ((e: api.ChatEvent) => void) | undefined;
  let options: api.ChatOptions | undefined;
  let resolve!: () => void;
  const done = new Promise<void>((res) => {
    resolve = res;
  });
  m.streamChat.mockImplementation((_sid, _msg, cb, opts) => {
    onEvent = cb;
    options = opts;
    return done;
  });
  return {
    emit: (e: api.ChatEvent) => act(async () => onEvent?.(e)),
    options: () => options,
    finish: () => act(async () => resolve()),
  };
}

function editButtons(): HTMLElement[] {
  return screen.queryAllByRole("button", { name: "编辑并重新发送这条消息" });
}

beforeEach(() => {
  primeApiMock(m);
  localStorage.clear();
  m.fetchSessions.mockResolvedValue([session("A", "会话A")]);
  m.fetchModels.mockResolvedValue({
    default: "deepseek-v4flash",
    models: {},
    providers: {},
    limits: {},
  });
  m.fetchSession.mockResolvedValue(editable("A", "会话A"));
});

async function open(title = "会话A") {
  const user = userEvent.setup();
  render(<App />);
  await screen.findByText(title);
  await user.click(sidebarRow(title));
  await screen.findByText("原来的问题");
  return user;
}

describe("App · 编辑历史消息", () => {
  it("编辑入口在空闲会话可见，把原文载入输入框并说明副作用", async () => {
    const user = await open();
    const [retract] = editButtons();
    expect(retract).toBeTruthy();

    await user.click(retract);
    expect(composerField().value).toBe("原来的问题");
    expect(screen.getByText("正在编辑这条消息")).toBeTruthy();
    // 副作用说明必须写明：文件不会回退
    expect(screen.getByText(/文件、命令等已执行的操作不会回退/)).toBeTruthy();
  });

  it("重新发送带着目标回合和 revision，受理后只替换所属会话", async () => {
    const stream = controllableStream();
    const user = await open();
    await user.click(editButtons()[0]);
    await user.clear(composerField());
    await user.type(composerField(), "改过的问题");
    await user.click(screen.getByRole("button", { name: "发送消息" }));

    await waitFor(() => expect(m.streamChat).toHaveBeenCalled());
    const opts = stream.options();
    expect(opts?.edit_turn_id).toBe("A-t1");
    expect(opts?.expected_revision).toBe(1);

    // 受理前：旧对话仍在屏幕上（服务端还没改）
    expect(screen.getByText("原来的回答")).toBeTruthy();

    await stream.emit({
      type: "turn_accepted",
      session_id: "A",
      turn_id: "A-t2",
      replaced_turn_id: "A-t1",
      revision: 2,
      messages: [
        { role: "user", content: "改过的问题", turn_id: "A-t2" },
      ],
    });
    // 受理后的投影来自服务端：被替换的分支不再显示
    await waitFor(() => expect(screen.queryByText("原来的回答")).toBeNull());
    expect(screen.getByText("改过的问题")).toBeTruthy();

    await stream.emit({ type: "text", content: "新的回答" });
    expect(await screen.findByText("新的回答")).toBeTruthy();
    await stream.finish();
  });

  it("取消恢复编辑前的草稿，而不是清空输入框", async () => {
    const user = await open();
    await user.type(composerField(), "我刚写的半句");
    await user.click(editButtons()[0]);
    expect(composerField().value).toBe("原来的问题");

    await user.click(screen.getByRole("button", { name: "取消" }));
    expect(composerField().value).toBe("我刚写的半句");
    expect(screen.queryByText("正在编辑这条消息")).toBeNull();
  });

  it("Escape 先关菜单再取消编辑", async () => {
    m.fetchCommands.mockResolvedValue({
      commands: [
        { id: "personal::review", name: "review", description: "审查改动", kind: "template" },
      ],
    });
    const user = await open();
    await user.click(editButtons()[0]);
    await user.clear(composerField());
    await user.type(composerField(), "/");
    expect(await screen.findByRole("listbox")).toBeTruthy();

    await user.keyboard("{Escape}");
    // 菜单先关，编辑仍在
    expect(screen.queryByRole("listbox")).toBeNull();
    expect(screen.getByText("正在编辑这条消息")).toBeTruthy();

    await user.keyboard("{Escape}");
    expect(screen.queryByText("正在编辑这条消息")).toBeNull();
  });

  it("HTTP 失败时保留编辑草稿和原对话", async () => {
    m.streamChat.mockRejectedValue(new Error("HTTP 409"));
    const user = await open();
    await user.click(editButtons()[0]);
    await user.clear(composerField());
    await user.type(composerField(), "改过的问题");
    await user.click(screen.getByRole("button", { name: "发送消息" }));

    expect(await screen.findByText("HTTP 409")).toBeTruthy();
    // 被拒的编辑不能悄悄丢掉原文
    expect(screen.getByText("原来的回答")).toBeTruthy();
    expect(screen.getByText("正在编辑这条消息")).toBeTruthy();
  });

  it("刷新后目标回合已不存在时退出编辑，不会改到别的回合", async () => {
    const stream = controllableStream();
    const user = await open();
    await user.type(composerField(), "我刚开始写的");
    await user.click(editButtons()[0]);
    await user.type(composerField(), "，补充一句");
    // 重新打开会话：服务端已经没有那个回合了（换了分支或别的标签页改过）
    m.fetchSession.mockResolvedValue({
      ...detail("A", "会话A", [{ role: "user", content: "别的消息", turn_id: "A-t9" }]),
      turns: [{ id: "A-t9", status: "completed" as const }],
      revision: 5,
    });
    await user.click(sidebarRow("会话A"));
    await screen.findByText("别的消息");

    await waitFor(() => expect(screen.queryByText("正在编辑这条消息")).toBeNull());
    // 退回编辑前的那份草稿，而不是把文本挂到一个已经变了的回合上
    expect(composerField().value).toBe("我刚开始写的");
    await stream.finish();
  });

  it("回合进行中不提供编辑入口", async () => {
    const stream = controllableStream();
    const user = await open();
    await user.type(composerField(), "继续问");
    await user.click(screen.getByRole("button", { name: "发送消息" }));
    await waitFor(() => expect(m.streamChat).toHaveBeenCalled());

    expect(editButtons()).toHaveLength(0);
    await stream.finish();
    await waitFor(() => expect(editButtons().length).toBeGreaterThan(0));
  });

  it("编辑草稿跟着会话走，不会写入另一个会话", async () => {
    m.fetchSessions.mockResolvedValue([session("A", "会话A"), session("B", "会话B")]);
    m.fetchSession.mockImplementation((id: string) =>
      Promise.resolve(editable(id, id === "A" ? "会话A" : "会话B", id === "A" ? "甲的问题" : "乙的问题")),
    );
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText("会话A");

    await user.click(sidebarRow("会话A"));
    await screen.findByText("甲的问题");
    await user.click(editButtons()[0]);
    expect(composerField().value).toBe("甲的问题");

    await user.click(sidebarRow("会话B"));
    await screen.findByText("乙的问题");
    // 另一个会话的输入框不是 A 的编辑草稿
    expect(composerField().value).toBe("");
    expect(screen.queryByText("正在编辑这条消息")).toBeNull();

    await user.click(sidebarRow("会话A"));
    await screen.findByText("正在编辑这条消息");
    // 回到 A：草稿和它的编辑目标都还在
    expect(composerField().value).toBe("甲的问题");
    expect(screen.getByText("甲的问题", { selector: ".composer-editing-quote" })).toBeTruthy();
  });

  it("中文组词中按回车不发送，上屏后编辑仍在", async () => {
    const stream = controllableStream();
    const user = await open();
    await user.click(editButtons()[0]);
    const field = composerField();
    await user.clear(field);

    await act(async () => {
      field.dispatchEvent(new CompositionEvent("compositionstart", { bubbles: true }));
      field.dispatchEvent(new CompositionEvent("compositionupdate", { bubbles: true, data: "bian" }));
    });
    await user.keyboard("{Enter}");
    expect(m.streamChat).not.toHaveBeenCalled();

    await act(async () => {
      field.dispatchEvent(new CompositionEvent("compositionend", { bubbles: true, data: "编" }));
    });
    expect(screen.getByText("正在编辑这条消息")).toBeTruthy();
    await stream.finish();
  });
});
