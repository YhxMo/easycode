// Chat stream lifecycle and request ownership.
import { useCallback, useRef, useState } from "react";
import type { ChatOptions } from "./api";
import { cancelSessionChat, streamChat } from "./api";
import { applyChatEvent, expirePending, stampTurnMeta } from "./chatStream";
import { isAbortError } from "./lib/history";
import type { Item } from "./types";

export interface UseChatStreamParams {
  input: string;
  currentId: string | null;
  chosenRoot: string | null;
  /** Secondary roots of the session being composed (draft) or viewed. */
  secondary: string[];
  /** Permission mode of the session being composed (draft) or viewed. */
  permission: string;
  /** Model name at send time, stamped onto the turn's assistant messages for the reply meta row. */
  currentModelName: string;
  /** Foreground session is loading or failed to load: composing is not allowed. */
  sendBlocked: boolean;
  refreshSessions: () => void;
  setInput: (v: string) => void;
  setCurrentId: (v: string | null) => void;
  /** Called when an approval_required event arrives (opens the approval sheet). */
  onApprovalRequired: () => void;
}

/** One in-flight send: its abort handle plus the session it created. */
interface ActiveRequest {
  controller: AbortController;
  sessionId: string | null;
}

export function useChatStream(params: UseChatStreamParams) {
  // Always read the freshest external inputs through `latest` so `send`/`stop`
  // can be stable callbacks without ever capturing a stale closure.
  const latest = useRef(params);
  latest.current = params;

  const [busy, setBusy] = useState(false);

  const [items, setItems] = useState<Item[]>([]);

  // Stream ownership: the active request object is the single identity. A
  // stale/aborted request is one that no longer matches activeRef.
  const activeRef = useRef<ActiveRequest | null>(null);

  /** Abandon the in-flight request and release the foreground immediately. */
  const detach = useCallback(() => {
    activeRef.current?.controller.abort();
    activeRef.current = null;
    setBusy(false);
  }, []);

  const send = useCallback(async () => {
    const c = latest.current;
    const text = c.input.trim();
    // activeRef is claimed synchronously before the first await, so this also
    // blocks a second send from the same event-loop tick.
    if (!text || activeRef.current || c.sendBlocked) return;
    c.setInput("");
    setBusy(true);
    const sessionId = c.currentId;
    const turnStartedAt = Date.now();
    const modelAtSend = c.currentModelName;
    const request: ActiveRequest = { controller: new AbortController(), sessionId };
    activeRef.current = request;
    const opts: ChatOptions = {};
    if (sessionId === null) {
      if (c.chosenRoot) opts.root = c.chosenRoot;
      // The draft's list is always explicit (empty included): only a request
      // that omits the field inherits the project's secondary binding.
      opts.secondary_roots = c.secondary;
    }
    opts.permission_mode = c.permission;
    opts.signal = request.controller.signal;
    const patches: Item[] = [
      // Stamp the time at send: rendering must not fall back to the clock.
      { kind: "user", text, time: new Date().toISOString() },
      { kind: "assistant", text: "" },
    ];
    setItems((prev) => [...prev, ...patches]);
    let terminalError = false;
    try {
      await streamChat(sessionId, text, (ev) => {
        // Only the current request may touch the view.
        if (activeRef.current !== request) return;
        if (ev.type === "session") {
          if (ev.session_id) {
            // Track the created session on the request itself so stop() targets
            // the request's own session, not whatever view is foreground.
            request.sessionId = ev.session_id;
            c.setCurrentId(ev.session_id);
          }
          c.refreshSessions();
          return;
        }
        // Opening the approval overlay is foreground state, not items, so it is
        // handled here; the approval item itself is produced by the reducer.
        if (ev.type === "approval_required") c.onApprovalRequired();
        if (ev.type === "error") terminalError = true;
        setItems((prev) => applyChatEvent(prev, ev));
      }, opts);
    } catch (err) {
      // A stale/aborted request must not add an error row to a different
      // session's view. A deliberate abort is not an error either.
      if (activeRef.current !== request) return;
      if (isAbortError(err)) {
        setItems((prev) => applyChatEvent(prev, { type: "cancelled" }));
        return;
      }
      // The backend already reported a terminal error for this turn (the
      // stream then ended): do not append a second error row.
      if (!terminalError) {
        // Route through the reducer so a network failure also interrupts any
        // still-running tool card instead of leaving it spinning forever.
        setItems((prev) =>
          applyChatEvent(prev, {
            type: "error",
            error: err instanceof Error ? err.message : String(err),
          }),
        );
      }
    } finally {
      // Only the request that still owns the foreground may tear it down: a
      // detached/stale request must not clear a newer request's busy state or
      // take over its items. The session list refresh is side-effect-only.
      if (activeRef.current === request) {
        activeRef.current = null;
        setBusy(false);
        setItems((prev) => stampTurnMeta(expirePending(prev), modelAtSend, Date.now() - turnStartedAt));
      }
      c.refreshSessions();
    }
  }, []);

  const stop = useCallback(() => {
    // Abort the local reader + tell the backend to cancel; the send() finally
    // still runs and resets busy while this request stays active.
    const request = activeRef.current;
    if (!request) return;
    request.controller.abort();
    if (request.sessionId) cancelSessionChat(request.sessionId).catch(() => {});
  }, []);

  return { send, stop, busy, items, setItems, detach };
}
