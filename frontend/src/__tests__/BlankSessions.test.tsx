import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import App from "../App";
import * as api from "../api";
import { composerField, primeApiMock, session, sidebarRow } from "./helpers";

// Blank conversations are real sessions from the moment they are asked for:
// they survive a refresh, inherit the project on screen, and a tab close
// removes one that never ran while leaving a running one alone.
vi.mock("../api", async () => (await import("./helpers")).apiMock);

const m = vi.mocked(api);

const composer = () => composerField() as HTMLTextAreaElement;
/** Real session tabs: the start page's own tab has no close button. */
const tabTitles = () =>
  [...document.querySelectorAll(".tab-strip .tab")]
    .filter((t) => t.querySelector(".tab-close"))
    .map((t) => t.querySelector(".tab-title")?.textContent ?? "");
const activeTitle = () => document.querySelector(".tab.active .tab-title")?.textContent ?? "";
const labels = () => [...document.querySelectorAll<HTMLElement>(".tab-strip .tab-label")];

/** A streamChat mock that stays in flight until the test resolves it. */
function pendingStream() {
  m.streamChat.mockImplementation(
    () =>
      new Promise<void>(() => {
        /* never settles: the turn is still running when the tab closes */
      }),
  );
}

beforeEach(() => {
  primeApiMock(m);
  m.fetchSessions.mockResolvedValue([]);
  m.fetchModels.mockResolvedValue({
    default: "m",
    models: {},
    providers: {},
    limits: {},
  });
});

describe("App · 空白会话", () => {
  it("「新会话」立即创建真实会话，并继承当前会话的项目", async () => {
    const user = userEvent.setup();
    const proj = { ...session("A", "会话A"), root: "/repoA" };
    const created = { ...session("N1", "新会话"), root: "/repoA", started: false };
    m.fetchSessions.mockResolvedValue([proj, created]);
    m.fetchSession.mockImplementation(async (id: string) =>
      id === "N1" ? { ...created, messages: [] } : { ...proj, messages: [] },
    );
    m.createSession.mockResolvedValue(created);

    render(<App />);
    await screen.findByText("会话A");
    await user.click(sidebarRow("会话A"));
    await user.click(screen.getByRole("button", { name: "新建会话" }));

    // The project comes from the conversation on screen, not from the default.
    await waitFor(() => expect(m.createSession).toHaveBeenCalledWith("/repoA", expect.any(String)));
    await waitFor(() => expect(tabTitles()).toEqual(["会话A", "新会话"]));
    expect(activeTitle()).toBe("新会话");
    expect(composer().value).toBe("");
  });

  it("多个空白会话并存，刷新后标签、当前标签与草稿都回来", async () => {
    const user = userEvent.setup();
    const created: Record<string, api.SessionSummary> = {};
    m.fetchSessions.mockImplementation(async () => Object.values(created));
    m.createSession.mockImplementation(async () => {
      const s = { ...session(`n${Object.keys(created).length + 1}`, "新会话"), started: false };
      created[s.id] = s;
      return s;
    });
    m.fetchSession.mockImplementation(async (id: string) => ({
      ...created[id],
      messages: [],
    }));

    const view = render(<App />);
    await user.click(screen.getByRole("button", { name: "新建会话" }));
    await waitFor(() => expect(tabTitles()).toEqual(["新会话"]));
    await user.type(composer(), "第一个草稿");
    await user.click(screen.getByRole("button", { name: "新建会话" }));
    await waitFor(() => expect(tabTitles()).toEqual(["新会话", "新会话"]));
    // A new conversation starts with an empty composer: the other one's text
    // stays with the conversation it was typed into.
    expect(composer().value).toBe("");

    view.unmount();
    // The server has both by now: a reload is answered by the real list.
    m.fetchSessions.mockResolvedValue(Object.values(created));
    render(<App />);

    // Open tabs, the tab on screen and the unsent text all survive a reload.
    await waitFor(() => expect(tabTitles()).toEqual(["新会话", "新会话"]));
    // Wait for the remembered tab to come back before touching the strip: a
    // click that raced it would be replaced by the restore.
    await waitFor(() => expect(activeTitle()).toBe("新会话"));
    await waitFor(() =>
      expect(document.querySelectorAll(".tab.active .tab-close").length).toBe(1),
    );
    await user.click(labels()[0]);
    await waitFor(() => expect(composer().value).toBe("第一个草稿"));
  });

  it("发送与关闭同时发生：流已经占了这一格，只关闭视图", async () => {
    const user = userEvent.setup();
    const blank = { ...session("N1", "新会话"), started: false };
    m.fetchSessions.mockResolvedValue([blank]);
    m.fetchSession.mockResolvedValue({ ...blank, messages: [] });
    m.createSession.mockResolvedValue(blank);
    pendingStream();

    render(<App />);
    await user.click(screen.getByRole("button", { name: "新建会话" }));
    await waitFor(() => expect(tabTitles()).toEqual(["新会话"]));
    await user.type(composer(), "hi");
    await user.click(screen.getByRole("button", { name: /发送消息/ }));
    await waitFor(() => expect(m.streamChat).toHaveBeenCalled());

    await user.click(screen.getByRole("button", { name: "关闭 新会话" }));

    // The turn is running, so the session is not blank any more: nothing is
    // deleted and the turn keeps going in the background.
    expect(m.deleteSession).not.toHaveBeenCalled();
    await waitFor(() => expect(tabTitles()).toEqual([]));
    expect(document.querySelector(".session-item.running")).toBeTruthy();
  });

  it("关闭失败时保留标签并提示，不把会话留在看不见的地方", async () => {
    const user = userEvent.setup();
    const blank = { ...session("N1", "新会话"), started: false };
    m.fetchSessions.mockResolvedValue([blank]);
    m.fetchSession.mockResolvedValue({ ...blank, messages: [] });
    m.createSession.mockResolvedValue(blank);
    m.deleteSession.mockRejectedValue(new Error("文件占用"));

    render(<App />);
    await user.click(screen.getByRole("button", { name: "新建会话" }));
    await waitFor(() => expect(tabTitles()).toEqual(["新会话"]));

    await user.click(screen.getByRole("button", { name: "关闭 新会话" }));

    await screen.findByText("关闭会话失败: 文件占用");
    expect(tabTitles()).toEqual(["新会话"]);
  });
});
