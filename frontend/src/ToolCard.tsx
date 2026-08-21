import { useState } from "react";
import type { ToolCall } from "./api";

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
  const args = Object.entries(tool.arguments)
    .map(([k, v]) => `${k}=${JSON.stringify(v)}`)
    .join(" ");
  return (
    <div className={`tool-card ${done ? "done" : "running"}`}>
      <div className="tool-head" onClick={() => setOpen(!open)}>
        <span className="tool-dot">⚙</span>
        <span className="tool-name">{tool.name}</span>
        <span className="tool-args">{args}</span>
        <span className="tool-state">{done ? "✓" : "…"}</span>
      </div>
      {open && result !== undefined && (
        <pre className="tool-result">{result}</pre>
      )}
    </div>
  );
}