import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { SkillView, SkillsInfo } from "../api";
import * as api from "../api";
import { ExtensionsSettings } from "../ExtensionsSettings";

vi.mock("../api", async () => (await import("./helpers")).apiMock);

const m = vi.mocked(api);

const PROJECT = "/ws/proj";

function skill(overrides: Partial<SkillView> = {}): SkillView {
  return {
    name: "review",
    description: "检查代码",
    scope: "project",
    path: `${PROJECT}/.easycode/skills/review/SKILL.md`,
    directory: `${PROJECT}/.easycode/skills/review`,
    effective: true,
    ...overrides,
  };
}

function info(overrides: Partial<SkillsInfo> = {}): SkillsInfo {
  return {
    root: PROJECT,
    enabled: true,
    install_roots: {
      personal: "/home/u/.easycode/skills",
      project: `${PROJECT}/.easycode/skills`,
    },
    skills: [skill()],
    errors: [],
    ...overrides,
  };
}

function renderPanel(onToast = vi.fn(), onChanged = vi.fn()) {
  render(
    <ExtensionsSettings
      root={PROJECT}
      sessionId={null}
      initialTab="skills"
      projectName="项目 P"
      projectPath={PROJECT}
      onClose={vi.fn()}
      onChanged={onChanged}
      onToast={onToast}
    />,
  );
  return { onToast, onChanged };
}

/** Open the import form and type a source folder. */
async function openForm(user: ReturnType<typeof userEvent.setup>, path = "/src/pack") {
  await user.click(await screen.findByRole("button", { name: "添加 Skill" }));
  if (path) await user.type(screen.getByLabelText(/源文件夹/), path);
}

beforeEach(() => {
  vi.clearAllMocks();
  m.fetchSkills.mockResolvedValue(info());
  m.fetchMcp.mockResolvedValue({
    root: PROJECT,
    projects: [],
    scopes: [],
    servers: [],
    errors: [],
  });
  m.fetchMcpStatus.mockResolvedValue({ started: false, servers: [] });
  m.chooseWorkspace.mockResolvedValue({ paths: [], supported: true });
});

