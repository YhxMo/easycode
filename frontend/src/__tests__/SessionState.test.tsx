import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import App from "../App";
import * as api from "../api";
import { composerField, detail, primeApiMock, session, sessionRow, sidebarRow } from "./helpers";

// S1 regression set: which conversation owns a tab, a draft and a pane choice.
vi.mock("../api", async () => (await import("./helpers")).apiMock);

const m = vi.mocked(api);

/** Captures every started stream so a test can drive them independently. */
function captureStreams() {
  const calls: Array<{
    onEvent: (e: api.ChatEvent) => void;
    resolve: () => void;
    /** True once the caller aborted the request this call represents. */
    aborted: () => boolean;
  }> = [];
  m.streamChat.mockImplementation((_sid, _msg, cb, opts) => {
    let resolve!: () => void;
    const done = new Promise<void>((res) => {
      resolve = res;
    });
    calls.push({ onEvent: cb, resolve, aborted: () => opts?.signal?.aborted === true });
    return done;
  });
  return calls;
}

const composer = () => composerField() as HTMLTextAreaElement;
/** Real session tabs: the draft's own tab has no close button. */
const tabTitles = () =>
  [...document.querySelectorAll(".tab-strip .tab")]
    .filter((t) => t.querySelector(".tab-close"))
    .map((t) => t.querySelector(".tab-title")?.textContent ?? "");
const activeTitle = () => document.querySelector(".tab.active .tab-title")?.textContent ?? "";
const paneOpen = () => document.querySelector(".right-pane") !== null;
const closePane = async (user: ReturnType<typeof userEvent.setup>) => {
  await user.click(screen.getByRole("button", { name: "关闭面板" }));
};

/** A read_file call makes the turn produce context, which opens the pane. */
async function produceContext(stream: { onEvent: (e: api.ChatEvent) => void }, path: string) {
  const tool_call = { id: "t1", name: "read_file", arguments: { path } };
  await act(async () => stream.onEvent({ type: "tool_start", tool_call }));
  await act(async () =>
    stream.onEvent({
      type: "tool_result",
      tool_call,
      result: JSON.stringify({
        status: "ok",
        path,
        in_allowed: true,
        start_line: 1,
        end_line: 1,
        total_lines: 1,
        truncated: false,
        lines: 1,
        chars: 3,
        content: "1: x",
      }),
    }),
  );
  await act(async () => stream.onEvent({ type: "text", content: "完成" }));
}

beforeEach(() => {
  window.localStorage.clear();
  primeApiMock(m);
  m.fetchSessions.mockResolvedValue([session("A", "会话A"), session("B", "会话B")]);
  m.fetchModels.mockResolvedValue({
    default: "deepseek-v4flash",
    models: {},
    providers: {},
    limits: {},
  });
  m.fetchSession.mockImplementation((id: string) =>
    Promise.resolve(detail(id, id === "A" ? "会话A" : "会话B", [])),
  );
});

