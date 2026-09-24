import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import App from "../App";
import * as api from "../api";
import { primeApiMock } from "./helpers";

// Finder-choose failures used to be reported twice: a local `.picker-error`
// (easy to miss when the sidebar is collapsed or a modal covers it) plus the
// App-level toast. DEC-F1 keeps a single error channel: the global toast via
// the `onError` prop.
//
// Test drives App purely against a mocked ./api (easycode-audit rule 3: no
// network, no real ~/.easycode).
vi.mock("../api", async () => (await import("./helpers")).apiMock);

const m = vi.mocked(api);

beforeEach(() => {
  primeApiMock(m);
  m.fetchSessions.mockResolvedValue([]);
  m.fetchModels.mockResolvedValue({ default: "deepseek-v4flash", models: {}, providers: {}, limits: {} });
});

describe("App · 访达选择失败冒泡", () => {
  it("chooseWorkspace 拒绝后，App 级 toast 出现错误文案", async () => {
    const user = userEvent.setup();
    m.chooseWorkspace.mockRejectedValue(new Error("访达引擎不可用"));

    render(<App />);
    // Foreground is a blank new session (currentId === null), so the sidebar
    // shows the ProjectPicker with its "用访达添加主目录" button.
    await screen.findByRole("button", { name: "用访达添加主目录" });

    await user.click(screen.getByRole("button", { name: "用访达添加主目录" }));

    // The error must bubble through ProjectPicker.onError to the app toast
    // instead of living only in the local .picker-error.
    await waitFor(() => {
      const toast = document.querySelector(".app-toast");
      expect(toast?.textContent).toContain("访达引擎不可用");
    });
    // DEC-F1: no local duplicate — the error lives only in the global toast.
    expect(document.querySelector(".picker-error")).toBeNull();
  });
});
