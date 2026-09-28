import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import App from "../app/App";
import * as api from "../api";
import { deferred, primeApiMock } from "./helpers";

// 回归：模型菜单曾被 `.chat-card` 的 overflow 裁掉。菜单现在通过 portal 挂到
// body，用视口坐标定位；删除改为二次确认；列表读取带序号，晚到的响应不能覆盖
// 已经成功的切换结果。
vi.mock("../api", async () => (await import("./helpers")).apiMock);

const m = vi.mocked(api);

const MODELS: api.ModelsInfo = {
  default: "alpha",
  models: { alpha: { model: "m-alpha" }, beta: { model: "m-beta" } },
  providers: { alpha: "openai", beta: "openai" },
  limits: {},
};

function trigger(): HTMLElement {
  return document.querySelector(".model-trigger") as HTMLElement;
}

function menu(): HTMLElement | null {
  return document.querySelector(".model-menu");
}

async function openMenu(user: ReturnType<typeof userEvent.setup>) {
  render(<App />);
  await screen.findByText("m-alpha");
  await user.click(trigger());
  await screen.findByText("beta");
}

beforeEach(() => {
  primeApiMock(m);
  m.fetchSessions.mockResolvedValue([]);
  m.fetchModels.mockResolvedValue(MODELS);
});

describe("模型菜单 · 定位与裁剪", () => {
  it("菜单挂到 body 上，不再受聊天卡片的 overflow 影响", async () => {
    const user = userEvent.setup();
    await openMenu(user);

    const panel = menu();
    expect(panel).toBeTruthy();
    expect(panel?.parentElement).toBe(document.body);
    expect(document.querySelector(".chat-card")?.contains(panel as Node)).toBe(false);
  });

  it("打开时按视口写入定位与最大高度", async () => {
    const user = userEvent.setup();
    await openMenu(user);

    const style = (menu() as HTMLElement).style;
    expect(style.position === "" || style.position === "fixed").toBe(true);
    expect(style.maxHeight).not.toBe("");
    expect(style.width).not.toBe("");
    // Anchored by exactly one vertical edge, so the panel is never stretched.
    expect([style.top, style.bottom].filter((v) => v !== "" && v !== "auto")).toHaveLength(1);
  });

  it("点击菜单内部不会误关（portal 后触发器与菜单同属内部区域）", async () => {
    const user = userEvent.setup();
    await openMenu(user);

    await user.click(screen.getByText("切换模型"));
    expect(menu()).toBeTruthy();
  });

  it("Escape 关闭菜单并把焦点交还触发器", async () => {
    const user = userEvent.setup();
    await openMenu(user);

    await user.keyboard("{Escape}");
    await waitFor(() => expect(menu()).toBeNull());
    expect(document.activeElement).toBe(trigger());
  });

  it("从菜单打开表单弹窗后关闭，焦点回到仍然存在的触发器", async () => {
    const user = userEvent.setup();
    await openMenu(user);

    // The menu entry that opened the dialog is gone by the time it closes, so
    // focus has to land on the control that is still on screen.
    await user.click(screen.getByRole("button", { name: /添加模型/ }));
    await screen.findByRole("dialog");
    await user.keyboard("{Escape}");

    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(document.activeElement).toBe(trigger());
  });
});

describe("模型菜单 · 删除确认", () => {
  it("取消不发请求，确认后才调用删除接口", async () => {
    const user = userEvent.setup();
    m.deleteModel.mockResolvedValue(MODELS);
    await openMenu(user);

    await user.click(screen.getByRole("button", { name: "删除 beta" }));
    const dialog = await screen.findByRole("dialog");
    expect(dialog.textContent).toContain("保留全部历史");

    await user.click(screen.getByRole("button", { name: "取消" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(m.deleteModel).not.toHaveBeenCalled();

    // 取消只关掉确认框；再次删除要重新打开菜单，且此时才真正发出请求。
    await user.click(trigger());
    await user.click(await screen.findByRole("button", { name: "删除 beta" }));
    await screen.findByRole("dialog");
    await user.click(screen.getByRole("button", { name: "确认删除" }));
    await waitFor(() => expect(m.deleteModel).toHaveBeenCalledWith("beta"));
  });

  it("删除失败时说明原因，不装作成功", async () => {
    const user = userEvent.setup();
    m.deleteModel.mockRejectedValue(new Error("模型正在使用中"));
    await openMenu(user);

    await user.click(screen.getByRole("button", { name: "删除 beta" }));
    await screen.findByRole("dialog");
    await user.click(screen.getByRole("button", { name: "确认删除" }));

    await waitFor(() => {
      expect(document.querySelector(".app-toast")?.textContent).toContain("模型正在使用中");
    });
  });
});

describe("模型菜单 · 列表读取", () => {
  it("晚到的读取结果不覆盖已经成功的切换", async () => {
    const user = userEvent.setup();
    const stale = deferred<api.ModelsInfo>();
    // First call (mount) resolves; the refresh that opening the menu fires is
    // held open until after the switch has already landed.
    m.fetchModels.mockResolvedValueOnce(MODELS).mockReturnValueOnce(stale.promise);
    m.switchModel.mockResolvedValue({ ...MODELS, default: "beta" });

    render(<App />);
    await screen.findByText("m-alpha");
    await user.click(trigger());
    await screen.findByText("beta");
    await user.click(screen.getByRole("button", { name: "beta" }));
    await waitFor(() => expect(m.switchModel).toHaveBeenCalledWith("beta"));

    // The old list, arriving after the switch has already landed: flushing it
    // inside act() is what makes the write actually reach the component, so the
    // assertion below would fail if the guard were removed.
    await act(async () => {
      stale.resolve(MODELS);
    });
    expect(document.querySelector(".model-trigger .dropdown-value")?.textContent).toBe("m-beta");
  });

  it("读取失败会说明，而不是被空 catch 吞掉", async () => {
    const user = userEvent.setup();
    // First call is the mount read; the refresh fired by opening the menu is
    // the one that fails.
    m.fetchModels.mockResolvedValueOnce(MODELS).mockRejectedValueOnce(new Error("boom"));

    render(<App />);
    await screen.findByText("m-alpha");
    await user.click(trigger());

    await waitFor(() => {
      expect(document.querySelector(".app-toast")?.textContent).toContain("读取模型列表失败");
    });
  });
});
