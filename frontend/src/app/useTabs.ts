import { useCallback, useEffect, useState } from "react";
import type { SessionSummary } from "../api";
import { DRAFT_KEY } from "../features/chat/useChatStream";
import type { OpenTab } from "../features/sidebar/TabBar";
import {
  CURRENT_TAB_KEY,
  OPEN_TABS_KEY,
  readStoredCurrentTab,
  readStoredTabs,
} from "./constants";

/**
 * The open tabs: the conversations the reader has opened, in order.
 *
 * A tab is an id. Closing one closes only the view — the conversation stays in
 * the sidebar and a background turn keeps streaming into its own slot — so the
 * list is pruned only when the server says the conversation is gone, or when it
 * left the main list on purpose.
 *
 * `currentId` is the conversation on screen; remembering it lives here so both
 * ends of that storage key are this hook's.
 */
export function useTabs(currentId: string | null) {
  const [open, setOpen] = useState<string[]>(() => readStoredTabs());
  // The conversation that was on screen, read once: the effect that writes the
  // current (still empty) view back to the same key runs before a later read
  // could, so reading it again would always find "none".
  const [remembered] = useState(readStoredCurrentTab);

  useEffect(() => {
    try {
      window.localStorage.setItem(OPEN_TABS_KEY, JSON.stringify(open));
    } catch {
      // localStorage 不可用（隐私模式等）——仅内存态，忽略即可
    }
  }, [open]);

  useEffect(() => {
    try {
      window.localStorage.setItem(CURRENT_TAB_KEY, currentId ?? "");
    } catch {
      // localStorage 不可用（隐私模式等）——仅内存态，忽略即可
    }
  }, [currentId]);

  const register = useCallback((id: string) => {
    setOpen((prev) => (prev.includes(id) ? prev : [...prev, id]));
  }, []);

  /**
   * Keep only the ids the newest list still carries.
   *
   * Called after a successful load and never on initialization or failure: an
   * older reply that lost the race must not prune a tab the newest list still
   * has (e.g. one a turn named while the request was out).
   */
  const keepOnly = useCallback((known: Set<string>) => {
    setOpen((prev) => {
      const kept = prev.filter((id) => known.has(id));
      return kept.length === prev.length ? prev : kept;
    });
  }, []);

  /** Drop ids that left the main list on purpose (archived). */
  const drop = useCallback((gone: Set<string>) => {
    setOpen((prev) => (prev.some((id) => gone.has(id)) ? prev.filter((id) => !gone.has(id)) : prev));
  }, []);

  return { open, setOpen, register, keepOnly, drop, remembered };
}

/**
 * The strip as drawn: tabs are ids, and the titles come from the session list
 * so a rename shows up. The start page keeps its own leading tab so the strip
 * always names what is on screen.
 */
export function tabStrip(
  open: string[],
  currentId: string | null,
  sessionById: Map<string, SessionSummary>,
): OpenTab[] {
  const sessionTabs = open.map((id) => ({ id, title: sessionById.get(id)?.title ?? "会话" }));
  return currentId === null ? [{ id: DRAFT_KEY, title: "新会话", draft: true }, ...sessionTabs] : sessionTabs;
}
