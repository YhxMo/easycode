// Unsent composer text, one record per conversation.
import { useCallback, useMemo, useState } from "react";

export interface Draft {
  text: string;
  /** Caret offset restored when the conversation comes back to the foreground. */
  caret: number;
}

const EMPTY: Draft = { text: "", caret: 0 };

export interface DraftStore {
  get: (key: string) => Draft;
  update: (key: string, patch: (draft: Draft) => Draft) => void;
  clear: (key: string) => void;
  /** Hand a draft to the conversation a request turned into. */
  move: (from: string, to: string) => void;
}

/**
 * Per-conversation drafts, keyed exactly like the stream entries (session id,
 * or the draft key before the backend names it).
 *
 * Deliberately memory-only: an unsent private message is not written to
 * browser storage, so a refresh starts with empty composers.
 */
export function useDrafts(): DraftStore {
  const [map, setMap] = useState<Map<string, Draft>>(() => new Map());

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