describe("App · 标签生命周期", () => {
  it("A→B→A 都保留标签，刷新后从存储恢复", async () => {
    const user = userEvent.setup();
    const { unmount } = render(<App />);
    await screen.findByText("会话A");
    await user.click(sidebarRow("会话A"));
    await waitFor(() => expect(tabTitles()).toEqual(["会话A"]));

    await user.click(sidebarRow("会话B"));
    await waitFor(() => expect(tabTitles()).toEqual(["会话A", "会话B"]));

    await user.click(sidebarRow("会话A"));
    expect(tabTitles()).toEqual(["会话A", "会话B"]);

    const stored = JSON.parse(window.localStorage.getItem("easycode:open_tabs") ?? "[]");
    expect(stored).toEqual(["A", "B"]);

    // A reload restores the same strip instead of only the foreground.
    unmount();
    render(<App />);
    await waitFor(() => expect(tabTitles()).toEqual(["会话A", "会话B"]));
  });

  it("恢复存储里的标签时去重并忽略非字符串", async () => {
    window.localStorage.setItem("easycode:open_tabs", JSON.stringify(["A", "A", 7, "", "B"]));
    render(<App />);
    await waitFor(() => expect(tabTitles()).toEqual(["会话A", "会话B"]));
  });

  it("服务端已不存在的标签在列表加载成功后清除", async () => {
    window.localStorage.setItem("easycode:open_tabs", JSON.stringify(["A", "gone"]));
    render(<App />);
    await waitFor(() => expect(tabTitles()).toEqual(["会话A"]));
  });

  it("关闭标签不取消它的后台回合", async () => {
    const user = userEvent.setup();
    const streams = captureStreams();

    render(<App />);
    await screen.findByText("会话A");
    await user.click(sidebarRow("会话A"));
    await user.type(composer(), "first");
    await user.click(screen.getByRole("button", { name: /发送消息/ }));
    await waitFor(() => expect(streams.length).toBe(1));

    await user.click(screen.getByRole("button", { name: "关闭 会话A" }));
    await waitFor(() => expect(tabTitles()).toEqual([]));
    expect(m.cancelSessionChat).not.toHaveBeenCalled();

    // The turn kept writing into its own slot: reopening shows its output.
    await act(async () => streams[0].onEvent({ type: "text", content: "后台输出" }));
    await user.click(sidebarRow("会话A"));
    await screen.findByText("后台输出");
    expect(m.streamChat).toHaveBeenCalledTimes(1);
  });

  it("删除失败时标签仍在，删除成功后不留空壳", async () => {
    const user = userEvent.setup();
    m.fetchSession.mockImplementation(async (id: string) =>
      detail(id, "session", [{ role: "assistant", content: "内容" }]),
    );
    m.deleteSession.mockRejectedValueOnce(new Error("文件占用"));
    render(<App />);
    await screen.findByText("会话A");
    await user.click(sidebarRow("会话A"));
    await user.click(within(sessionRow("会话A")).getByTitle("删除"));

    await user.click(screen.getByRole("button", { name: "确认删除" }));
    await waitFor(() => expect(document.querySelector(".app-toast")).not.toBeNull());
    expect(tabTitles()).toEqual(["会话A"]);

    // Deleting a session this page no longer knows the title of must not leave
    // a nameless "会话" tab behind.
    m.fetchSessions.mockResolvedValue([session("B", "会话B")]);
    m.deleteSession.mockResolvedValue({ deleted: true });
    await waitFor(() => expect(sidebarRow("会话B")).toBeTruthy());
    await user.click(within(sessionRow("会话B")).getByTitle("删除"));
    await user.click(screen.getByRole("button", { name: "确认删除" }));
    await waitFor(() => expect(tabTitles()).toEqual([]));
  });

  it("重复的 session 事件不会复制标签", async () => {
    const user = userEvent.setup();
    const streams = captureStreams();

    render(<App />);
    await screen.findByText("会话A");
    // The backend writes the session before it names it, so the very next list
    // already carries it.
    const created = { ...session("new", "新会话标题") };
    m.fetchSessions.mockResolvedValue([session("A", "会话A"), session("B", "会话B"), created]);

    await user.type(composer(), "go");
    await user.click(screen.getByRole("button", { name: /发送消息/ }));
    await waitFor(() => expect(streams.length).toBe(1));

    await act(async () => streams[0].onEvent({ type: "session", session_id: "new" }));
    await act(async () => streams[0].onEvent({ type: "session", session_id: "new" }));
    await waitFor(() => expect(tabTitles()).toEqual(["新会话标题"]));
    expect(activeTitle()).toBe("新会话标题");
  });
});

