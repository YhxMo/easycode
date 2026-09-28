// Unsent composer text, one record per conversation.
import { useCallback, useEffect, useMemo, useState } from "react";
import { commandNameIn, type CommandSelection } from "./commands";

/** A plain draft: text the user has typed but not sent. */
export interface PlainDraft {
  text: string;
  /** Caret offset restored when the conversation comes back to the foreground. */
  caret: number;
  /**
   * The `/` menu entry this draft picked, while the text is still that command.
   * It travels with the text so switching conversations cannot leave one
   * conversation's selection attached to another's words. ``null`` — never
   * absent — when the draft holds no selection.
   */
  command: CommandSelection | null;
}

/**
 * A draft that is rewriting an earlier message.
 *
 * It carries the revision the conversation was read at, because the server
 * refuses an edit that names a revision it has moved past — the user's own page
 * must not rewrite a conversation another tab has changed underneath it.
 */
export interface EditDraft extends PlainDraft {
  /** The turn being replaced. */
  editTurnId: string;
  expectedRevision: number;
  /** What the composer held before editing began, to restore on cancel. */
  previousDraft: PlainDraft;
}

export type Draft = PlainDraft | (EditDraft & { caret: number });

export function isEditing(draft: Draft): draft is EditDraft {
  return typeof (draft as EditDraft).editTurnId === "string";
}

const EMPTY: PlainDraft = { text: "", caret: 0, command: null };

/** Browser-local store of unsent text, keyed like the stream entries. */
export const DRAFTS_KEY = "easycode:drafts";

/** A stored selection, kept only when it is a complete id/name pair. */
function commandFrom(value: unknown): CommandSelection | null {
  const raw = value as { id?: unknown; name?: unknown } | null;
  if (!raw || typeof raw.id !== "string" || typeof raw.name !== "string") return null;
  return { id: raw.id, name: raw.name };
}

function plainFrom(value: unknown): PlainDraft | null {
  const draft = value as { text?: unknown; caret?: unknown; command?: unknown } | null;
  if (typeof draft?.text !== "string") return null;
  return {
    text: draft.text,
    caret: typeof draft.caret === "number" ? draft.caret : 0,
    command: commandFrom(draft.command),
  };
}

