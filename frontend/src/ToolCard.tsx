import { useMemo, useState } from "react";
import type { ToolCall } from "./api";

type ToolResult = {
  status?: string;
  path?: string;
  diff?: string;
  message?: string;
  stdout?: string;
  stderr?: string;
  [key: string]: unknown;
};

function parseResult(result?: string): ToolResult | null {
  if (!result) return null;
  try {
    const value = JSON.parse(result) as unknown;
    return value && typeof value === "object" && !Array.isArray(value)
      ? (value as ToolResult)
      : null;
  } catch {
    return null;
  }
}

function toolLabel(name: string): string {
  if (name === "execute_shell") return "Shell";
  if (name === "edit_file" || name === "write_file") return "补丁";
  if (name === "read_file") return "读取";
  if (name === "list_dir") return "浏览";
  return name;
}

function compactValue(value: unknown): string {
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

function diffStats(diff?: string): { added: number; removed: number } | null {
  if (!diff) return null;
  let added = 0;
  let removed = 0;
  for (const line of diff.split("\n")) {
    if (line.startsWith("+++") || line.startsWith("---")) continue;
    if (line.startsWith("+")) added += 1;
    if (line.startsWith("-")) removed += 1;
  }
  return { added, removed };
}

export function ToolCard({
  tool,
  result,
  done,
}: {
  tool: ToolCall;
  result?: string;
  done: boolean;
}) {
  const [open, setOpen] = useState(false);
  const parsed = useMemo(() => parseResult(result), [result]);
  const command = typeof tool.arguments.command === "string" ? tool.arguments.command : null;
  const path = typeof tool.arguments.path === "string" ? tool.arguments.path : parsed?.path;
  const stats = diffStats(parsed?.diff);
  const args = Object.entries(tool.arguments)
    .filter(([key]) => key !== "old_string" && key !== "new_string" && key !== "command")
    .map(([key, value]) => `${key}=${compactValue(value)}`)
    .join(" ");

  const detail = command ?? path ?? args;
  const failed =
    parsed?.status === "error" ||
    parsed?.rejected === true ||
    (typeof parsed?.exit_code === "number" && parsed.exit_code !== 0);
  const status = failed ? "error" : done ? "done" : "running";
  const stateLabel = status === "error" ? "失败" : done ? "完成" : "执行中";
  // stderr is only a concern when the command failed; a nonzero exit code or
  // an explicit "error"/rejected status marks failure, not mere stderr output
  // (e.g. curl -I writes its progress meter to stderr while succeeding).
  const showStderr = !failed && !!parsed?.stderr && parsed?.exit_code === 0;
  return (
    <div className={`tool-card ${status} ${tool.name === "edit_file" || tool.name === "write_file" ? "patch-tool" : ""}`}>
      <button className="tool-head" type="button" onClick={() => setOpen(!open)} aria-expanded={open}>
        <span className="tool-chevron" aria-hidden="true">{open ? "⌄" : "›"}</span>
        <span className="tool-dot" aria-hidden="true">{status === "error" ? "×" : done ? "✓" : "·"}</span>
        <span className="tool-name">{toolLabel(tool.name)}</span>
        {detail && <span className="tool-args" title={detail}>{detail}</span>}
        {stats && <span className="tool-inline-stats"><b>+{stats.added}</b> <i>-{stats.removed}</i></span>}
        <span className="tool-state">{stateLabel}</span>
      </button>
      {open && (
        <div className="tool-body">
          {command && <pre className="tool-command"><span>$</span> {command}</pre>}
          {path && (
            <div className="tool-file-line">
              <span>{path}</span>
              {stats && <span className="tool-diff-stats"><b>+{stats.added}</b> <i>-{stats.removed}</i></span>}
            </div>
          )}
          {parsed?.message && <div className="tool-message">{parsed.message}</div>}
          {result !== undefined && (
            <pre className="tool-result">{parsed?.diff ?? parsed?.stdout ?? parsed?.stderr ?? result}</pre>
          )}
          {showStderr && <pre className="tool-stderr">stderr: {parsed?.stderr}</pre>}
        </div>
      )}
    </div>
  );
}
