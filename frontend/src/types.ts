// Shared chat UI types.
export type ApprovalState = "pending" | "approved" | "denied" | "expired";

export type Item =
  | { kind: "user"; text: string; time?: string }
  | {
      kind: "assistant";
      text: string;
      /** Model name captured at send time (streamed turns only; history has no per-message model). */
      model?: string;
      /** Wall-clock duration of the turn that produced this reply, stamped when the stream finishes. */
      durationMs?: number;
    }
  | {
      kind: "tool";
      id: string;
      name: string;
      args: Record<string, unknown>;
      result?: string;
      done: boolean;
    }
  | {
      kind: "approval";
      id: string;
      toolCallId: string;
      name: string;
      args: Record<string, unknown>;
      reason?: string;
      scope?: string;
      state: ApprovalState;
    }
  | { kind: "review"; text: string }
  | { kind: "notice"; text: string }
  | { kind: "error"; text: string };
