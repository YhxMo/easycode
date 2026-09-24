import { afterEach, describe, expect, it, vi } from "vitest";
import type { ChatEvent } from "../api";
import { deleteSession, fetchCommands, fetchSessions, saveProject, streamChat } from "../api";

function mockFetch(response: { ok: boolean; status: number; json: () => Promise<unknown> }) {
  const fn = vi.fn().mockResolvedValue(response);
  vi.stubGlobal("fetch", fn);
  return fn;
}

/** A 200 SSE response streaming the given chunks, then EOF. */
function streamFetch(chunks: Array<string | Uint8Array>) {
  const encoder = new TextEncoder();
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(
      new Response(
        new ReadableStream({
          start(controller) {
            for (const chunk of chunks) {
              controller.enqueue(typeof chunk === "string" ? encoder.encode(chunk) : chunk);
            }
            controller.close();
          },
        }),
        { status: 200, headers: { "Content-Type": "text/event-stream" } },
      ),
    ),
  );
}

const TOOL_START =
  'data: {"type":"tool_start","tool_call":{"id":"t1","name":"execute_shell","arguments":{}}}\n\n';

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("api 请求层", () => {
  it("200 时解析 JSON", async () => {
    mockFetch({ ok: true, status: 200, json: async () => [{ id: "s1" }] });
    await expect(fetchSessions()).resolves.toEqual([{ id: "s1" }]);
  });

  it("非 2xx 时抛出后端 detail", async () => {
    mockFetch({
      ok: false,
      status: 422,
      json: async () => ({ detail: "no API key configured" }),
    });
    await expect(fetchSessions()).rejects.toThrow("no API key configured");
  });

  it("非 JSON 错误响应回退到 HTTP 状态", async () => {
    mockFetch({
      ok: false,
      status: 500,
      json: async () => {
        throw new Error("not json");
      },
    });
    await expect(fetchSessions()).rejects.toThrow("HTTP 500");
  });

  it("POST 带正确的 headers 和 body", async () => {
    const fn = mockFetch({ ok: true, status: 200, json: async () => ({}) });
    await saveProject("/p", ["/s"], "sid", "name");
    expect(fn).toHaveBeenCalledWith("/api/workspaces/projects", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ root: "/p", secondary: ["/s"], session_id: "sid", name: "name" }),
    });
  });

  it("无 body 的 DELETE 不带 Content-Type", async () => {
    const fn = mockFetch({ ok: true, status: 200, json: async () => ({ ok: true }) });
    await deleteSession("s1");
    expect(fn).toHaveBeenCalledWith("/api/sessions/s1", { method: "DELETE" });
  });

  it("fetchCommands 区分未传与显式空 secondary_roots", async () => {
    const fn = mockFetch({ ok: true, status: 200, json: async () => ({ commands: [] }) });

    await fetchCommands(null, { root: "/p", secondary: [] });
    expect(fn).toHaveBeenLastCalledWith("/api/commands", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: null, root: "/p", secondary_roots: [] }),
    });

    await fetchCommands(null, { root: "/p", secondary: null });
    expect(JSON.parse((fn.mock.calls.at(-1)?.[1] as RequestInit).body as string)).toEqual({
      session_id: null,
      root: "/p",
      secondary_roots: null,
    });

    await fetchCommands("s1");
    expect(JSON.parse((fn.mock.calls.at(-1)?.[1] as RequestInit).body as string)).toEqual({
      session_id: "s1",
    });
  });
});

describe("streamChat SSE 收尾", () => {
  function collect() {
    const events: ChatEvent[] = [];
    return { events, onEvent: (e: ChatEvent) => events.push(e) };
  }

  it("收到 done 正常返回", async () => {
    streamFetch([TOOL_START, 'data: {"type":"done"}\n\n']);
    const { events, onEvent } = collect();
    await expect(streamChat(null, "hi", onEvent)).resolves.toBeUndefined();
    expect(events.map((e) => e.type)).toEqual(["tool_start", "done"]);
  });

  it("只有 tool_start 后 EOF：视为连接中断", async () => {
    streamFetch([TOOL_START]);
    const { events, onEvent } = collect();
    await expect(streamChat(null, "hi", onEvent)).rejects.toThrow("连接中断");
    expect(events.map((e) => e.type)).toEqual(["tool_start"]);
  });

  it("text 后 EOF：保留文本并报告未正常完成", async () => {
    streamFetch(['data: {"type":"text","content":"partial answer"}\n\n']);
    const { events, onEvent } = collect();
    await expect(streamChat(null, "hi", onEvent)).rejects.toThrow("连接中断");
    expect(events).toEqual([{ type: "text", content: "partial answer" }]);
  });

  it("error 后 EOF：不再报告中断", async () => {
    streamFetch(['data: {"type":"error","error":"boom"}\n\n']);
    const { events, onEvent } = collect();
    await expect(streamChat(null, "hi", onEvent)).resolves.toBeUndefined();
    expect(events).toEqual([{ type: "error", error: "boom" }]);
  });

  it("主动 cancelled 正常返回", async () => {
    streamFetch(['data: {"type":"cancelled"}\n\n']);
    await expect(streamChat(null, "hi", () => {})).resolves.toBeUndefined();
  });

  it("EOF 前的完整尾部事件（无空行分隔）仍被处理", async () => {
    streamFetch([TOOL_START, 'data: {"type":"done"}']);
    const { events, onEvent } = collect();
    await expect(streamChat(null, "hi", onEvent)).resolves.toBeUndefined();
    expect(events.map((e) => e.type)).toEqual(["tool_start", "done"]);
  });

  it("残缺 JSON 不静默丢弃：视为连接中断", async () => {
    streamFetch([TOOL_START, 'data: {"type":"do']);
    const { events, onEvent } = collect();
    await expect(streamChat(null, "hi", onEvent)).rejects.toThrow("连接中断");
    expect(events.map((e) => e.type)).toEqual(["tool_start"]);
  });

  it("JSON 分片与 UTF-8 跨块仍正确解析", async () => {
    const encoder = new TextEncoder();
    const payload = encoder.encode(
      'data: {"type":"text","content":"中文片段"}\n\ndata: {"type":"done"}\n\n',
    );
    const marker = encoder.encode("文")[0];
    const cut = payload.indexOf(marker) + 1; // split inside the multibyte char
    streamFetch([payload.slice(0, cut), payload.slice(cut)]);
    const { events, onEvent } = collect();
    await expect(streamChat(null, "hi", onEvent)).resolves.toBeUndefined();
    expect(events).toEqual([{ type: "text", content: "中文片段" }, { type: "done" }]);
  });

  it("错误响应复用后端 detail", async () => {
    mockFetch({ ok: false, status: 422, json: async () => ({ detail: "no API key" }) });
    await expect(streamChat(null, "hi", () => {})).rejects.toThrow("no API key");
  });
});
