// Convert persisted history into chat items.
import type { ApprovalRecord, ArtifactRecord, TurnFailure } from "../api";
import type { Item, ToolItem } from "../types";

export function normalizeToolArgs(value: unknown): Record<string, unknown> {
  if (value && typeof value === "object" && !Array.isArray(value)) {
    return value as Record<string, unknown>;
  }
  if (typeof value === "string") {
    try {
      const parsed = JSON.parse(value);
      if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
        return parsed as Record<string, unknown>;
      }
    } catch {
      return { input: value };
    }
  }
  return {};
}

export function currentTimeLabel(): string {
  return new Intl.DateTimeFormat("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date());
}

export function formatClock(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return currentTimeLabel();
  return new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit" }).format(d);
}

/** "4 分 19 秒" / "19 秒" — wall-clock label for the assistant reply meta row. */
export function formatDuration(ms: number): string {
  const total = Math.max(0, Math.round(ms / 1000));
  const minutes = Math.floor(total / 60);
  const seconds = total % 60;
  return minutes > 0 ? `${minutes} 分 ${seconds} 秒` : `${seconds} 秒`;
}

export function isAbortError(err: unknown): boolean {
  return err instanceof DOMException ? err.name === "AbortError" : (err as Error)?.name === "AbortError";
}

// 后端历史消息（OpenAI 风格 dict）的宽松形状；字段按 historyToItems 的实际读取面声明。
export interface HistoryMessage {
  role: string;
  content?: unknown;
  tool_call_id?: string;
  tool_calls?: { id?: string; function?: { name?: string; arguments?: string } }[];
  /** The turn this message belongs to, as the server records it. */
  turn_id?: string;
  [key: string]: unknown;
}

/**
 * A conversation as the server holds it, converted into the items the view
 * renders.
 *
 * Failures are attached through the turn they ended rather than through a
 * timestamp, so the same input produces the same transcript whatever order the
 * server happens to list them in.
 */
export function historyToItems(
  messages: HistoryMessage[],
  approvals: ApprovalRecord[] = [],
  failures: TurnFailure[] = [],
): Item[] {
  const items: Item[] = [];
  const toolResults = new Map<string, string>();
  for (const m of messages) {
    if (m.role === "tool" && m.tool_call_id) toolResults.set(String(m.tool_call_id), String(m.content ?? ""));
  }
  const approvalByCall = new Map<string, ApprovalRecord>();
  for (const a of approvals) approvalByCall.set(String(a.tool_call_id), a);
  const failureByTurn = new Map<string, TurnFailure>();
  for (const f of failures) if (f.turn_id) failureByTurn.set(String(f.turn_id), f);
  // The error ended its turn, so it belongs at the end of that turn — not
  // beside the prompt that started it.
  let pending: TurnFailure | null = null;
  const flushFailure = () => {
    if (pending) items.push({ kind: "error", text: pending.message, code: pending.code });
    pending = null;
  };
  for (const m of messages) {
    if (m.role === "user") {
      flushFailure();
      const turnId = m.turn_id ? String(m.turn_id) : undefined;
      pending = (turnId && failureByTurn.get(turnId)) || null;
      items.push({ kind: "user", text: String(m.content ?? ""), turnId, commandId: null });
    } else if (m.role === "assistant") {
      const turnId = m.turn_id ? String(m.turn_id) : undefined;
      if (m.content) items.push({ kind: "assistant", text: String(m.content), turnId });
      if (m.tool_calls?.length) {
        for (const tc of m.tool_calls) {
          if (!tc.function) continue;
          const id = String(tc.id ?? "");
          const approval = approvalByCall.get(id);
          if (approval) {
            items.push({
              kind: "approval",
              id: `hist-${id}`,
              toolCallId: id,
              name: approval.name,
              args: approval.args ?? {},
              reason: approval.reason,
              scope: approval.scope,
              state: approval.decision,
            });
          }
          items.push({
            kind: "tool",
            id,
            name: tc.function.name ?? "",
            args: normalizeToolArgs(tc.function.arguments),
            result: toolResults.get(id),
            done: true,
          });
        }
      }
    }
  }
  flushFailure();
  return items;
}

/**
 * The session's file-tool records as tool items.
 *
 * They are the same shape a live turn produces, so the pane reads both through
 * one path; unlike a history message they survive a context compaction, which
 * is what keeps earlier turns' cards and diffs on screen.
 */
export function artifactsToItems(records: ArtifactRecord[]): ToolItem[] {
  return records.map((record) => ({
    kind: "tool" as const,
    id: record.id,
    name: record.name,
    args: normalizeToolArgs(record.args),
    result: record.result,
    done: true,
  }));
}