describe("App · 草稿归属", () => {
  it("每个会话各自保存未发送的草稿", async () => {
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText("会话A");
    await user.click(sidebarRow("会话A"));
    await user.type(composer(), "只给 A 的草稿");

    await user.click(sidebarRow("会话B"));
    expect(composer().value).toBe("");

    await user.type(composer(), "只给 B 的草稿");
    await user.click(sidebarRow("会话A"));
    expect(composer().value).toBe("只给 A 的草稿");
  });

  it("关闭已有回合的标签只收起视图，重开仍能恢复草稿", async () => {
    const user = userEvent.setup();
    m.fetchSession.mockImplementation(async (id: string) =>
      detail(id, "会话A", [{ role: "user", content: "hi" }, { role: "assistant", content: "内容" }]),
    );
    render(<App />);
    await screen.findByText("会话A");
    await user.click(sidebarRow("会话A"));
    await user.type(composer(), "还没发");
    await user.click(screen.getByRole("button", { name: "关闭 会话A" }));

    // The conversation had already run, so the tab close is view-only.
    expect(m.deleteSession).not.toHaveBeenCalled();
    await user.click(sidebarRow("会话A"));
    expect(composer().value).toBe("还没发");
  });

  it("关闭空白标签会连同会话一起删除", async () => {
    const user = userEvent.setup();
    // The server creates it, then lists it: the tab only survives because the
    // list carries it, which is how a real blank session behaves.
    let created: api.SessionSummary | null = null;
    m.fetchSessions.mockImplementation(async () => [
      session("A", "会话A"),
      ...(created ? [created] : []),
    ]);
    m.createSession.mockImplementation(async () => {
      created = { ...session("new-1", "新会话"), started: false };
      return created;
    });
    m.fetchSession.mockImplementation(async (id: string) =>
      id === "new-1" && created ? { ...created, messages: [] } : detail(id, "会话A", []),
    );
    render(<App />);
    await screen.findByText("会话A");
    await user.click(screen.getByRole("button", { name: "新建会话" }));
    await waitFor(() => expect(m.createSession).toHaveBeenCalled());
    await waitFor(() => expect(tabTitles()).toEqual(["新会话"]));

    await user.click(screen.getByRole("button", { name: "关闭 新会话" }));

    // The server confirmed it never started a turn, which is what allows the
    // tab close to remove it instead of only hiding the view.
    await waitFor(() => expect(m.deleteSession).toHaveBeenCalledWith("new-1", true));
    await waitFor(() => expect(tabTitles()).toEqual([]));
  });

  it("前台发送并立即切走，回合结束不清空另一个会话的输入", async () => {
    const user = userEvent.setup();
    const streams = captureStreams();

    render(<App />);
    await screen.findByText("会话A");
    await user.click(sidebarRow("会话A"));
    await user.type(composer(), "A 的消息");
    await user.click(screen.getByRole("button", { name: /发送消息/ }));
    await waitFor(() => expect(streams.length).toBe(1));

    await user.click(sidebarRow("会话B"));
    await user.type(composer(), "B 正在写");
    await act(async () => streams[0].resolve());

    expect(composer().value).toBe("B 正在写");
  });

  it("新建会话命名后，草稿跟着新会话而不是留在前台猜测的会话里", async () => {
    const user = userEvent.setup();
    const streams = captureStreams();

    render(<App />);
    await screen.findByText("会话A");
    const created = { ...session("new", "新会话标题") };
    m.fetchSessions.mockResolvedValue([session("A", "会话A"), session("B", "会话B"), created]);

    await user.type(composer(), "第一条");
    await user.click(screen.getByRole("button", { name: /发送消息/ }));
    await waitFor(() => expect(streams.length).toBe(1));

    await act(async () => streams[0].onEvent({ type: "session", session_id: "new" }));
    // The user keeps composing after the id arrived: the text belongs to the
    // created conversation, so switching away and back must keep it.
    await user.type(composer(), "补充");
    await user.click(sidebarRow("会话B"));
    expect(composer().value).toBe("");
    await user.click(screen.getByRole("tab", { name: /新会话标题/ }));
    expect(composer().value).toBe("补充");
  });
});

