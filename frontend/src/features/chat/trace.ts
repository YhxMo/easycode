// Aggregate a turn's tool calls into an expandable trace.
import type { Item } from "../../types";
import { parseResult, resultStatus } from "./toolResult";

export type StepStatus = "running" | "done" | "error";

export interface TraceStep {
  id: string;
  name: string;
  /** Localised verb shown next to the status icon. */
  label: string;
  /** Path, pattern or command the step acted on. */
  detail: string;
  status: StepStatus;
  /** Raw tool result, exactly as the backend sent it. */
  raw?: string;
}

export interface Trace {
  steps: TraceStep[];
  running: boolean;
  /** Files read or listed. */
  reads: number;
  /** Content searches. */
  searches: number;
}

const LABELS: Record<string, string> = {
  execute_shell: "Shell",
  edit_file: "补丁",
  write_file: "写入",
  read_file: "读取",
  grep: "搜索",
  glob: "查找",
  task: "子任务",
  parallel_tasks: "并行任务",
  use_skill: "技能",
  update_todos: "清单",
};

function detailOf(args: Record<string, unknown>): string {
  for (const key of ["path", "pattern", "command", "url", "name"]) {
    const value = args[key];
    if (typeof value === "string" && value) return value;
  }
  return "";
}

function statusOf(item: Extract<Item, { kind: "tool" }>): StepStatus {
  if (!item.done) return "running";
  if (!item.result) return "done";
  return resultStatus(parseResult(item.result)) === "error" ? "error" : "done";
}

/**
 * The result is kept exactly as it arrived: formatting every payload while
 * aggregating a trace was work done for steps the reader never expands. The
 * one place that needs pretty JSON is the expanded detail itself.
 */
export function toolStep(item: Extract<Item, { kind: "tool" }>): TraceStep {
  return {
    id: item.id,
    name: item.name,
    label: LABELS[item.name] ?? item.name,
    detail: detailOf(item.args),
    status: statusOf(item),
    raw: item.result,
  };
}

/** The trace of one turn (items after the last user message). */
export function traceOf(turn: Item[]): Trace {
  const steps: TraceStep[] = [];
  for (const item of turn) {
    if (item.kind === "tool") steps.push(toolStep(item));
  }
  return {
    steps,
    running: steps.some((s) => s.status === "running"),
    reads: steps.filter((s) => s.name === "read_file" || s.name === "glob").length,
    searches: steps.filter((s) => s.name === "grep").length,
  };
}
