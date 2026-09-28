import type { McpInfo, McpScope } from "../../api";
import type { ServerForm } from "./mcpForm";

/**
 * The fields of one server's configuration.
 *
 * Purely a view over the form the panel holds: it is rendered only while a form
 * is open, and its save button lives in the dialog's actions row with the panel
 * that owns the request.
 */
export function McpServerForm({
  form,
  info,
  onChange,
}: {
  form: ServerForm;
  /** Scope paths and the project's display name, once the config has loaded. */
  info: McpInfo | null;
  onChange: (next: ServerForm) => void;
}) {
  const scopesFor = (scope: McpScope) => info?.scopes.find((s) => s.scope === scope);
  const projectLabel = info?.projects.find((p) => p.root === info.root)?.name ?? null;

  const field = (
    label: string,
    key: keyof ServerForm,
    props: { placeholder?: string; hint?: string; multiline?: boolean } = {},
  ) => (
    <label className="modal-field">
      <span>{label}</span>
      {props.multiline ? (
        <textarea
          rows={3}
          value={String(form[key] ?? "")}
          placeholder={props.placeholder}
          onChange={(e) => onChange({ ...form, [key]: e.target.value })}
        />
      ) : (
        <input
          type="text"
          value={String(form[key] ?? "")}
          placeholder={props.placeholder}
          onChange={(e) => onChange({ ...form, [key]: e.target.value })}
        />
      )}
      {props.hint ? <small>{props.hint}</small> : null}
    </label>
  );

  return (
    <>
      <div className="modal-section">
        <div className="modal-section-title">基本信息</div>
        <label className="modal-field">
          <span>作用域</span>
          <span className="modal-select-wrap">
            <select
              value={form.scope}
              onChange={(e) => onChange({ ...form, scope: e.target.value as McpScope })}
            >
              {(info?.scopes ?? [{ scope: "project" as McpScope, path: "" }]).map((row) => (
                <option key={row.scope} value={row.scope}>
                  {row.scope === "project"
                    ? `当前项目${projectLabel ? `（${projectLabel}）` : ""}`
                    : row.scope === "personal"
                      ? "个人（所有项目）"
                      : "应用启动配置"}
                </option>
              ))}
            </select>
          </span>
          <small>{scopesFor(form.scope)?.path ?? ""}</small>
        </label>
        {field("名称", "name", { placeholder: "filesystem" })}
        <label className="modal-field">
          <span>传输</span>
          <span className="modal-select-wrap">
            <select
              value={form.transport}
              onChange={(e) => onChange({ ...form, transport: e.target.value as "stdio" | "http" })}
            >
              <option value="stdio">本地命令（stdio）</option>
              <option value="http">远程地址（Streamable HTTP）</option>
            </select>
          </span>
        </label>
      </div>

      <div className="modal-section">
        <div className="modal-section-title">
          {form.transport === "stdio" ? "启动方式" : "连接方式"}
        </div>
        {form.transport === "stdio" ? (
          <>
            {field("命令", "command", { placeholder: "npx" })}
            {field("参数", "args", { multiline: true, hint: "每行一个" })}
            {field("工作目录", "cwd", {
              placeholder: "留空即当前目录",
              hint: "必须在工作区内",
            })}
            {field("环境变量", "env", { multiline: true, hint: "每行 KEY=VALUE" })}
            <label className="modal-check">
              <input
                type="checkbox"
                checked={form.network}
                onChange={(e) => onChange({ ...form, network: e.target.checked })}
              />
              <span>允许访问网络（首次下载包时需要）</span>
            </label>
          </>
        ) : (
          <>
            {field("地址", "url", { placeholder: "https://example.com/mcp" })}
            {field("请求头", "headers", { multiline: true, hint: "每行 KEY: VALUE" })}
          </>
        )}
      </div>

      <div className="modal-section">
        <div className="modal-section-title">工具与超时</div>
        {field("启动超时（秒）", "startup", { placeholder: "10" })}
        {field("工具超时（秒）", "tool", { placeholder: "60" })}
        {field("只允许这些工具", "enabledTools", { placeholder: "留空即全部，* 表示全部" })}
        {field("禁用这些工具", "disabledTools", { placeholder: "逗号或空格分隔" })}
      </div>

      <div className="modal-section">
        <div className="modal-section-title">凭据</div>
        <label className="modal-field">
          <span>凭据类型</span>
          <span className="modal-select-wrap">
            <select
              value={form.secretKind}
              onChange={(e) =>
                onChange({ ...form, secretKind: e.target.value as ServerForm["secretKind"] })
              }
            >
              <option value="env">环境变量</option>
              <option value="header">请求头</option>
              <option value="bearer">Bearer Token</option>
            </select>
          </span>
        </label>
        {form.secretKind !== "bearer"
          ? field(form.secretKind === "env" ? "变量名" : "请求头名", "secretName", {
              placeholder: form.secretKind === "env" ? "GITHUB_TOKEN" : "Authorization",
            })
          : null}
        {field("凭据值", "secretValue", {
          placeholder: form.secretKind === "bearer" ? "粘贴 Token" : "粘贴密钥",
          hint: "保存后不再回显；留空表示不修改",
        })}
      </div>
    </>
  );
}
