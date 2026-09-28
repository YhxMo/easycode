import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { McpInfo, McpServerView } from "../api";
import * as api from "../api";
import { ExtensionsSettings } from "../features/extensions/ExtensionsSettings";

vi.mock("../api", async () => (await import("./helpers")).apiMock);

const m = vi.mocked(api);

const PROJECT = "/ws/proj";

function info(overrides: Partial<McpInfo> = {}): McpInfo {
  return {
    root: PROJECT,
    projects: [{ root: PROJECT, name: "proj" }],
    scopes: [
      { scope: "personal", root: null, path: "/home/u/.easycode/mcp.json", exists: false },
      { scope: "app", root: null, path: "/ws/easycode.config.json", exists: true },
      { scope: "project", root: PROJECT, name: "proj", path: `${PROJECT}/easycode.config.json`, exists: false },
    ],
    servers: [],
    errors: [],
    ...overrides,
  };
}

function server(overrides: Partial<McpServerView> = {}): McpServerView {
  return {
    name: "fs",
    scope: "personal",
    overrides: [],
    enabled: true,
    config: { transport: "stdio", command: "npx", args: ["-y", "pkg"] },
    credential: null,
    ...overrides,
  };
}

/** The MCP tab of the extensions dialog, which is where the panel now lives. */
function renderPanel(onToast = vi.fn(), onChanged = vi.fn()) {
  render(
    <ExtensionsSettings
      root={PROJECT}
      sessionId={null}
      initialTab="mcp"
      projectName="proj"
      projectPath={PROJECT}
      onClose={vi.fn()}
      onChanged={onChanged}
      onToast={onToast}
    />,
  );
  return onToast;
}

beforeEach(() => {
  vi.clearAllMocks();
  m.fetchMcp.mockResolvedValue(info());
  m.fetchMcpStatus.mockResolvedValue({ started: false, servers: [] });
});

