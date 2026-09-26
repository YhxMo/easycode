// Chat stream lifecycle and request ownership.
import { useCallback, useMemo, useRef, useState } from "react";
import type { ChatOptions } from "./api";
import { cancelSessionChat, streamChat } from "./api";
import { applyChatEvent, currentTurn, expirePending, stampTurnMeta } from "./chatStream";
import { isAbortError } from "./lib/history";
import type { Item } from "./types";

/** Registry key of the not-yet-created session the composer is composing into. */
export const DRAFT_KEY = "\u0000draft";

export interface UseChatStreamParams {
  input: string;
  currentId: string | null;
  chosenRoot: string | null;
  /** Secondary roots of the session being composed (draft) or viewed. */
  secondary: string[];
  /** Permission mode of the session being composed (draft) or viewed. */
  permission: string;
  /** Id of the `/` menu entry the composer is holding, if any. */
  commandId: string | null;
  /** Model name at send time, stamped onto the turn's assistant messages for the reply meta row. */
  currentModelName: string;
  /** Foreground session is loading or failed to load: composing is not allowed. */
  sendBlocked: boolean;
  /**
   * View version of the foreground, read at event time. A stream that named its
   * session while the user was still looking at it takes the foreground; one
   * that lands after the user navigated away only fills its own slot.
   */
  getViewToken: () => number;
  refreshSessions: () => void;
  /** Delete exactly the draft a send consumed (never the foreground's). */
  clearDraft: (key: string) => void;
  /** Hand the draft of a request that was started as a draft to its session. */
  moveDraft: (from: string, to: string) => void;
  setCurrentId: (v: string | null) => void;
  /** Called when an approval_required event arrives for the foreground session. */
  onApprovalRequired: () => void;
  /** A backend-assigned session id, for the tab list. */
  onSessionNamed: (id: string) => void;
}

/**
 * One in-flight send. `key` is the registry slot the turn writes into: it
 * starts as the draft slot and migrates to the real session id as soon as the
 * backend names the session. `retired` is set when the turn ends, which is what
 * makes a late event from an aborted reader harmless.
 */
interface ActiveRequest {
  controller: AbortController;
  sessionId: string | null;
  key: string;
  retired: boolean;
}

/** A viewable conversation: its items, plus the request still writing them. */
interface Entry {
  items: Item[];
  request: ActiveRequest | null;
}

/** Per-session badge state for the tab bar and sidebar. */
export interface StreamActivity {
  busy: boolean;
  approvals: number;
}

export type StreamActivityMap = Record<string, StreamActivity>;

