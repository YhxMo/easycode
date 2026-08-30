// Custom hook that encapsulates the send orchestration for the chat stream
// (B5). It owns the request-ownership refs (currentRequestRef/reqSeqRef/
// currentStreamSessionRef/sendAbortRef/openSeqRef) plus the `busy` and `items`
// state, and exposes a dependency-injected `send`/`stop`. Every external input
// (current session id, chosen permission, the api surface, etc.) is passed in as
// a parameter rather than reaching into App's closure, so the hook is decoupled
// from App's implementation.
//
// The item-level SSE transitions are delegated to the pure `applyChatEvent`
// reducer; this hook only adds the side effects that are NOT about items
// (session id binding, opening the approval overlay, session-list refresh and
// the new-session claim in the finally block).
import { useCallback, useRef, useState } from "react";
import type { ChatOptions } from "./api";
import { cancelSessionChat, fetchSessions, streamChat } from "./api";
import { applyChatEvent, stampTurnMeta } from "./chatStream";
import { isAbortError } from "./lib/history";
import type { Item, RollbackInfo } from "./types";

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
  setRollbackInfo: (v: RollbackInfo | null) => void;
  setInput: (v: string) => void;
  setCurrentId: (v: string | null) => void;
  setCurrentPermission: (v: string) => void;
  /** Called when an approval_required event arrives (opens the approval sheet). */
  onApprovalRequired: () => void;
  streamChat: typeof streamChat;
  cancelSessionChat: typeof cancelSessionChat;
  fetchSessions: typeof fetchSessions;
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
    c.setRollbackInfo(null);
    setBusy(true);
    const sessionId = c.currentId;
    // each request owns a unique token. A stale/aborted request has a
    // token that no longer matches currentRequestRef and drops all its events.
    const token = ++reqSeqRef.current;
    currentRequestRef.current = token;
    currentStreamSessionRef.current = sessionId;
    // Capture the open-generation at send time so the finally block can
    // tell whether the user navigated (openSession / 新会话) while this stream
    // was still in flight. Only the still-current open generation may claim the
    // freshly-created session id; otherwise we refresh the list and leave the
    // foreground session ownership untouched.
    const openSeqAtSend = openSeqRef.current;
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
      await c.streamChat(sessionId, text, (ev) => {
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
      if (isAbortError(err)) return;
      setItems((prev) => [
        ...prev,
        { kind: "error", text: err instanceof Error ? err.message : String(err) },
      ]);
    } finally {
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
        if (sessionId === null && openSeqRef.current === openSeqAtSend) {
          // Pick up the newly-created session, but only when the user is still on
          // the same new-session chain that started this request. If
          // they navigated away while the stream ran (openSession / 新会话), a
          // later open generation advanced openSeqRef — do not steal the
          // foreground session ownership; the list is already refreshed above.
          const list = await c.fetchSessions().catch(() => []);
          if (list.length) {
            c.setCurrentId(list[0].id);
            currentStreamSessionRef.current = list[0].id;
            c.setCurrentPermission(list[0].permission_mode ?? c.chosenPermission);
          }
        }
      }
      sendAbortRef.current = null;
    }
  }, []);

  const stop = useCallback(() => {
    // Abort the local reader + tell the backend to cancel; the send() finally
    // still runs and resets busy since we keep the request token.
    sendAbortRef.current?.abort();
    if (latest.current.currentId) {
      latest.current.cancelSessionChat(latest.current.currentId).catch(() => {});
    }
  }, []);

  return {
    send,
    stop,
    busy,
    items,
    setBusy,
    setItems,
    currentRequestRef,
    reqSeqRef,
    currentStreamSessionRef,
    sendAbortRef,
    openSeqRef,
  };
}
