// Chat stream lifecycle and request ownership.
import { useCallback, useRef, useState } from "react";
import type { ChatOptions } from "./api";
import { cancelSessionChat, streamChat } from "./api";
import { applyChatEvent, stampTurnMeta } from "./chatStream";
import { isAbortError } from "./lib/history";
import type { Item } from "./types";

export interface UseChatStreamParams {
  input: string;
  currentId: string | null;
  chosenRoot: string | null;
  chosenSecondary: string[];
  chosenPermission: string;
  currentPermission: string;
  /** Model name at send time, stamped onto the turn's assistant messages for the reply meta row. */
  currentModelName: string;
  refreshSessions: () => void;
  setInput: (v: string) => void;
  setCurrentId: (v: string | null) => void;
  /** Called when an approval_required event arrives (opens the approval sheet). */
  onApprovalRequired: () => void;
}

export function useChatStream(params: UseChatStreamParams) {
  // Always read the freshest external inputs through `latest` so `send`/`stop`
  // can be stable callbacks without ever capturing a stale closure.
  const latest = useRef(params);
  latest.current = params;

  const [busy, setBusy] = useState(false);
  // send() reads `busy` synchronously for its re-entrancy guard, but a stable
  // callback cannot see the state directly, so mirror it in a ref.
  const busyRef = useRef(busy);
  busyRef.current = busy;

  const [items, setItems] = useState<Item[]>([]);

  // Request generation + per-session stream
  // ownership. currentRequestRef holds the token of the *active* request; a
  // stale/aborted request is one whose token no longer matches. openSeqRef
  // guards openSession against out-of-order fetchSession responses.
  const currentRequestRef = useRef<number | null>(null);
  const reqSeqRef = useRef(0);
  const currentStreamSessionRef = useRef<string | null>(null);
  const sendAbortRef = useRef<AbortController | null>(null);
  const openSeqRef = useRef(0);

  const send = useCallback(async () => {
    const c = latest.current;
    const text = c.input.trim();
    if (!text || busyRef.current) return;
    c.setInput("");
    busyRef.current = true;
    setBusy(true);
    const sessionId = c.currentId;
    // each request owns a unique token. A stale/aborted request has a
    // token that no longer matches currentRequestRef and drops all its events.
    const token = ++reqSeqRef.current;
    currentRequestRef.current = token;
    currentStreamSessionRef.current = sessionId;
    const turnStartedAt = Date.now();
    const modelAtSend = c.currentModelName;
    const controller = new AbortController();
    sendAbortRef.current = controller;
    const opts: ChatOptions = {};
    if (c.currentId === null) {
      if (c.chosenRoot) opts.root = c.chosenRoot;
      if (c.chosenSecondary.length) opts.secondary_roots = c.chosenSecondary;
    }
    opts.permission_mode = c.currentId === null ? c.chosenPermission : c.currentPermission;
    opts.signal = controller.signal;
    const patches: Item[] = [
      { kind: "user", text },
      { kind: "assistant", text: "" },
    ];
    setItems((prev) => [...prev, ...patches]);
    try {
      await streamChat(sessionId, text, (ev) => {
        // Only the current request may touch the view.
        if (currentRequestRef.current !== token) return;
        if (ev.type === "session") {
          if (ev.session_id) {
            c.setCurrentId(ev.session_id);
            currentStreamSessionRef.current = ev.session_id;
          }
          c.refreshSessions();
          return;
        }
        // Opening the approval overlay is foreground state, not items, so it is
        // handled here; the approval item itself is produced by the reducer.
        if (ev.type === "approval_required") c.onApprovalRequired();
        setItems((prev) => applyChatEvent(prev, ev));
      }, opts);
    } catch (err) {
      // A stale/aborted request must not add an error row to a different
      // session's view. A deliberate abort is not an error either.
      if (currentRequestRef.current !== token) return;
      if (isAbortError(err)) {
        setItems((prev) => applyChatEvent(prev, { type: "cancelled" }));
        return;
      }
      setItems((prev) => [
        ...prev,
        { kind: "error", text: err instanceof Error ? err.message : String(err) },
      ]);
    } finally {
      busyRef.current = false;
      setBusy(false);
      if (currentRequestRef.current === token) {
        setItems((prev) =>
          stampTurnMeta(
            prev.map((it) =>
              it.kind === "approval" && it.state === "pending" ? { ...it, state: "expired" } : it,
            ),
            modelAtSend,
            Date.now() - turnStartedAt,
          ),
        );
        c.refreshSessions();
      }
      sendAbortRef.current = null;
    }
  }, []);

  const stop = useCallback(() => {
    // Abort the local reader + tell the backend to cancel; the send() finally
    // still runs and resets busy since we keep the request token.
    sendAbortRef.current?.abort();
    if (latest.current.currentId) {
      cancelSessionChat(latest.current.currentId).catch(() => {});
    }
  }, []);

  return {
    send,
    stop,
    busy,
    items,
    setItems,
    currentRequestRef,
    currentStreamSessionRef,
    sendAbortRef,
    openSeqRef,
  };
}
