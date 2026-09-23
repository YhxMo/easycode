import { afterEach, describe, expect, it, vi } from "vitest";
import { deleteSession, fetchSessions, saveProject } from "../api";

function mockFetch(response: { ok: boolean; status: number; json: () => Promise<unknown> }) {
  const fn = vi.fn().mockResolvedValue(response);
  vi.stubGlobal("fetch", fn);
  return fn;
}

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
});