describe("App · 面板归属", () => {
  it("面板开关属于整个窗口：切换会话不改变它，新产物仍会自动展开", async () => {
    const user = userEvent.setup();
    const streams = captureStreams();

    render(<App />);
    await screen.findByText("会话A");
    await user.click(sidebarRow("会话A"));
    await user.type(composer(), "a1");
    await user.click(screen.getByRole("button", { name: /发送消息/ }));
    await waitFor(() => expect(streams.length).toBe(1));
    await produceContext(streams[0], "a/a.ts");
    await act(async () => streams[0].resolve());
    await waitFor(() => expect(paneOpen()).toBe(true));

    await closePane(user);
    expect(paneOpen()).toBe(false);

    // Switching conversations is not by itself a reason to open the pane again.
    await user.click(sidebarRow("会话B"));
    await waitFor(() => expect(activeTitle()).toBe("会话B"));
    expect(paneOpen()).toBe(false);

    // B's own new artifacts do open it: the automatic opening is per turn.
    await user.type(composer(), "b1");
    await user.click(screen.getByRole("button", { name: /发送消息/ }));
    await waitFor(() => expect(streams.length).toBe(2));
    await produceContext(streams[1], "b/b.ts");
    await act(async () => streams[1].resolve());
    await waitFor(() => expect(paneOpen()).toBe(true));

    // One switch serves the whole window: A now shows the open pane too.
    await user.click(sidebarRow("会话A"));
    await waitFor(() => expect(activeTitle()).toBe("会话A"));
    expect(paneOpen()).toBe(true);
  });

  it("上一轮的清单不会让新回合一发送就弹开面板", async () => {
    const user = userEvent.setup();
    const streams = captureStreams();
    m.fetchSession.mockImplementation(async (id: string) => ({
      ...detail(id, "会话A", [{ role: "assistant", content: "旧回复" }]),
      todos: [{ text: "上一轮的任务", status: "pending" }],
    }));

    render(<App />);
    await screen.findByText("会话A");
    await user.click(sidebarRow("会话A"));
    await screen.findByText("旧回复");
    expect(paneOpen()).toBe(false);

    await user.type(composer(), "新的一轮");
    await user.click(screen.getByRole("button", { name: /发送消息/ }));
    await waitFor(() => expect(streams.length).toBe(1));
    // A pending turn with no artifact of its own must not pop the pane open.
    expect(paneOpen()).toBe(false);

    // This turn's own list is what re-arms the automatic opening.
    await act(async () =>
      streams[0].onEvent({ type: "todo", todos: [{ text: "本轮任务", status: "pending" }] }),
    );
    expect(paneOpen()).toBe(true);
  });

  it("预览目标属于会话，切换后不显示另一个会话选中的文件", async () => {
    const user = userEvent.setup();
    const streams = captureStreams();
    m.fetchFileContent.mockImplementation(async (_id: string, path: string) => ({
      path,
      text: `内容 of ${path}`,
      start_line: 1,
      total_lines: 1,
      truncated: false,
    }));

    render(<App />);
    await screen.findByText("会话A");
    await user.click(sidebarRow("会话A"));
    await user.type(composer(), "a1");
    await user.click(screen.getByRole("button", { name: /发送消息/ }));
    await waitFor(() => expect(streams.length).toBe(1));
    await produceContext(streams[0], "shared/a.ts");
    await act(async () => streams[0].resolve());
    await waitFor(() => expect(paneOpen()).toBe(true));

    await user.click(await screen.findByRole("button", { name: /shared\/a\.ts/ }));
    await screen.findByText("内容 of shared/a.ts");

    await user.click(sidebarRow("会话B"));
    await waitFor(() => expect(activeTitle()).toBe("会话B"));
    expect(screen.queryByText("内容 of shared/a.ts")).toBeNull();
  });
});

