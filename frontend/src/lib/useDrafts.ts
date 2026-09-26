// Unsent composer text, one record per conversation.
import { useCallback, useEffect, useMemo, useState } from "react";

export interface Draft {
  text: string;
  /** Caret offset restored when the conversation comes back to the foreground. */
  caret: number;
}

const EMPTY: Draft = { text: "", caret: 0 };

/** Browser-local store of unsent text, keyed like the stream entries. */
export const DRAFTS_KEY = "easycode:drafts";

/** Restore the saved drafts: strings only, never fatal. */
function readStoredDrafts(): Map<string, Draft> {
  try {
    const saved = window.localStorage.getItem(DRAFTS_KEY);
    if (!saved) return new Map();
    const parsed: unknown = JSON.parse(saved);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return new Map();
    const out = new Map<string, Draft>();
    for (const [key, value] of Object.entries(parsed as Record<string, unknown>)) {
      const draft = value as { text?: unknown; caret?: unknown };
      if (!key || typeof draft?.text !== "string") continue;
      out.set(key, {
        text: draft.text,
        caret: typeof draft.caret === "number" ? draft.caret : 0,
      });
    }
    return out;
  } catch {
    // localStorage 不可用（隐私模式等）——仅内存态，忽略即可
    return new Map();
  }
}

function storeDrafts(map: Map<string, Draft>): void {
  try {
    window.localStorage.setItem(DRAFTS_KEY, JSON.stringify(Object.fromEntries(map)));
  } catch {
    // 同上：写不进去时草稿仍在本页内存里，不影响这次会话
  }
}

export interface DraftStore {
  get: (key: string) => Draft;
  update: (key: string, patch: (draft: Draft) => Draft) => void;
  clear: (key: string) => void;
  /** Hand a draft to the conversation a request turned into. */
  move: (from: string, to: string) => void;
}

/**
 * Per-conversation drafts, keyed exactly like the stream entries (session id,
 * or the draft key of a not-yet-started conversation).
 *
 * They live in this browser's localStorage so a refresh keeps what was typed;
 * sending or deleting the conversation clears its entry, and the text never
 * reaches the server as a draft.
 */
export function useDrafts(): DraftStore {
  const [map, setMap] = useState<Map<string, Draft>>(() => readStoredDrafts());

  // Persist after the commit, so a keystroke's draft is on disk before the
  // next one arrives and no reducer runs a side effect.
  useEffect(() => {
    storeDrafts(map);
  }, [map]);

  const update = useCallback((key: string, patch: (draft: Draft) => Draft) => {
    setMap((prev) => {
      const current = prev.get(key) ?? EMPTY;
      const next = patch(current);
      if (next.text === current.text && next.caret === current.caret) return prev;
      const out = new Map(prev);
      if (next.text === "" && next.caret === 0) out.delete(key);
      else out.set(key, next);
      return out;
    });
  }, []);

  const clear = useCallback((key: string) => {
    setMap((prev) => {
      if (!prev.has(key)) return prev;
      const next = new Map(prev);
      next.delete(key);
      return next;
    });
  }, []);

  const move = useCallback((from: string, to: string) => {
    if (from === to) return;
    setMap((prev) => {
      const draft = prev.get(from);
      if (!draft) return prev;
      const next = new Map(prev);
      next.delete(from);
      next.set(to, draft);
      return next;
    });
  }, []);

  // `get` is read during render, so it closes over the current map: the
  // mutators stay stable while the read follows the latest committed state.
  const get = useCallback((key: string) => map.get(key) ?? EMPTY, [map]);

  return useMemo(() => ({ get, update, clear, move }), [get, update, clear, move]);
}
