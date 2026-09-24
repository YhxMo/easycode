// Pure SSE events to chat items reducer.
import type { ChatEvent } from "./api";
import type { ApprovalState, Item } from "./types";

/** Resolve every pending approval to "expired" (turn ended / cancelled / error). */
export function expirePending(items: Item[]): Item[] {
  return items.map((it) =>
    it.kind === "approval" && it.state === "pending" ? { ...it, state: "expired" } : it,
  );
}

function interruptPending(items: Item[]): Item[] {
  return expirePending(items).map((item) =>
    item.kind === "tool" && !item.done
      ? { ...item, done: true, result: JSON.stringify({
          status: "error",
          message: "执行已中断，未收到最终结果；已开始的操作可能继续完成。",
        }) }
      : item,
  );
}

/** Index of the last user item (-1 when the list has none). */
function currentTurnStart(items: Item[]): number {
  for (let i = items.length - 1; i >= 0; i--) {
    if (items[i].kind === "user") return i;
  }
  return -1;
}

/** Items belonging to the current turn: everything after the last user item. */
export function currentTurn(items: Item[]): Item[] {
  return items.slice(currentTurnStart(items) + 1);
}

function appendOrExtendAssistant(prev: Item[], content: string): Item[] {
  const last = prev[prev.length - 1];
  if (last?.kind === "assistant") {
    const next = [...prev];
    // Copy the last item so the caller's array is never mutated.
    next[next.length - 1] = { ...last, text: last.text + content };
    return next;
  }
  // Text after a tool call starts a new assistant block (the old handler dropped
  // it, hiding the model's final reply).
  return [...prev, { kind: "assistant", text: content }];
}

function fillToolResult(prev: Item[], ev: Extract<ChatEvent, { type: "tool_result" }>): Item[] {
  const next = [...prev];
  for (let i = next.length - 1; i >= 0; i--) {
    const item = next[i];
    if (item.kind === "tool" && item.id === ev.tool_call?.id) {
      next[i] = { ...item, result: ev.result, done: true };
      return next;
    }
  }
  return next;
}

function finishDone(prev: Item[]): Item[] {
  const withExpired = expirePending(prev);
  // Judge the whole current turn, not just its last item: a review card or a
  // finished tool may follow the final assistant text, and an error must never
  // be contradicted by a "completed" notice.
  const turn = currentTurn(withExpired);
  const reported = turn.some(
    (it) => it.kind === "error" || (it.kind === "assistant" && it.text.trim()),
  );
  if (reported || !turn.some((it) => it.kind === "tool")) return withExpired;
  const last = turn[turn.length - 1];
  if (last?.kind === "tool" && !last.done) {
    return [...withExpired, { kind: "error", text: "⚠ 回合已结束但工具未返回结果" }];
  }
  return [
    ...withExpired,
    { kind: "notice", text: "✓ 已完成（模型未输出文字回复，工具可能已生效）" },
  ];
}

/**
 * Stamp the trailing assistant blocks (everything after the last user message)
 * with the model name and wall-clock duration of the finished turn. Only
 * non-empty assistant items get the stamp so the empty placeholder block
 * (kept when the model replied purely via tools) stays clean.
 */
export function stampTurnMeta(items: Item[], model: string | undefined, durationMs: number): Item[] {
  const lastUser = currentTurnStart(items);
  return items.map((it, idx) =>
    idx > lastUser && it.kind === "assistant" && it.text.trim() ? { ...it, model, durationMs } : it,
  );
}

export function applyChatEvent(prev: Item[], ev: ChatEvent): Item[] {
  switch (ev.type) {
    case "text":
      return appendOrExtendAssistant(prev, ev.content ?? "");
    case "cancelled":
      return [...interruptPending(prev), { kind: "notice", text: "已停止本轮，已执行的操作保留。" }];
    case "tool_start":
      return [
        ...prev,
        {
          kind: "tool",
          id: ev.tool_call.id,
          name: ev.tool_call.name,
          args: ev.tool_call.arguments,
          done: false,
        },
      ];
    case "tool_result":
      return fillToolResult(prev, ev);
    case "done":
      return finishDone(prev);
    case "error":
      return [...interruptPending(prev), { kind: "error", text: ev.error ?? "error" }];
    case "approval_required":
      return [
        ...prev,
        {
          kind: "approval",
          id: ev.approval_id,
          toolCallId: ev.tool_call.id,
          name: ev.tool_call.name,
          args: ev.tool_call.arguments,
          reason: ev.reason,
          scope: ev.scope,
          state: "pending" as ApprovalState,
        },
      ];
    case "review":
      return [...prev, { kind: "review", text: ev.content ?? "" }];
    case "todo": {
      // The task list is session state that the model rewrites wholesale, so it
      // replaces the previous list in place instead of stacking up in history.
      const todos = ev.todos ?? [];
      let at = -1;
      for (let i = prev.length - 1; i >= 0; i -= 1) {
        if (prev[i].kind === "todo") {
          at = i;
          break;
        }
      }
      if (at === -1) return [...prev, { kind: "todo", todos }];
      return prev.map((it, i) => (i === at ? { kind: "todo" as const, todos } : it));
    }
    default:
      return prev;
  }
}
