import type { ModelsInfo } from "../api";
import type { PaneSection } from "../features/pane/RightPane";

export const EMPTY_MODELS: ModelsInfo = { default: "", models: {}, providers: {}, limits: {} };

/**
 * Chat width at which the pane covers the stream instead of sharing the row.
 * Mirrors the `@container` rule for `.right-pane` in styles.css: keeping the
 * conversation at least 560px wide is what both numbers express.
 */
export const NARROW_CHAT_PX = 950;

/** Offered after a turn the server ended on purpose; filled in, never sent. */
export const CONTINUE_PROMPT =
  "继续完成上一轮未完成的任务。先核对工作区里已经发生的改动和任务清单，再决定下一步。";

/** Right-hand pane sections, in the order the tabs appear. */
export const PANE_SECTIONS: PaneSection[] = [
  { id: "tasks", label: "任务" },
  { id: "context", label: "上下文" },
  { id: "changes", label: "变更" },
  { id: "file", label: "文件" },
];

/** Key of the conversation list the tabs were opened from. */
export const OPEN_TABS_KEY = "easycode:open_tabs";

/** The conversation that was on screen, so a refresh comes back to it. */
export const CURRENT_TAB_KEY = "easycode:current_tab";

/** Restore the stored tab list: strings only, de-duplicated, never fatal. */
export function readStoredTabs(): string[] {
  try {
    const saved = window.localStorage.getItem(OPEN_TABS_KEY);
    if (!saved) return [];
    const parsed: unknown = JSON.parse(saved);
    if (!Array.isArray(parsed)) return [];
    return [...new Set(parsed.filter((v): v is string => typeof v === "string" && v !== ""))];
  } catch {
    // localStorage 不可用（隐私模式等）——仅内存态，忽略即可
    return [];
  }
}

export function readStoredCurrentTab(): string | null {
  try {
    return window.localStorage.getItem(CURRENT_TAB_KEY) || null;
  } catch {
    return null;
  }
}