/** Restore the saved drafts: strings only, never fatal. */
function readStoredDrafts(): Map<string, Draft> {
  try {
    const saved = window.localStorage.getItem(DRAFTS_KEY);
    if (!saved) return new Map();
    const parsed: unknown = JSON.parse(saved);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return new Map();
    const out = new Map<string, Draft>();
    for (const [key, value] of Object.entries(parsed as Record<string, unknown>)) {
      if (!key) continue;
      const plain = plainFrom(value);
      if (plain === null) continue;
      const edit = value as {
        editTurnId?: unknown;
        expectedRevision?: unknown;
        commandId?: unknown;
        previousDraft?: unknown;
      };
      if (typeof edit.editTurnId === "string" && typeof edit.expectedRevision === "number") {
        // Drafts written before the selection moved into ``command`` kept a bare
        // id: the name it belonged to is the message's own first token, which is
        // exactly what the menu offers.
        const legacyId = typeof edit.commandId === "string" ? edit.commandId : null;
        const legacyName = legacyId ? commandNameIn(plain.text) : "";
        out.set(key, {
          ...plain,
          command: plain.command ?? (legacyId && legacyName ? { id: legacyId, name: legacyName } : null),
          editTurnId: edit.editTurnId,
          expectedRevision: edit.expectedRevision,
          previousDraft: plainFrom(edit.previousDraft) ?? EMPTY,
        });
        continue;
      }
      out.set(key, plain);
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

const sameCommand = (a: CommandSelection | null | undefined, b: CommandSelection | null | undefined) =>
  (a?.id ?? null) === (b?.id ?? null) && (a?.name ?? null) === (b?.name ?? null);

function isSame(a: Draft, b: Draft): boolean {
  if (a.text !== b.text || a.caret !== b.caret || !sameCommand(a.command, b.command)) return false;
  const ae = isEditing(a);
  const be = isEditing(b);
  if (ae !== be) return false;
  if (!ae || !be) return true;
  return (
    a.editTurnId === b.editTurnId &&
    a.expectedRevision === b.expectedRevision &&
    a.previousDraft.text === b.previousDraft.text &&
    a.previousDraft.caret === b.previousDraft.caret &&
    sameCommand(a.previousDraft.command, b.previousDraft.command)
  );
}

/** Nothing worth remembering: no text, no caret, and no edit to come back to. */
function isEmpty(draft: Draft): boolean {
  return draft.text === "" && draft.caret === 0 && !isEditing(draft);
}

export interface DraftStore {
  get: (key: string) => Draft;
  update: (key: string, patch: (draft: Draft) => Draft) => void;
  clear: (key: string) => void;
  /** Hand a draft to the conversation a request turned into. */
  move: (from: string, to: string) => void;
  /**
   * Open an edit of one recorded turn, remembering the draft it replaced.
   *
   * Only one level is kept: starting a second edit while one is open changes
   * the target but keeps the plain draft the user began with, so cancelling
   * always lands back on their own text rather than on an earlier edit's.
   */
  startEdit: (
    key: string,
    edit: {
      turnId: string;
      revision: number;
      /** The command the message was sent with; the text decides whether it still applies. */
      command: CommandSelection | null;
      text: string;
    },
  ) => void;
  /** Leave edit mode and put the remembered draft back. */
  cancelEdit: (key: string) => void;
  /** Re-anchor or drop an edit against the conversation as just read. */
  revalidateEdit: (key: string, turnIds: string[], revision?: number) => void;
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

  /** Replace one draft, given what it currently holds. */
  const update = useCallback((key: string, patch: (draft: Draft) => Draft) => {
    setMap((prev) => {
      const current = prev.get(key) ?? EMPTY;
      const next = patch(current);
      if (isSame(next, current)) return prev;
      const out = new Map(prev);
      if (isEmpty(next)) out.delete(key);
      else out.set(key, next);
      return out;
    });
  }, []);

  /**
   * Drop an edit whose target is gone, keeping the text the user began with.
   *
   * Called when a conversation is (re)read: the revision may have moved on, in
   * which case the edit is re-anchored, or the turn may have been replaced, in
   * which case there is nothing left to edit and the plain draft comes back.
   */
  const revalidateEdit = useCallback((key: string, turnIds: string[], revision?: number) => {
    setMap((prev) => {
      const current = prev.get(key);
      if (!current || !isEditing(current)) return prev;
      const next =
        turnIds.includes(current.editTurnId) && revision !== undefined
          ? { ...current, expectedRevision: revision }
          : current.previousDraft;
      if (isSame(next, current)) return prev;
      const out = new Map(prev);
      if (isEmpty(next)) out.delete(key);
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

  const startEdit = useCallback<DraftStore["startEdit"]>((key, edit) => {
    setMap((prev) => {
      const current = prev.get(key) ?? EMPTY;
      const previousDraft = isEditing(current) ? current.previousDraft : current;
      const next: Draft = {
        text: edit.text,
        caret: edit.text.length,
        editTurnId: edit.turnId,
        expectedRevision: edit.revision,
        command: edit.command,
        previousDraft,
      };
      const out = new Map(prev);
      out.set(key, next);
      return out;
    });
  }, []);

  const cancelEdit = useCallback((key: string) => {
    setMap((prev) => {
      const current = prev.get(key);
      if (!current || !isEditing(current)) return prev;
      const out = new Map(prev);
      const restored = current.previousDraft;
      if (restored.text === "" && restored.caret === 0) out.delete(key);
      else out.set(key, restored);
      return out;
    });
  }, []);

  // `get` is read during render, so it closes over the current map: the
  // mutators stay stable while the read follows the latest committed state.
  const get = useCallback((key: string) => map.get(key) ?? EMPTY, [map]);

  return useMemo(
    () => ({ get, update, clear, move, startEdit, cancelEdit, revalidateEdit }),
    [get, update, clear, move, startEdit, cancelEdit, revalidateEdit],
  );
}
