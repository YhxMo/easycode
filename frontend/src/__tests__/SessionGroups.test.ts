import { describe, expect, it } from "vitest";
import { groupSessions } from "../features/sidebar/sessionGroups";
import type { SessionSummary } from "../api";
import { project } from "./helpers";

function s(id: string, root: string | null, created: string, extra: Partial<SessionSummary> = {}): SessionSummary {
  return {
    id,
    title: id,
    created_at: created,
    model_alias: "m",
    ...(root ? { root } : {}),
    ...extra,
  };
}

describe("groupSessions", () => {
  it("按工作目录归属：有根目录进项目，无根目录进最近", () => {
    const groups = groupSessions(
      [s("a", "/p/x", "2026-01-02T00:00:00Z"), s("b", null, "2026-01-01T00:00:00Z")],
      [project("/p/x")],
    );
    expect(groups.projects).toHaveLength(1);
    expect(groups.projects[0].sessions.map((x) => x.id)).toEqual(["a"]);
    expect(groups.recents.map((x) => x.id)).toEqual(["b"]);
  });

  it("置顶会话仍在项目成员表里，但项目只显示未置顶的那些", () => {
    const groups = groupSessions(
      [
        s("a", "/p/x", "2026-01-02T00:00:00Z", { pinned: true, pinned_at: "2026-02-01T00:00:00Z" }),
        s("b", "/p/x", "2026-01-01T00:00:00Z"),
      ],
      [project("/p/x")],
    );
    expect(groups.pinned.map((x) => x.id)).toEqual(["a"]);
    // The full membership is what ordering, running state and the project's own
    // actions read; it must keep every conversation.
    expect(groups.projects[0].sessions.map((x) => x.id)).toEqual(["a", "b"]);
    expect(groups.projects[0].visibleSessions.map((x) => x.id)).toEqual(["b"]);
  });

  it("无根目录的置顶会话只进置顶，不进最近", () => {
    const groups = groupSessions(
      [
        s("a", null, "2026-01-02T00:00:00Z", { pinned: true, pinned_at: "2026-02-01T00:00:00Z" }),
        s("b", null, "2026-01-01T00:00:00Z"),
      ],
      [],
    );
    expect(groups.pinned.map((x) => x.id)).toEqual(["a"]);
    expect(groups.recents.map((x) => x.id)).toEqual(["b"]);
  });

  it("置顶按 pinned_at 倒序，而不是创建时间", () => {
    const groups = groupSessions(
      [
        s("old", null, "2026-03-01T00:00:00Z", { pinned: true, pinned_at: "2026-01-01T00:00:00Z" }),
        s("new", null, "2026-01-01T00:00:00Z", { pinned: true, pinned_at: "2026-03-01T00:00:00Z" }),
      ],
      [],
    );
    expect(groups.pinned.map((x) => x.id)).toEqual(["new", "old"]);
  });

  it("没有会话的项目仍显示，置顶项目排在最前", () => {
    const groups = groupSessions(
      [s("a", "/p/x", "2026-03-01T00:00:00Z")],
      [project("/p/x"), project("/p/empty"), project("/p/pinned", { pinned: true, name: "置顶项目" })],
    );
    expect(groups.projects.map((g) => g.root)).toEqual(["/p/pinned", "/p/x", "/p/empty"]);
    expect(groups.projects[0].name).toBe("置顶项目");
    expect(groups.projects[2].sessions).toEqual([]);
  });

  it("项目名回退到目录名，项目内会话按创建时间倒序", () => {
    const groups = groupSessions(
      [s("older", "/p/x", "2026-01-01T00:00:00Z"), s("newer", "/p/x", "2026-02-01T00:00:00Z")],
      [project("/p/x")],
    );
    expect(groups.projects[0].name).toBe("x");
    expect(groups.projects[0].sessions.map((x) => x.id)).toEqual(["newer", "older"]);
  });

  it("最近分区按创建时间倒序", () => {
    const groups = groupSessions(
      [s("a", null, "2026-01-01T00:00:00Z"), s("b", null, "2026-03-01T00:00:00Z")],
      [],
    );
    expect(groups.recents.map((x) => x.id)).toEqual(["b", "a"]);
  });
});
