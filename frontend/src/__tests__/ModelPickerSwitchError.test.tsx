import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import App from "../App";
import * as api from "../api";
import { composerField, primeApiMock, sidebarRow } from "./helpers";

// Switching to a model without a configured API key returns
// HTTP 422 and the old code closed the menu / only logged, so the user believed
// the switch succeeded while still running on the previous model. The
// ModelPicker now reports the failure through `onError` to the App toast AND
// keeps the old model highlighted (onChange is only called on success).
//
// Test drives App purely against a mocked ./api (easycode-audit rule 3: no
// network, no real ~/.easycode).
vi.mock("../api", async () => (await import("./helpers")).apiMock);

const m = vi.mocked(api);

beforeEach(() => {
  primeApiMock(m);
  m.fetchSessions.mockResolvedValue([]);
  // Two models so a switch to a second one can be attempted.
  m.fetchModels.mockResolvedValue({
    default: "alpha",
    models: { alpha: { model: "m-alpha" }, beta: { model: "m-beta" } },
    providers: { alpha: "openai", beta: "openai" },
    limits: {},
  });
});

describe("App · 模型切换失败反馈", () => {
  it("switchModel 拒绝（422）后错误 toast 出现且旧模型保持选中", async () => {
    const user = userEvent.setup();
    m.switchModel.mockRejectedValue(new Error("no API key configured"));

    render(<App />);

    // ModelPicker loads alpha as the active model.
    await screen.findByText("m-alpha");

    // Open the model dropdown.
    const trigger = document.querySelector(".model-trigger") as HTMLElement;
    expect(trigger).toBeTruthy();
    await user.click(trigger);

    // Choose the beta model from the menu. Rows are labelled by alias — the
    // name every operation keys on — not by the model id.
    const beta = await screen.findByRole("button", { name: "beta" });
    fireEvent.click(beta);

    // The failure surfaces on the App toast with the err.message.
    await waitFor(() => {
      expect(document.querySelector(".app-toast")?.textContent).toContain("no API key configured");
    });

    // The old model (alpha) stays the active one — the switch must not have
    // moved selection to beta. The trigger's dropdown value still reads m-alpha.
    expect(document.querySelector(".model-trigger .dropdown-value")?.textContent).toBe("m-alpha");
  });
});

// The model menu is global: the trigger must name the conversation's own model,
// and a turn running anywhere on the page has to block switching (the backend
// rebinds every session and refuses while any of them is busy).
describe("App · 模型范围与并发", () => {
  it("前台会话显示自己的模型，不是全局默认", async () => {
    const user = userEvent.setup();
    m.fetchSessions.mockResolvedValue([
      {
        id: "s1",
        title: "会话A",
        created_at: "2026-01-01T00:00:00Z",
        model_alias: "beta",
        permission_mode: "ask",
      },
    ]);
    m.fetchSession.mockResolvedValue({
      id: "s1",
      title: "会话A",
      created_at: "2026-01-01T00:00:00Z",
      model_alias: "beta",
      permission_mode: "ask",
      messages: [],
    });

    render(<App />);
    await screen.findByText("会话A");
    await user.click(sidebarRow("会话A"));

    await waitFor(() =>
      expect(document.querySelector(".model-trigger .dropdown-value")?.textContent).toBe("m-beta"),
    );
  });

  it("有回合在运行时不能切换，菜单说明原因", async () => {
    const user = userEvent.setup();
    let release!: () => void;
    m.streamChat.mockImplementation(
      () =>
        new Promise<void>((res) => {
          release = res;
        }),
    );
    m.fetchSessions.mockResolvedValue([
      {
        id: "s1",
        title: "会话A",
        created_at: "2026-01-01T00:00:00Z",
        model_alias: "alpha",
        permission_mode: "ask",
      },
    ]);
    m.fetchSession.mockResolvedValue({
      id: "s1",
      title: "会话A",
      created_at: "2026-01-01T00:00:00Z",
      model_alias: "alpha",
      permission_mode: "ask",
      messages: [],
    });

    render(<App />);
    await screen.findByText("会话A");
    await user.click(sidebarRow("会话A"));
    await user.type(composerField(), "go");
    await user.click(screen.getByRole("button", { name: /发送消息/ }));
    await waitFor(() => expect(screen.getByText("正在工作")).toBeTruthy());

    await user.click(document.querySelector(".model-trigger") as HTMLElement);
    await screen.findByText("有会话正在运行，结束后才能切换");

    const beta = screen.getByRole("button", { name: "beta" }) as HTMLButtonElement;
    expect(beta.disabled).toBe(true);
    fireEvent.click(beta);
    expect(m.switchModel).not.toHaveBeenCalled();

    await act(async () => release());
  });

  it("切换成功后重新读取会话列表，让绑定与实际一致", async () => {
    const user = userEvent.setup();
    m.switchModel.mockResolvedValue({
      default: "beta",
      models: { alpha: { model: "m-alpha" }, beta: { model: "m-beta" } },
      providers: { alpha: "openai", beta: "openai" },
      limits: {},
    });

    render(<App />);
    await screen.findByText("m-alpha");
    const before = m.fetchSessions.mock.calls.length;

    await user.click(document.querySelector(".model-trigger") as HTMLElement);
    fireEvent.click(await screen.findByRole("button", { name: "beta" }));

    await waitFor(() => expect(m.switchModel).toHaveBeenCalledWith("beta"));
    await waitFor(() => expect(m.fetchSessions.mock.calls.length).toBeGreaterThan(before));
  });
});
