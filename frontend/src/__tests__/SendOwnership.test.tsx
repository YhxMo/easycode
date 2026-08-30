import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import App from "../App";
import * as api from "../api";

// Regression tests: a new-session first-turn stream whose
// finally block hijacks the foreground session ownership.
//
// Repro: user is in a brand-new session (currentId === null), sends a prompt,
// and while the stream is still in flight (before the backend emits the
// "session" event that binds the currentId) clicks "新会话" again to switch the
// foreground to another blank session. The old stream's finally then runs
// `setCurrentId(list[0].id)` unconditionally, silently re-routing the user's
// input in the blank session to the previous stream's newly-created session.
//
// These tests drive App purely against a mocked ./api (no network, no real
// ~/.easycode) and hold the SSE stream open with a controllable promise, so the
// navigation happens deterministically before the turn completes.
vi.mock("../api", () => ({
  fetchSessions: vi.fn(),
  fetchArchivedSessions: vi.fn(),
  fetchSession: vi.fn(),
  fetchWorkspaces: vi.fn(),
  fetchModels: vi.fn(),
  fetchCommands: vi.fn(),
  setSessionPermission: vi.fn(),
  streamChat: vi.fn(),
  undoSession: vi.fn(),
  redoSession: vi.fn(),
  submitApproval: vi.fn(),
  cancelSessionChat: vi.fn(),
  deleteSession: vi.fn(),
  archiveProjectChats: vi.fn(),
  createWorktree: vi.fn(),
  pinProject: vi.fn(),
  removeProject: vi.fn(),
  revealInFinder: vi.fn(),
  saveProject: vi.fn(),
  setSessionArchived: vi.fn(),
}));

const m = vi.mocked(api);

function session(id: string, title: string): api.SessionSummary {
  return { id, title, created_at: "2026-01-01T00:00:00Z", model_alias: "m", permission_mode: "ask" };
}

/**
 * A streamChat mock that captures `onEvent` and fails the test if a second
 * stream is started (each send should spawn exactly one stream).
 */
function controllableStream() {
  let onEvent: ((e: api.ChatEvent) => void) | undefined;
  let resolve!: () => void;
  const done = new Promise<void>((res) => {
    resolve = res;
  });
  m.streamChat.mockImplementation((_sid, _msg, cb, _opts) => {
    onEvent = cb;
    return done;
  });
  return {
    get: () => onEvent,
    /** Resolve the in-flight stream so send()'s finally block runs. */
    finish: () =>
      act(async () => {
        resolve();
      }),
  };
}

/** The foreground conversation title rendered in the chat header. */
function headerTitle(): string {
  return document.querySelector(".chat-context strong")?.textContent ?? "";
}

beforeEach(() => {
  vi.clearAllMocks();
  m.fetchSessions.mockResolvedValue([session("A", "会话A")]);
  m.fetchArchivedSessions.mockResolvedValue([]);
  m.fetchWorkspaces.mockResolvedValue({ default: "", projects: [] });
  m.fetchModels.mockResolvedValue({ default: "deepseek-v4flash", models: {} });
  m.fetchCommands.mockResolvedValue({ commands: [] });
  m.fetchSession.mockResolvedValue({ ...session("A", "会话A"), messages: [] });
  // streamChat/resolve default so a stray call doesn't hit the network.
  m.streamChat.mockResolvedValue(undefined);
  m.setSessionPermission.mockImplementation(async (_id, mode) => ({ id: "A", permission_mode: mode }));
  m.undoSession.mockResolvedValue({ ok: true });
  m.redoSession.mockResolvedValue({ ok: true });
  m.submitApproval.mockResolvedValue(undefined);
});

describe("App · 会话归属", () => {
  it("流进行中点「新会话」后，旧流的 finally 不得把前台 currentId 劫持回已完成的会话", async () => {
    const user = userEvent.setup();
    const stream = controllableStream();

    render(<App />);
    // Sidebar shows the existing session; foreground is still the blank
    // new session (currentId === null -> header "新会话").
    await screen.findByText("会话A");
    expect(headerTitle()).toBe("新会话");

    // Send from the new session (sessionId = null); stream stays in flight and
    // crucially has NOT emitted a "session" event yet.
    await user.type(screen.getByRole("textbox"), "hello");
    await user.click(screen.getByRole("button", { name: /发送消息/ }));
    await waitFor(() => expect(stream.get()).toBeTruthy());

    // While the stream is still pending, the user clicks 新会话 to switch the
    // foreground to a fresh blank session again (openSession(null)). Because
    // the stream never bound a session, this does NOT abort it — the old
    // request token remains "active" and its finally would clear/hijack.
    await user.click(screen.getByRole("button", { name: /新会话/ }));
    expect(headerTitle()).toBe("新会话");

    // Now let the first turn complete; the old stream's finally runs.
    await stream.finish();

    // The completed (old) stream must refresh the list but must NOT take over
    // the view: the user's blank session stays the foreground owner.
    await waitFor(() => expect(headerTitle()).toBe("新会话"));
    expect(document.querySelector(".session-item.active")).toBeNull();
  });

  it("未中途导航时，新会话首轮流结束后仍回填新建会话 id（保持原行为）", async () => {
    const user = userEvent.setup();
    const stream = controllableStream();

    render(<App />);
    await screen.findByText("会话A");
    expect(headerTitle()).toBe("新会话");

    await user.type(screen.getByRole("textbox"), "bye");
    await user.click(screen.getByRole("button", { name: /发送消息/ }));
    await waitFor(() => expect(stream.get()).toBeTruthy());

    // No navigation whatsoever: the fallback must still claim the newly
    // created session (list[0].id) so the turn is owned properly.
    await stream.finish();

    await waitFor(() => expect(headerTitle()).toBe("会话A"));
    expect(document.querySelector(".session-item.active")?.textContent).toContain("会话A");
  });
});
