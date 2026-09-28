import { useCallback } from "react";
import type { RefObject } from "react";
import { fetchSession, submitApproval } from "../api";
import type { ApprovalState, Item } from "../types";
import { commandNameIn } from "../features/composer/commands";
import type { DraftStore } from "../features/composer/useDrafts";
import type { StickToBottom } from "../lib/useStickToBottom";
import { CONTINUE_PROMPT } from "./constants";

/** One user message, as the transcript holds it. */
type UserItem = Extract<Item, { kind: "user" }>;

export interface MessageActionsInputs {
  drafts: DraftStore;
  /** The conversation whose draft is on screen. */
  draftKey: string;
  currentId: string | null;
  busy: boolean;
  sendBlocked: boolean;
  fieldRef: RefObject<HTMLTextAreaElement | null>;
  sticky: StickToBottom;
  insertDraftText: (text: string) => void;
  setItems: (update: (prev: Item[]) => Item[]) => void;
  toast: (kind: "ok" | "err", text: string) => void;
}

/**
 * What the transcript's own controls do: answer an approval, pick up an
 * interrupted turn, or send an earlier message back to the composer.
 *
 * Everything here writes through the draft the conversation on screen owns, and
 * reads the revision it is editing from the server at the moment of the click —
 * the view's copy can be older than the last turn it streamed.
 */
export function useMessageActions({
  drafts,
  draftKey,
  currentId,
  busy,
  sendBlocked,
  fieldRef,
  sticky,
  insertDraftText,
  setItems,
  toast,
}: MessageActionsInputs) {
  const decideApproval = useCallback(
    async (item: Extract<Item, { kind: "approval" }>, approve: boolean, always: boolean) => {
      const mark = (state: ApprovalState) =>
        setItems((prev) =>
          prev.map((it) => (it.kind === "approval" && it.id === item.id ? { ...it, state } : it)),
        );
      mark(approve ? "approved" : "denied");
      try {
        await submitApproval(item.id, approve, always);
      } catch {
        // The approval was already resolved (e.g. turn cancelled, timed out or
        // the user refreshed) — mark it expired instead of reverting to pending.
        mark("expired");
      }
    },
    [setItems],
  );

  /**
   * A continuation the user has to agree to: the prompt goes into the composer
   * and nothing is sent. What the interrupted turn already did is still on
   * disk, so the message tells the model to look before it acts.
   */
  const continueUnfinished = useCallback(() => {
    // Never overwrite what the user is writing: the draft is theirs.
    if (!drafts.get(draftKey).text.trim()) insertDraftText(CONTINUE_PROMPT);
    fieldRef.current?.focus();
    sticky.stick();
  }, [drafts, draftKey, insertDraftText, fieldRef, sticky]);

  /**
   * Send one earlier message back to the composer.
   *
   * The revision is read from the server right now rather than kept from the
   * last load: an edit has to name the conversation as it is, and the view's
   * copy can be older than the last turn it streamed. A failed read leaves the
   * draft alone and explains itself — editing a conversation the server has
   * moved past would either be refused or, worse, replace the wrong turn.
   */
  const startEdit = useCallback(
    (item: UserItem) => {
      if (busy || sendBlocked || currentId === null) return;
      const key = draftKey;
      // The command this message was sent with, taken from the message itself:
      // the id the server recorded, and the name its own first token writes.
      // Whatever the composer happens to be holding is not this message's.
      const name = commandNameIn(item.text);
      const command = item.commandId && name ? { id: item.commandId, name } : null;
      fetchSession(currentId)
        .then((detail) => {
          if (detail.revision === undefined) return;
          drafts.startEdit(key, {
            turnId: item.turnId ?? "",
            revision: detail.revision,
            command,
            text: item.text,
          });
          fieldRef.current?.focus();
          sticky.stick();
        })
        .catch((e: unknown) => {
          toast("err", `无法开始编辑: ${e instanceof Error ? e.message : String(e)}`);
        });
    },
    [busy, currentId, draftKey, drafts, sendBlocked, toast, fieldRef, sticky],
  );

  return { decideApproval, continueUnfinished, startEdit };
}
