import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { FilePreview } from "../components/primitives/FilePreview";
import * as api from "../api";
import { deferred } from "./helpers";

// The pane keys the preview by conversation *and* path (see App), so a file
// with the same relative name in another conversation mounts a fresh instance
// instead of showing the previous one's text.

afterEach(() => {
  vi.restoreAllMocks();
});

describe("FilePreview", () => {
  it("切换会话的同名文件立刻回到加载态，不被旧响应覆盖", async () => {
    const first = deferred<api.FileContent>();
    const second = deferred<api.FileContent>();
    const fetchFileContent = vi
      .spyOn(api, "fetchFileContent")
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(second.promise);

    const { rerender } = render(
      <FilePreview key={"s1:src/a.ts"} sessionId="s1" path="src/a.ts" onBack={() => {}} />,
    );
    expect(screen.getByText("正在读取…")).toBeTruthy();

    rerender(<FilePreview key={"s2:src/a.ts"} sessionId="s2" path="src/a.ts" onBack={() => {}} />);
    expect(screen.getByText("正在读取…")).toBeTruthy();

    // The first conversation's reply lands after the switch: it must not paint.
    first.resolve({
      path: "src/a.ts",
      text: "s1 的内容",
      start_line: 1,
      total_lines: 1,
      truncated: false,
    });
    await Promise.resolve();
    expect(screen.queryByText("s1 的内容")).toBeNull();

    second.resolve({
      path: "src/a.ts",
      text: "s2 的内容",
      start_line: 1,
      total_lines: 1,
      truncated: false,
    });
    await screen.findByText("s2 的内容");
    expect(fetchFileContent).toHaveBeenCalledTimes(2);
  });

  it("读取失败显示原因，不用空内容冒充文件", async () => {
    vi.spyOn(api, "fetchFileContent").mockRejectedValue(new Error("path is outside the workspace"));
    render(<FilePreview key={"s1:x"} sessionId="s1" path="x" onBack={() => {}} />);
    await waitFor(() =>
      expect(screen.getByText(/path is outside the workspace/)).toBeTruthy(),
    );
  });
});
