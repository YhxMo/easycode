// Pure SSE → items state-transition reducer (B5).
//
// `applyChatEvent(prev, ev)` is a side-effect-free reducer: it takes the current
// items array and one SSE `ChatEvent`, and returns a *new* array describing the
// next state. It never mutates `prev` and never touches React state, so it can
// be unit-tested in isolation. Side effects that are NOT about items (setting the
// foreground session id, opening the approval overlay, refreshing the session
// list) stay in the caller (useChatStream / App) rather than leaking here.
import type { ChatEvent } from "./api";
import type { ApprovalState, Item } from "./types";

/** Resolve every pending approval to "expired" (turn ended / cancelled / error). */
function expirePending(items: Item[]): Item[] {
  return items.map((it) =>
    it.kind === "approval" && it.state === "pending" ? { ...it, state: "expired" } : it,
  );
}

/**
 * Whether the *current turn* (everything after the last user message) has already
 * started a tool. Faithful to the previous `ranTool` ref, which was true for the
 * lifetime of a stream once any `tool_start` had been seen: a tool_start always
 * pushes a `tool` item after the user message, and nothing removes items, so the
 * presence of a tool card in the trailing segment is equivalent.
 */
function currentTurnStartedTool(items: Item[]): boolean {
  for (let i = items.length - 1; i >= 0; i--) {
    if (items[i].kind === "user") return items.slice(i + 1).some((it) => it.kind === "tool");
  }
  return items.some((it) => it.kind === "tool");
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
  const last = withExpired[withExpired.length - 1];
  const ranTool = currentTurnStartedTool(withExpired);
  const noReply = ranTool && (last?.kind !== "assistant" || !(last.text ?? "").trim());
  if (!noReply) return withExpired;
  const msg =
    last?.kind === "tool" && !last.done
      ? "⚠ 回合已结束但工具未返回结果"
      : "✓ 已完成（模型未输出文字回复，工具可能已生效）";
  return [
    ...withExpired,
    {
      kind: noReply && last?.kind === "tool" && !last.done ? "error" : "notice",
      text: msg,
    },
  ];
}

/**
 * Stamp the trailing assistant blocks (everything after the last user message)
 * with the model name and wall-clock duration of the finished turn. Only
 * non-empty assistant items get the stamp so the empty placeholder block
 * (kept when the model replied purely via tools) stays clean.
 */
export function stampTurnMeta(items: Item[], model: string | undefined, durationMs: number): Item[] {
  let lastUser = -1;
  for (let i = items.length - 1; i >= 0; i--) {
    if (items[i].kind === "user") {
      lastUser = i;
      break;
    }
  }
  return items.map((it, idx) =>
    idx > lastUser && it.kind === "assistant" && (it.text ?? "").trim() ? { ...it, model, durationMs } : it,
  );
}

export function applyChatEvent(prev: Item[], ev: ChatEvent): Item[] {
  switch (ev.type) {
    case "session":
      // The session id is foreground state (set by the caller), not items.
      return prev;
    case "text":
      return appendOrExtendAssistant(prev, ev.content ?? "");
    case "cancelled":
      return [...expirePending(prev), { kind: "notice", text: "⏹ 已中断" }];
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
      return [...expirePending(prev), { kind: "error", text: ev.error ?? "error" }];
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
    default:
      return prev;
  }
}
