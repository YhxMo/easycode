import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import App from "../App";
import * as api from "../api";
import { primeApiMock } from "./helpers";

// 回归：后端只返回 has_api_key/key_tail（绝不下发明文）。已有密钥时“清除密钥”
// 必须可用；输入框只承载新输入，留空保存省略 api_key，清除走 clear_key。
vi.mock("../api", async () => (await import("./helpers")).apiMock);

const m = vi.mocked(api);

const MODELS: api.ModelsInfo = {
  default: "alpha",
  models: { alpha: { model: "m-alpha", key_id: "model-1" } },
  providers: { alpha: "openai" },
  limits: {},
};

const DETAIL: api.EditableModel = {
  alias: "alpha",
  model: "m-alpha",
  provider: "openai",
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
  m.updateModel.mockResolvedValue(MODELS);
});

async function openEdit(user: ReturnType<typeof userEvent.setup>) {
  render(<App />);
  await screen.findByText("m-alpha");
  await user.click(document.querySelector(".model-trigger") as HTMLElement);
  await user.click(document.querySelector(".model-detail-icon.edit") as HTMLElement);
  await screen.findByText("编辑模型");
}

function lastUpdateBody(): api.UpdateModelBody {
  const call = m.updateModel.mock.calls.at(-1);
  expect(call).toBeTruthy();
  return (call as [string, api.UpdateModelBody])[1];
}

describe("模型编辑 · 密钥接口", () => {
  it("已有密钥：清除密钥可用，留空保存省略 api_key（保留旧密钥）", async () => {
    const user = userEvent.setup();
    await openEdit(user);

    const clear = screen.getByRole("button", { name: "清除密钥" }) as HTMLButtonElement;
    expect(clear.disabled).toBe(false);
    expect((screen.getByLabelText("API Key") as HTMLInputElement).value).toBe("");

    await user.click(screen.getByRole("button", { name: "保存" }));
    await waitFor(() => expect(m.updateModel).toHaveBeenCalledTimes(1));
    const body = lastUpdateBody();
    expect("api_key" in body).toBe(false);
    expect(body.clear_key).toBe(false);
  });

  it("替换：输入新密钥后保存只发送新输入", async () => {
    const user = userEvent.setup();
    await openEdit(user);

    await user.type(screen.getByLabelText("API Key"), "sk-new-key");
    await user.click(screen.getByRole("button", { name: "保存" }));
    await waitFor(() => expect(m.updateModel).toHaveBeenCalledTimes(1));
    const body = lastUpdateBody();
    expect(body.api_key).toBe("sk-new-key");
    expect(body.clear_key).toBe(false);
  });

  it("清除：点击清除密钥后保存走 clear_key 且不发送 api_key", async () => {
    const user = userEvent.setup();
    await openEdit(user);

    await user.click(screen.getByRole("button", { name: "清除密钥" }));
    expect(screen.getByText("保存后将删除该模型凭据")).toBeTruthy();
    await user.click(screen.getByRole("button", { name: "保存" }));
    await waitFor(() => expect(m.updateModel).toHaveBeenCalledTimes(1));
    const body = lastUpdateBody();
    expect(body.clear_key).toBe(true);
    expect("api_key" in body).toBe(false);
  });
});