describe("扩展弹窗 · MCP 面板", () => {
  it("lists the effective servers with the scope that won the name", async () => {
    m.fetchMcp.mockResolvedValue(
      info({
        servers: [
          server({ scope: "project", overrides: ["personal"], enabled: false }),
          server({ name: "gh", credential: { id: "c1", kind: "env", names: ["token"], has_value: true } }),
        ],
      }),
    );

    renderPanel();

    expect(await screen.findByText("fs")).toBeTruthy();
    expect(screen.getByText("当前项目")).toBeTruthy();
    // The shadowed scope is named, so the override is explainable rather than
    // looking like the personal server simply vanished.
    expect(screen.getByText(/覆盖了个人的同名配置/)).toBeTruthy();
    expect(screen.getByText("已存凭据")).toBeTruthy();
  });

  it("shows a file nobody can parse instead of an empty list", async () => {
    m.fetchMcp.mockResolvedValue(info({ errors: ["无法读取 /home/u/.easycode/mcp.json: ..."] }));

    renderPanel();

    expect(await screen.findByText(/无法读取/)).toBeTruthy();
  });

  it("writes a project-scope entry that switches a personal server off there", async () => {
    const user = userEvent.setup();
    m.fetchMcp.mockResolvedValue(info({ servers: [server()] }));
    m.saveMcpServer.mockResolvedValue(info({ servers: [server()] }));
    const onChanged = vi.fn();
    renderPanel(vi.fn(), onChanged);
    await screen.findByText("fs");

    await user.click(screen.getByRole("button", { name: "本项目停用" }));

    await waitFor(() => expect(m.saveMcpServer).toHaveBeenCalled());
    // The menu must be able to re-read its sources after a save.
    await waitFor(() => expect(onChanged).toHaveBeenCalled());
    const [scope, root, name, config] = m.saveMcpServer.mock.calls[0];
    expect([scope, root, name]).toEqual(["project", PROJECT, "fs"]);
    expect(config.enabled).toBe(false);
    // The entry keeps the server's own settings; only the scope changes.
    expect(config.command).toBe("npx");
  });

  it("restores the lower scope by removing the project override", async () => {
    const user = userEvent.setup();
    m.fetchMcp.mockResolvedValue(
      info({ servers: [server({ scope: "project", overrides: ["personal"], enabled: false })] }),
    );
    m.removeMcpServer.mockResolvedValue(info({ servers: [server()] }));
    renderPanel();
    await screen.findByText("fs");

    await user.click(screen.getByRole("button", { name: "改用上一层" }));

    await waitFor(() => expect(m.removeMcpServer).toHaveBeenCalledWith("project", PROJECT, "fs"));
  });

  it("keeps a pasted secret out of the configuration it saves", async () => {
    const user = userEvent.setup();
    m.saveMcpServer.mockResolvedValue(info({ servers: [server()] }));
    m.saveMcpCredential.mockResolvedValue(info());
    renderPanel();

    await user.click(await screen.findByRole("button", { name: "添加服务" }));
    await user.type(screen.getByLabelText(/名称/), "gh");
    await user.type(screen.getByLabelText(/命令/), "npx");
    await user.type(screen.getByLabelText(/变量名/), "GITHUB_TOKEN");
    await user.type(screen.getByLabelText(/凭据值/), "ghp_secret");
    await user.click(screen.getByRole("button", { name: "保存" }));

    await waitFor(() => expect(m.saveMcpCredential).toHaveBeenCalled());
    const [scope, root, name, kind, values] = m.saveMcpCredential.mock.calls[0];
    expect([scope, root, name, kind]).toEqual(["project", PROJECT, "gh", "env"]);
    expect(values).toEqual({ token: "ghp_secret" });

    const saved = m.saveMcpServer.mock.calls[0][3];
    expect(saved.secret_env).toEqual({ GITHUB_TOKEN: "token" });
    // The configuration file names where the secret lives; it never holds it.
    expect(JSON.stringify(saved)).not.toContain("ghp_secret");
  });

  it("drops the fields the other transport owns", async () => {
    const user = userEvent.setup();
    m.saveMcpServer.mockResolvedValue(info());
    renderPanel();

    await user.click(await screen.findByRole("button", { name: "添加服务" }));
    await user.type(screen.getByLabelText(/名称/), "remote");
    await user.selectOptions(screen.getByLabelText(/传输/), "http");
    await user.type(screen.getByLabelText(/地址/), "https://example.com/mcp");
    await user.click(screen.getByRole("button", { name: "保存" }));

    await waitFor(() => expect(m.saveMcpServer).toHaveBeenCalled());
    const saved = m.saveMcpServer.mock.calls[0][3];
    expect(saved.url).toBe("https://example.com/mcp");
    expect(saved.transport).toBe("http");
    // A server with both a command and a url is a configuration nobody wrote.
    expect(saved.command).toBeUndefined();
    expect(saved.args).toBeUndefined();
  });

  it("reports a rejected save instead of pretending it worked", async () => {
    const user = userEvent.setup();
    m.saveMcpServer.mockRejectedValue(new Error("s: 需要 command（本地）或 url（远程）"));
    const onToast = renderPanel();

    await user.click(await screen.findByRole("button", { name: "添加服务" }));
    await user.type(screen.getByLabelText(/名称/), "broken");
    await user.click(screen.getByRole("button", { name: "保存" }));

    await waitFor(() => expect(onToast).toHaveBeenCalledWith("err", expect.stringContaining("需要 command")));
    // The dialog stays open on the form, so the text the user typed is still there.
    expect((screen.getByLabelText(/名称/) as HTMLInputElement).value).toBe("broken");
  });

  it("配置已保存但凭据失败时报告部分成功，并让菜单重新读取", async () => {
    const user = userEvent.setup();
    m.saveMcpServer.mockResolvedValue(info({ servers: [server()] }));
    m.saveMcpCredential.mockRejectedValue(new Error("凭据文件不可写"));
    const onToast = vi.fn();
    const onChanged = vi.fn();
    renderPanel(onToast, onChanged);

    await user.click(await screen.findByRole("button", { name: "添加服务" }));
    await user.type(screen.getByLabelText(/名称/), "gh");
    await user.type(screen.getByLabelText(/命令/), "npx");
    await user.type(screen.getByLabelText(/凭据值/), "ghp_secret");
    await user.click(screen.getByRole("button", { name: "保存" }));

    // The server entry is already in effect, so the failure must not read as
    // "nothing happened", and the menu still has to be re-read.
    await waitFor(() =>
      expect(onToast).toHaveBeenCalledWith("err", expect.stringContaining("凭据保存失败")),
    );
    expect(onChanged).toHaveBeenCalled();
  });
});