describe("App · 侧栏会话行", () => {
  it("会话行是真实按钮：键盘可以打开、置顶和删除", async () => {
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText("会话A");

    // Enter on the open control switches the conversation.
    sidebarRow("会话A").focus();
    await user.keyboard("{Enter}");
    expect(activeTitle()).toBe("会话A");

    // Pin and delete are their own buttons, reachable with the keyboard.
    const pin = within(sessionRow("会话A")).getByRole("button", { name: /置顶会话/ });
    pin.focus();
    await user.keyboard("{Enter}");
    await waitFor(() => expect(m.pinSession).toHaveBeenCalledWith("A", true));

    const del = within(sessionRow("会话A")).getByRole("button", { name: "删除会话 会话A" });
    del.focus();
    await user.keyboard("{Enter}");
    expect(screen.getByRole("dialog", { name: "删除会话" })).toBeTruthy();
  });

  it("归档恢复失败时条目仍在并给出原因", async () => {
    const user = userEvent.setup();
    m.fetchArchivedSessions.mockResolvedValue([session("A", "归档会话")]);
    m.setSessionArchived = vi.fn().mockRejectedValue(new Error("磁盘只读")) as never;
    render(<App />);
    await user.click(await screen.findByRole("button", { name: /已归档/ }));
    await screen.findByText("归档会话");

    await user.click(screen.getByRole("button", { name: "恢复" }));

    await waitFor(() =>
      expect(document.querySelector(".app-toast")?.textContent).toContain("磁盘只读"),
    );
    // the entry did not silently vanish from its section
    expect(screen.getByText("归档会话")).toBeTruthy();
  });
});

describe("App · 草稿标签", () => {
  it("点击草稿标签不会把草稿键当成会话去加载", async () => {
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText("会话A");

    // The draft tab is already the open view: selecting it changes nothing and
    // must not request a session named after the draft key.
    await user.click(screen.getByRole("tab", { name: "新会话" }));
    expect(m.fetchSession).not.toHaveBeenCalled();
    expect(screen.queryByText(/会话加载失败/)).toBeNull();
    expect(activeTitle()).toBe("新会话");
  });
});

describe("App · 删除会话释放本机缓存", () => {
  const artifact = (id: string, path: string): api.ArtifactRecord => ({
    id,
    name: "read_file",
    args: { path },
    result: JSON.stringify({
      status: "ok",
      path,
      absolute_path: `/ws/${path}`,
      start_line: 1,
      end_line: 1,
      total_lines: 1,
      truncated: false,
      lines: 1,
      chars: 3,
      content: "1: x",
    }),
  });

  it("删除完成后到达的流事件不会把会话重新填回视图", async () => {
    const user = userEvent.setup();
    const streams = captureStreams();
    m.fetchSessions.mockResolvedValue([session("A", "会话A"), session("B", "会话B")]);
    m.fetchSession.mockImplementation((id: string) =>
      Promise.resolve({
        ...detail(id, id === "A" ? "会话A" : "会话B", []),
        started: true,
        artifacts: [artifact(`c-${id}`, `only-in-${id}.ts`)],
      }),
    );

    render(<App />);
    await screen.findByText("会话A");
    await user.click(sidebarRow("会话A"));
    await user.type(composerField(), "问一句");
    await user.click(screen.getByRole("button", { name: /发送消息/ }));
    await waitFor(() => expect(streams.length).toBe(1));

    await user.click(screen.getByRole("button", { name: /删除会话 会话A/ }));
    await user.click(screen.getByRole("button", { name: "确认删除" }));
    await waitFor(() => expect(m.deleteSession).toHaveBeenCalledWith("A"));

    // The conversation is gone: the request it was streaming is aborted, which
    // is what stops the page waiting on it, and a text event that was already
    // on the wire no longer lands anywhere the view can read it back from.
    expect(streams[0].aborted()).toBe(true);
    await act(async () => streams[0].onEvent({ type: "text", content: "迟到的输出" }));
    expect(screen.queryByText("迟到的输出")).toBeNull();
  });
});