export function useChatStream(params: UseChatStreamParams) {
  // Always read the freshest external inputs through `latest` so `send`/`stop`
  // can be stable callbacks without ever capturing a stale closure.
  const latest = useRef(params);
  latest.current = params;

  // One entry per conversation, keyed by session id (or DRAFT_KEY). Streams are
  // independent: switching away does not abort, so a background turn keeps
  // appending to its own entry while another session is in the foreground.
  const [entries, setEntries] = useState<Map<string, Entry>>(() => new Map());
  // Mirror of `entries` for callers that must ask *after* an await whether a
  // conversation is still streaming (a render-scope read would be stale there).
  const entriesRef = useRef(entries);
  entriesRef.current = entries;

  const viewKey = params.currentId ?? DRAFT_KEY;

  const patchEntry = useCallback(
    (key: string, patch: (entry: Entry) => Entry) => {
      setEntries((prev) => {
        const entry = prev.get(key);
        if (!entry) return prev;
        const next = new Map(prev);
        next.set(key, patch(entry));
        return next;
      });
    },
    [],
  );

  const writeItems = useCallback(
    (key: string, updater: (prev: Item[]) => Item[]) =>
      patchEntry(key, (entry) => ({ ...entry, items: updater(entry.items) })),
    [patchEntry],
  );

  /** Replace one conversation's items outright (history load). */
  const loadHistory = useCallback(
    (key: string, items: Item[]) =>
      setEntries((prev) =>
        new Map(prev).set(key, { items, request: prev.get(key)?.request ?? null }),
      ),
    [],
  );

  /** Suspend one conversation's cached items. Ignored while its turn is running. */
  const dropEntry = useCallback((key: string) => {
    setEntries((prev) => {
      const entry = prev.get(key);
      if (!entry || entry.request) return prev;
      const next = new Map(prev);
      next.delete(key);
      return next;
    });
  }, []);

  const send = useCallback(async () => {
    const c = latest.current;
    const text = c.input.trim();
    // `entries` comes from the last committed render, which discrete events
    // (Enter, click) always observe: a second send from the same keystroke sees
    // the slot already claimed and is ignored.
    if (!text || c.sendBlocked) return;
    const key = c.currentId ?? DRAFT_KEY;
    if (entries.get(key)?.request) return;
    const viewToken = c.getViewToken();
    // Clear exactly the draft this send consumed: text typed in another
    // conversation (or into the next draft while this one streams) stays.
    c.clearDraft(key);
    const turnStartedAt = Date.now();
    const modelAtSend = c.currentModelName;
    const request: ActiveRequest = {
      controller: new AbortController(),
      sessionId: c.currentId,
      key,
      retired: false,
    };
    const opts: ChatOptions = {};
    if (request.sessionId === null) {
      if (c.chosenRoot) opts.root = c.chosenRoot;
      // The draft's list is always explicit (empty included): only a request
      // that omits the field inherits the project's secondary binding.
      opts.secondary_roots = c.secondary;
    }
    opts.permission_mode = c.permission;
    if (c.commandId) opts.command_id = c.commandId;
    opts.signal = request.controller.signal;
    const patches: Item[] = [
      // Stamp the time at send: rendering must not fall back to the clock.
      { kind: "user", text, time: new Date().toISOString() },
      { kind: "assistant", text: "" },
    ];
    setEntries((prev) => {
      const entry = prev.get(key);
      return new Map(prev).set(key, { items: [...(entry?.items ?? []), ...patches], request });
    });
    let terminalError = false;
    try {
      await streamChat(request.sessionId, text, (ev) => {
        // A turn that already ended owns nothing: the aborted reader of a
        // stopped turn must not write into whatever claimed the slot next.
        if (request.retired) return;
        if (ev.type === "session") {
          const id = ev.session_id;
          if (id) {
            // Adopt the id the backend assigned: the entry moves to the real
            // session slot so the tab bar and sidebar can find it, and stop()
            // targets this request's own session rather than the foreground.
            const from = request.key;
            request.sessionId = id;
            request.key = id;
            setEntries((prev) => {
              const entry = prev.get(from);
              if (!entry) return prev;
              const next = new Map(prev);
              next.delete(from);
              next.set(id, entry);
              return next;
            });
            // An unsent draft belongs to the conversation it was being typed
            // into, so it follows the id this request claimed.
            if (from === DRAFT_KEY) c.moveDraft(from, id);
            c.onSessionNamed(id);
            // Only follow the new session if the user is still on the view that
            // started this turn; otherwise it joins the list in the background.
            if (viewToken === latest.current.getViewToken()) c.setCurrentId(id);
          }
          c.refreshSessions();
          return;
        }
        // Raising the approval overlay is foreground state, not items, so a
        // background turn must not steal the view for its own approval.
        if (
          ev.type === "approval_required" &&
          request.key === (latest.current.currentId ?? DRAFT_KEY)
        ) {
          latest.current.onApprovalRequired();
        }
        if (ev.type === "error") terminalError = true;
        writeItems(request.key, (prev) => applyChatEvent(prev, ev));
      }, opts);
    } catch (err) {
      if (request.retired) return;
      if (isAbortError(err)) {
        writeItems(request.key, (prev) => applyChatEvent(prev, { type: "cancelled" }));
        return;
      }
      // The backend already reported a terminal error for this turn (the
      // stream then ended): do not append a second error row.
      if (!terminalError) {
        // Route through the reducer so a network failure also interrupts any
        // still-running tool card instead of leaving it spinning forever.
        writeItems(request.key, (prev) =>
          applyChatEvent(prev, {
            type: "error",
            error: err instanceof Error ? err.message : String(err),
          }),
        );
      }
    } finally {
      request.retired = true;
      writeItems(request.key, (prev) =>
        stampTurnMeta(expirePending(prev), modelAtSend, Date.now() - turnStartedAt),
      );
      patchEntry(request.key, (entry) => ({ ...entry, request: null }));
      c.refreshSessions();
    }
  }, [entries, patchEntry, writeItems]);

  const stop = useCallback(() => {
    // Abort the local reader + tell the backend to cancel; the send() finally
    // still runs and retires the entry while this request stays active.
    const request = entries.get(latest.current.currentId ?? DRAFT_KEY)?.request;
    if (!request) return;
    request.controller.abort();
    if (request.sessionId) cancelSessionChat(request.sessionId).catch(() => {});
  }, [entries]);

  /** Replace the foreground conversation's items (approval decisions). */
  const setItems = useCallback(
    (updater: Item[] | ((prev: Item[]) => Item[])) =>
      writeItems(viewKey, (prev) => (typeof updater === "function" ? updater(prev) : updater)),
    [viewKey, writeItems],
  );

  /** Whether a conversation still has a turn running, readable at any time. */
  const isStreaming = useCallback(
    (key: string) => entriesRef.current.get(key)?.request != null,
    [],
  );

  const activity = useMemo<StreamActivityMap>(() => {
    const map: StreamActivityMap = {};
    for (const [key, entry] of entries) {
      const approvals = currentTurn(entry.items).filter(
        (it) => it.kind === "approval" && it.state === "pending",
      ).length;
      map[key] = { busy: entry.request !== null, approvals };
    }
    return map;
  }, [entries]);

  return {
    send,
    stop,
    items: entries.get(viewKey)?.items ?? [],
    busy: entries.get(viewKey)?.request != null,
    activity,
    setItems,
    loadHistory,
    dropEntry,
    isStreaming,
  };
}
