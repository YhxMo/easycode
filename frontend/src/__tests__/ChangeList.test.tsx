import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ChangeList } from "../components/primitives/ChangeList";
import type { ChangeRow } from "../lib/pane";

// The working tree's rows carry counts, not diffs: the diff of a row arrives
// when the reader opens it, and again whenever the counts were re-read — a diff
// fetched against the previous reading would sit under numbers it does not match.

const row: ChangeRow = {
  key: "git:/ws/src/app.ts",
  label: "src/app.ts",
  added: 1,
  removed: 1,
  gitSource: { repo: "/ws", path: "src/app.ts" },
};

const deferred = () => {
  let resolve!: (v: { diff: string | null; diff_note: string | null }) => void;
  const promise = new Promise<{ diff: string | null; diff_note: string | null }>((res) => {
    resolve = res;
  });
  return { promise, resolve };
};

const other: ChangeRow = {
  ...row,
  key: "git:/ws/src/other.ts",
  label: "src/other.ts",
  gitSource: { repo: "/ws", path: "src/other.ts" },
};

describe("ChangeList · 按需取 diff", () => {
  it("展开时才请求，且只请求这一行", async () => {
    const user = userEvent.setup();
    const loadDiff = vi.fn().mockResolvedValue({ diff: "+a\n", diff_note: null });
    render(<ChangeList rows={[row]} loadDiff={loadDiff} statsVersion={1} />);

    expect(loadDiff).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: /src\/app\.ts/ }));
    await waitFor(() => expect(loadDiff).toHaveBeenCalledTimes(1));
    expect(loadDiff).toHaveBeenCalledWith(row);
    expect(screen.getByText("+a")).toBeTruthy();
  });

  it("收起再展开不重复请求", async () => {
    const user = userEvent.setup();
    const loadDiff = vi.fn().mockResolvedValue({ diff: "+a\n", diff_note: null });
    render(<ChangeList rows={[row]} loadDiff={loadDiff} statsVersion={1} />);

    await user.click(screen.getByRole("button", { name: /src\/app\.ts/ }));
    await waitFor(() => expect(loadDiff).toHaveBeenCalledTimes(1));
    await user.click(screen.getByRole("button", { name: /src\/app\.ts/ }));
    await user.click(screen.getByRole("button", { name: /src\/app\.ts/ }));
    await waitFor(() => expect(loadDiff).toHaveBeenCalledTimes(1));
  });

  it("统计重新读取后，展开的行按同一份快照重取", async () => {
    const user = userEvent.setup();
    const first = deferred();
    const loadDiff = vi.fn((r: ChangeRow) =>
      r.key === row.key
        ? Promise.resolve({ diff: "+first\n", diff_note: null })
        : first.promise,
    );
    const { rerender, container } = render(
      <ChangeList rows={[row, other]} loadDiff={loadDiff} statsVersion={1} />,
    );

    await user.click(screen.getByRole("button", { name: /other\.ts/ }));
    await waitFor(() => expect(screen.getByText("正在读取改动…")).toBeTruthy());

    // New counts: every open row asks again, and this second reply lands first.
    const second = vi.fn().mockResolvedValue({ diff: "+second\n", diff_note: null });
    loadDiff.mockImplementation(second);
    rerender(<ChangeList rows={[row, other]} loadDiff={loadDiff} statsVersion={2} />);
    await waitFor(() => expect(second).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.getByText("+second")).toBeTruthy());

    // The first reply arrives after it and must be discarded: a diff from the
    // previous reading under the current counts is a wrong answer, not an old
    // one.
    await act(async () => first.resolve({ diff: "+stale\n", diff_note: null }));
    expect(container.textContent).toContain("+second");
    expect(container.textContent).not.toContain("+stale");
  });

  it("失败时保留折叠位置并说明原因", async () => {
    const user = userEvent.setup();
    const loadDiff = vi.fn().mockRejectedValue(new Error("该文件没有未提交改动"));
    render(<ChangeList rows={[row]} loadDiff={loadDiff} statsVersion={1} />);

    await user.click(screen.getByRole("button", { name: /src\/app\.ts/ }));
    await waitFor(() => expect(screen.getByText(/该文件没有未提交改动/)).toBeTruthy());
    expect(screen.getByRole("button", { name: /src\/app\.ts/ })).toBeTruthy();
  });

  it("没有 diff 的回答说明原因，且不再重复请求", async () => {
    const user = userEvent.setup();
    const loadDiff = vi.fn().mockResolvedValue({ diff: null, diff_note: "改动过大，未生成预览" });
    render(<ChangeList rows={[row]} loadDiff={loadDiff} statsVersion={1} />);

    await user.click(screen.getByRole("button", { name: /src\/app\.ts/ }));
    await waitFor(() => expect(screen.getByText("改动过大，未生成预览")).toBeTruthy());
    // "No diff" is an answer: collapsing and reopening must not ask again.
    await user.click(screen.getByRole("button", { name: /src\/app\.ts/ }));
    await user.click(screen.getByRole("button", { name: /src\/app\.ts/ }));
    expect(screen.getByText("改动过大，未生成预览")).toBeTruthy();
    expect(loadDiff).toHaveBeenCalledTimes(1);
  });

  it("既没有 diff 也没有原因时不留空白", async () => {
    const user = userEvent.setup();
    const loadDiff = vi.fn().mockResolvedValue({ diff: null, diff_note: null });
    render(<ChangeList rows={[row]} loadDiff={loadDiff} statsVersion={1} />);

    await user.click(screen.getByRole("button", { name: /src\/app\.ts/ }));
    await waitFor(() => expect(screen.getByText("没有可显示的改动。")).toBeTruthy());
  });

  it("失败后重新展开会再试一次", async () => {
    const user = userEvent.setup();
    const loadDiff = vi
      .fn()
      .mockRejectedValueOnce(new Error("网络中断"))
      .mockResolvedValue({ diff: "+retry\n", diff_note: null });
    render(<ChangeList rows={[row]} loadDiff={loadDiff} statsVersion={1} />);

    await user.click(screen.getByRole("button", { name: /src\/app\.ts/ }));
    await waitFor(() => expect(screen.getByText(/网络中断/)).toBeTruthy());
    await user.click(screen.getByRole("button", { name: /src\/app\.ts/ }));
    await user.click(screen.getByRole("button", { name: /src\/app\.ts/ }));
    await waitFor(() => expect(screen.getByText("+retry")).toBeTruthy());
    expect(loadDiff).toHaveBeenCalledTimes(2);
  });

  it("统计刷新前发出、已收起那一行的迟到回复不会落下", async () => {
    const user = userEvent.setup();
    const first = deferred();
    const loadDiff = vi.fn().mockReturnValueOnce(first.promise);
    const { rerender, container } = render(
      <ChangeList rows={[row]} loadDiff={loadDiff} statsVersion={1} />,
    );

    // Opened and closed again while its request is still out.
    await user.click(screen.getByRole("button", { name: /src\/app\.ts/ }));
    await user.click(screen.getByRole("button", { name: /src\/app\.ts/ }));
    rerender(<ChangeList rows={[row]} loadDiff={loadDiff} statsVersion={2} />);
    await act(async () => first.resolve({ diff: "+stale\n", diff_note: null }));

    // Reopened under the new counts: it asks again and shows only that answer.
    loadDiff.mockResolvedValue({ diff: "+fresh\n", diff_note: null });
    await user.click(screen.getByRole("button", { name: /src\/app\.ts/ }));
    await waitFor(() => expect(screen.getByText("+fresh")).toBeTruthy());
    expect(container.textContent).not.toContain("+stale");
    expect(loadDiff).toHaveBeenCalledTimes(2);
  });

  it("没有可打开的来源时不请求也不展开", async () => {
    const loadDiff = vi.fn();
    const staticRow: ChangeRow = {
      key: "git:/ws/logo.png",
      label: "logo.png",
      added: 0,
      removed: 0,
      note: "二进制文件",
    };
    render(<ChangeList rows={[staticRow]} loadDiff={loadDiff} statsVersion={1} />);

    expect(screen.queryByRole("button", { name: /logo\.png/ })).toBeNull();
    expect(screen.getByText("二进制文件")).toBeTruthy();
    expect(loadDiff).not.toHaveBeenCalled();
  });
});
