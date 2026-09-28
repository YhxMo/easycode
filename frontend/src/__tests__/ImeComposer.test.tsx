// 中文输入法（IME）：组词期间输入法拥有输入框，程序不能动光标、菜单或发送。
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import App from "../app/App";
import * as api from "../api";
import { composerField, primeApiMock } from "./helpers";

vi.mock("../api", async () => (await import("./helpers")).apiMock);

const m = vi.mocked(api);

beforeEach(() => {
  primeApiMock(m);
  m.fetchSessions.mockResolvedValue([]);
  m.fetchModels.mockResolvedValue({
    default: "deepseek-v4flash",
    models: {},
    providers: {},
    limits: {},
  });
});

/** Composition starts, types intermediate text, then commits a candidate. */
function compose(field: HTMLTextAreaElement, typed: string) {
  fireEvent.compositionStart(field);
  fireEvent.change(field, { target: { value: typed } });
}

describe("App · 中文输入法", () => {
  it("组词期间不改写光标，上屏后才同步一次", async () => {
    render(<App />);
    const field = composerField();
    // 先输入普通文本，草稿里记下光标位置
    fireEvent.change(field, { target: { value: "@s" } });
    const caretWrites = vi.spyOn(field, "setSelectionRange");

    compose(field, "@src");

    // 组词中途的拼音只进输入值：光标仍属于输入法，程序不得写回
    expect(field.value).toBe("@src");
    expect(caretWrites).not.toHaveBeenCalled();
    // 菜单也不跟着中途状态翻动
    expect(screen.queryByRole("listbox", { name: "工作区文件" })).toBeNull();

    fireEvent.compositionEnd(field, { data: "源码" });
    // 上屏后同步一次：菜单按真实查询打开，并（防抖后）发出查询
    await waitFor(() =>
      expect(screen.getByRole("listbox", { name: "工作区文件" })).toBeTruthy(),
    );
    await waitFor(() =>
      expect(m.fetchFiles).toHaveBeenCalledWith(
        null,
        { root: null, secondary: [] },
        "src",
      ),
    );
  });

  it("组词期间回车交给输入法，不会发送", async () => {
    render(<App />);
    const field = composerField();
    compose(field, "nihao");

    fireEvent.keyDown(field, { key: "Enter", isComposing: true });

    expect(m.streamChat).not.toHaveBeenCalled();
  });
});