describe("扩展弹窗 · Skills 面板", () => {
  it("列出已安装的 Skill，带来源、命令名与目录", async () => {
    m.fetchSkills.mockResolvedValue(
      info({
        skills: [
          skill(),
          skill({
            name: "review",
            description: "个人版本",
            scope: "personal",
            directory: "/home/u/.easycode/skills/review",
            path: "/home/u/.easycode/skills/review/SKILL.md",
            effective: false,
          }),
        ],
      }),
    );
    renderPanel();

    expect(await screen.findByText("检查代码")).toBeTruthy();
    expect(screen.getByText("个人版本")).toBeTruthy();
    // Both copies are listed; the shadowed one says so instead of disappearing.
    expect(screen.getAllByText("review")).toHaveLength(2);
    expect(screen.getByText("被同名项覆盖")).toBeTruthy();
    expect(screen.getAllByText("/review")).toHaveLength(2);
    expect(screen.getByText(`${PROJECT}/.easycode/skills/review`)).toBeTruthy();
    expect(screen.getByText("个人")).toBeTruthy();
  });

  it("空态、坏条目与失败重试各自独立", async () => {
    m.fetchSkills.mockResolvedValue(info({ skills: [], errors: ["broken: 缺少 description"] }));
    const { unmount } = render(
      <>
        <ExtensionsSettings
          root={PROJECT}
          sessionId={null}
          initialTab="skills"
          projectName="项目 P"
          projectPath={PROJECT}
          onClose={vi.fn()}
          onChanged={vi.fn()}
          onToast={vi.fn()}
        />
      </>,
    );
    expect(await screen.findByText("还没有安装任何 Skill。")).toBeTruthy();
    expect(screen.getByText(/broken: 缺少 description/)).toBeTruthy();
    unmount();

    m.fetchSkills.mockRejectedValue(new Error("boom"));
    renderPanel();
    expect(await screen.findByText(/读取 Skill 失败/)).toBeTruthy();
    m.fetchSkills.mockResolvedValue(info());
    await userEvent.setup().click(screen.getByRole("button", { name: "重试" }));
    expect(await screen.findByText("检查代码")).toBeTruthy();
  });

  it("配置禁用时说明调用已关闭，但仍可导入", async () => {
    m.fetchSkills.mockResolvedValue(info({ enabled: false }));
    const { onChanged } = renderPanel();
    expect(await screen.findByText(/配置里已关闭 Skill/)).toBeTruthy();

    m.importSkill.mockResolvedValue({
      imported: skill({ name: "late" }),
      root: PROJECT,
      warnings: [],
    });
    m.fetchSkills.mockResolvedValue(info({ enabled: false, skills: [skill({ name: "late" })] }));
    const user = userEvent.setup();
    await openForm(user, "/src/late");
    await user.click(screen.getByRole("button", { name: "导入" }));

    await waitFor(() => expect(m.importSkill).toHaveBeenCalled());
    await waitFor(() => expect(onChanged).toHaveBeenCalled());
  });

  it("选择器取消时不提交，不支持时保留手填路径", async () => {
    renderPanel();
    const user = userEvent.setup();
    await openForm(user, "");

    // A cancelled picker answers with no path: the field stays as it was.
    m.chooseWorkspace.mockResolvedValue({ paths: [], supported: true });
    await user.click(screen.getByRole("button", { name: "选择文件夹" }));
    expect((screen.getByLabelText(/源文件夹/) as HTMLInputElement).value).toBe("");
    expect(m.importSkill).not.toHaveBeenCalled();

    m.chooseWorkspace.mockResolvedValue({ paths: ["/src/pack"], supported: true });
    await user.click(screen.getByRole("button", { name: "选择文件夹" }));
    await waitFor(() =>
      expect((screen.getByLabelText(/源文件夹/) as HTMLInputElement).value).toBe("/src/pack"),
    );
  });

  it("平台不支持选择器时给出提示，路径输入仍可用", async () => {
    const onToast = vi.fn();
    renderPanel(onToast);
    const user = userEvent.setup();
    await openForm(user, "");

    m.chooseWorkspace.mockResolvedValue({ paths: [], supported: false });
    await user.click(screen.getByRole("button", { name: "选择文件夹" }));

    await waitFor(() =>
      expect(onToast).toHaveBeenCalledWith("err", expect.stringContaining("不支持系统选择器")),
    );
    await user.type(screen.getByLabelText(/源文件夹/), "/typed/by/hand");
    expect((screen.getByLabelText(/源文件夹/) as HTMLInputElement).value).toBe("/typed/by/hand");
  });

  it("导入成功后清空表单、刷新列表并说明安装位置", async () => {
    const onToast = vi.fn();
    const { onChanged } = renderPanel(onToast);
    const user = userEvent.setup();
    await openForm(user, "/src/pack");

    const installed = skill({
      name: "pack",
      directory: `${PROJECT}/.easycode/skills/pack`,
      path: `${PROJECT}/.easycode/skills/pack/SKILL.md`,
    });
    m.importSkill.mockResolvedValue({ imported: installed, root: PROJECT, warnings: [] });
    m.fetchSkills.mockResolvedValue(info({ skills: [installed] }));

    await user.click(screen.getByRole("button", { name: "导入" }));

    await waitFor(() =>
      expect(m.importSkill).toHaveBeenCalledWith({
        source_path: "/src/pack",
        scope: "project",
        root: PROJECT,
      }),
    );
    await waitFor(() => expect(screen.getByText("pack")).toBeTruthy());
    expect(screen.queryByLabelText(/源文件夹/)).toBeNull();
    expect(onChanged).toHaveBeenCalled();
    expect(onToast).toHaveBeenCalledWith("ok", expect.stringContaining(installed.directory));
  });

  it("个人作用域用个人的安装目录预览", async () => {
    renderPanel();
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "添加 Skill" }));
    await user.selectOptions(screen.getByLabelText(/作用域/), "personal");
    await user.type(screen.getByLabelText(/源文件夹/), "/src/pack");

    // The preview root is the server's own answer for that scope, never a
    // path the browser guessed.
    expect(
      screen.getByText((_, el) =>
        Boolean(el?.tagName === "CODE" && el.textContent === "/home/u/.easycode/skills/pack"),
      ),
    ).toBeTruthy();
  });

  it("冲突时不关闭表单，也不改动列表", async () => {
    const onToast = vi.fn();
    renderPanel(onToast);
    const user = userEvent.setup();
    await openForm(user, "/src/pack");
    m.importSkill.mockRejectedValue(new Error("已存在同名目录: /ws/proj/.easycode/skills/pack"));

    await user.click(screen.getByRole("button", { name: "导入" }));

    await waitFor(() =>
      expect(onToast).toHaveBeenCalledWith("err", expect.stringContaining("已存在同名目录")),
    );
    // The input the user typed is still there: a refused import is not a
    // completed one, and re-importing is theirs to decide.
    expect((screen.getByLabelText(/源文件夹/) as HTMLInputElement).value).toBe("/src/pack");
    expect(screen.queryByRole("button", { name: "添加 Skill" })).toBeNull();
  });

  it("导入进行中不能重复提交", async () => {
    renderPanel();
    const user = userEvent.setup();
    await openForm(user, "/src/pack");
    let release!: (value: api.ImportSkillResult) => void;
    m.importSkill.mockReturnValue(
      new Promise<api.ImportSkillResult>((resolve) => {
        release = resolve;
      }),
    );

    await user.click(screen.getByRole("button", { name: "导入" }));
    await waitFor(() => expect(m.importSkill).toHaveBeenCalledTimes(1));
    expect((screen.getByRole("button", { name: "导入" }) as HTMLButtonElement).disabled).toBe(true);

    await act(async () => {
      release({ imported: skill({ name: "pack" }), root: PROJECT, warnings: [] });
    });
    await waitFor(() => expect(screen.getByRole("button", { name: "添加 Skill" })).toBeTruthy());
    expect(m.importSkill).toHaveBeenCalledTimes(1);
  });

  it("安装已发布但会话刷新失败时给出警告，不当作失败", async () => {
    const onToast = vi.fn();
    const { onChanged } = renderPanel(onToast);
    const user = userEvent.setup();
    await openForm(user, "/src/pack");
    m.importSkill.mockResolvedValue({
      imported: skill({ name: "pack" }),
      root: PROJECT,
      warnings: ["已导入，但会话 s1 的扩展刷新失败: boom"],
    });
    m.fetchSkills.mockResolvedValue(info({ skills: [skill({ name: "pack" })] }));

    await user.click(screen.getByRole("button", { name: "导入" }));

    expect(await screen.findByText(/会话 s1 的扩展刷新失败/)).toBeTruthy();
    expect(onChanged).toHaveBeenCalled();
    // The install itself is a success, and the form is gone with it.
    expect(screen.queryByLabelText(/源文件夹/)).toBeNull();
    expect(onToast).toHaveBeenCalledWith("ok", expect.stringContaining("已安装"));
  });

  it("导入后不提供重复导入的重试入口", async () => {
    renderPanel();
    const user = userEvent.setup();
    await openForm(user, "/src/pack");
    m.importSkill.mockResolvedValue({
      imported: skill({ name: "pack" }),
      root: PROJECT,
      warnings: ["已导入，但会话 s1 的扩展刷新失败: boom"],
    });
    m.fetchSkills.mockResolvedValue(info({ skills: [skill({ name: "pack" })] }));

    await user.click(screen.getByRole("button", { name: "导入" }));

    expect(await screen.findByText(/刷新失败/)).toBeTruthy();
    // The panel only offers a fresh install, never "retry" the one that landed.
    expect(screen.getByRole("button", { name: "添加 Skill" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "重试" })).toBeNull();
  });
});

describe("扩展弹窗 · Skills 面板输入", () => {
  it("MCP 面板的连接状态不因打开 Skills 页签而读取", async () => {
    renderPanel();
    expect(await screen.findByText("检查代码")).toBeTruthy();
    expect(m.fetchMcp).not.toHaveBeenCalled();
    act(() => {
      fireEvent.keyDown(window, { key: "Escape" });
    });
  });
});
