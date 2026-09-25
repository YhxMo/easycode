import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import App from "../App";
import * as api from "../api";
import { primeApiMock } from "./helpers";

// 回归：添加与编辑曾各有一套弹窗，"保存失败"只在 App toast 上出现、输入随弹窗
// 一起消失，必填字段只在按钮 disabled 上体现，供应商默认写死 openai 造成错误分组。
vi.mock("../api", async () => (await import("./helpers")).apiMock);

const m = vi.mocked(api);

const MODELS: api.ModelsInfo = {
  default: "alpha",
  models: { alpha: { model: "m-alpha" } },
  providers: { alpha: "openai" },
  limits: {},
};

const DETAIL: api.EditableModel = {
  alias: "alpha",
  model: "m-alpha",
  provider: "bailian",
  base_url: "http://127.0.0.1:9000/v1",
  api_format: "openai_compatible",
  has_api_key: true,
  key_tail: "here",
};

beforeEach(() => {
  primeApiMock(m);
  m.fetchSessions.mockResolvedValue([]);
  m.fetchModels.mockResolvedValue(MODELS);
  m.fetchModel.mockResolvedValue(DETAIL);
});

async function openAdd(user: ReturnType<typeof userEvent.setup>) {
  render(<App />);
  await screen.findByText("m-alpha");
  await user.click(document.querySelector(".model-trigger") as HTMLElement);
  await user.click(await screen.findByRole("button", { name: /添加模型/ }));
  await screen.findByRole("dialog");
}

async function openEdit(user: ReturnType<typeof userEvent.setup>) {
  render(<App />);
  await screen.findByText("m-alpha");
  await user.click(document.querySelector(".model-trigger") as HTMLElement);
  await user.click(await screen.findByRole("button", { name: "编辑 alpha" }));
  await screen.findByRole("dialog");
}

/** React 只认原生 setter 之后的 input 事件，直接赋值不会触发 onChange。 */
function typeInto(label: string, value: string) {
  const input = screen.getByLabelText(label) as HTMLInputElement;
  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value")?.set;
  setter?.call(input, value);
  input.dispatchEvent(new Event("input", { bubbles: true }));
  return input;
}

describe("模型表单 · 必填校验", () => {
  it("缺别名与模型 ID 时就地提示，且不发请求", async () => {
    const user = userEvent.setup();
    await openAdd(user);

    await user.click(screen.getByRole("button", { name: "添加" }));

    expect(await screen.findByText("请填写别名")).toBeTruthy();
    expect(screen.getByText("请填写模型 ID")).toBeTruthy();
    expect(m.addModel).not.toHaveBeenCalled();
    // 弹窗留着，输入框还在原处。
    expect(screen.getByRole("dialog")).toBeTruthy();

    // 填上之后就撤掉该字段的提示，不必等到再次提交。
    typeInto("别名", "my-gpt");
    expect(screen.queryByText("请填写别名")).toBeNull();
    expect(screen.getByText("请填写模型 ID")).toBeTruthy();
  });

  it("新增时供应商默认为空，不写死分组，也不发送该字段", async () => {
    const user = userEvent.setup();
    m.addModel.mockResolvedValue(MODELS);
    await openAdd(user);

    expect((screen.getByLabelText("供应商") as HTMLInputElement).value).toBe("");
    typeInto("别名", "my-gpt");
    typeInto("模型 ID", "openai/gpt-5");
    await user.click(screen.getByRole("button", { name: "添加" }));

    await waitFor(() => expect(m.addModel).toHaveBeenCalledTimes(1));
    const body = m.addModel.mock.calls[0][0] as api.AddModelBody;
    expect("provider" in body).toBe(false);
  });

  it("重复别名会被指出，但沿用后端既有语义（覆盖）而不是发不出请求", async () => {
    const user = userEvent.setup();
    m.addModel.mockResolvedValue(MODELS);
    await openAdd(user);

    typeInto("别名", "alpha");
    typeInto("模型 ID", "m-alpha-2");
    expect(await screen.findByText(/已存在，添加会覆盖/)).toBeTruthy();

    await user.click(screen.getByRole("button", { name: "添加" }));
    await waitFor(() => expect(m.addModel).toHaveBeenCalledTimes(1));
  });
});

describe("模型表单 · 失败反馈", () => {
  it("保存失败时弹窗保持打开，输入保留，原因写在表单里", async () => {
    const user = userEvent.setup();
    m.updateModel.mockRejectedValue(new Error("alias already exists: beta"));
    await openEdit(user);

    typeInto("别名", "beta");
    typeInto("模型 ID", "m-beta");
    await user.click(screen.getByRole("button", { name: "保存" }));

    const banner = await screen.findByText("alias already exists: beta");
    expect(banner.className).toContain("modal-error");
    expect(screen.getByRole("dialog")).toBeTruthy();
    expect((screen.getByLabelText("别名") as HTMLInputElement).value).toBe("beta");
    expect((screen.getByLabelText("模型 ID") as HTMLInputElement).value).toBe("m-beta");
  });

  it("编辑读取失败时说明原因，不打开空表单", async () => {
    const user = userEvent.setup();
    m.fetchModel.mockRejectedValue(new Error("gone"));
    render(<App />);
    await screen.findByText("m-alpha");
    await user.click(document.querySelector(".model-trigger") as HTMLElement);
    await user.click(await screen.findByRole("button", { name: "编辑 alpha" }));

    await waitFor(() => {
      expect(document.querySelector(".app-toast")?.textContent).toContain("读取失败：gone");
    });
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});

describe("模型表单 · 凭据提示", () => {
  it("已有密钥只给尾号提示，不回填明文，留空保存不动它", async () => {
    const user = userEvent.setup();
    m.updateModel.mockResolvedValue(MODELS);
    await openEdit(user);

    expect(screen.getByText(/已有密钥（尾号 here），留空保存不会改动它。/)).toBeTruthy();
    expect((screen.getByLabelText("API Key") as HTMLInputElement).value).toBe("");

    await user.click(screen.getByRole("button", { name: "保存" }));
    await waitFor(() => expect(m.updateModel).toHaveBeenCalledTimes(1));
    const body = m.updateModel.mock.calls[0][1] as api.UpdateModelBody;
    expect("api_key" in body).toBe(false);
  });

  it("清除按钮说的是连接凭据：连 Base URL 一起删除", async () => {
    const user = userEvent.setup();
    m.updateModel.mockResolvedValue(MODELS);
    await openEdit(user);

    await user.click(screen.getByRole("button", { name: "清除连接凭据" }));
    expect(screen.getByText("保存后删除该模型的 API Key 与 Base URL")).toBeTruthy();
    await user.click(screen.getByRole("button", { name: "保存" }));

    await waitFor(() => expect(m.updateModel).toHaveBeenCalledTimes(1));
    expect((m.updateModel.mock.calls[0][1] as api.UpdateModelBody).clear_key).toBe(true);
  });
});
