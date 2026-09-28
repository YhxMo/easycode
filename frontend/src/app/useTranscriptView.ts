import { useMemo } from "react";
import type { Item } from "../types";
import { currentTurn } from "../features/chat/chatStream";
import type { StreamActivityMap } from "../features/chat/useChatStream";

/**
 * What the transcript on screen currently shows.
 *
 * Every value here is derived from the items themselves, in one place, because
 * they have to agree with each other: a loading row that says "thinking" while
 * an approval is waiting, or a switch that is allowed while a background turn
 * runs, are both failures of reading the same list two ways.
 */
export function useTranscriptView(items: Item[], activity: StreamActivityMap) {
  const exploration = useMemo(() => {
    let reads = 0;
    let searches = 0;
    for (const item of items) {
      if (item.kind !== "tool") continue;
      if (/^(read_file|glob)$/.test(item.name)) reads += 1;
      if (item.name === "grep") searches += 1;
    }
    return { reads, searches };
  }, [items]);

  const lastItem = items[items.length - 1];
  const activeTool = lastItem?.kind === "tool" && !lastItem.done;
  // Any turn on this page: a model switch rebinds every session, so a
  // background turn blocks it just as the foreground one does.
  const anyBusy = useMemo(() => Object.values(activity).some((a) => a.busy), [activity]);
  const turn = useMemo(() => currentTurn(items), [items]);
  // Scoped to the current turn (items after the last user message): restored
  // history approvals must not read as work waiting on the user now.
  const pendingApprovals = useMemo(
    () =>
      turn.filter(
        (it): it is Extract<Item, { kind: "approval" }> =>
          it.kind === "approval" && it.state === "pending",
      ),
    [turn],
  );
  // Turns are counted by user messages: a choice made in one turn must not
  // carry over to the next.
  const turnNo = useMemo(() => items.filter((it) => it.kind === "user").length, [items]);

  return {
    exploration,
    activeTool,
    anyBusy,
    turn,
    turnNo,
    pendingApprovals,
    pendingApproval: pendingApprovals.length > 0,
  };
}
