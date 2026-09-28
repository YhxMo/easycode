import type { CommandInfo } from "../../api";

/** Display order of the command-menu sections; also the cursor order. */
const KIND_ORDER: Record<CommandInfo["kind"], number> = {
  skill: 0,
  mcp: 1,
  template: 2,
  builtin: 3,
};

/** The `/` menu entry a draft is still holding. */
export interface CommandSelection {
  id: string;
  /** Without the leading `/`, e.g. `mcp:demo`. */
  name: string;
}

/** The command name a message writes, from its first token ("" when none). */
export function commandNameIn(text: string): string {
  const trimmed = text.trimStart();
  if (!trimmed.startsWith("/")) return "";
  return trimmed.slice(1).split(/\s/, 1)[0] ?? "";
}

export function filterCommands(commands: CommandInfo[], query: string): CommandInfo[] {
  const q = (query.startsWith("/") ? query.slice(1) : query).toLowerCase();
  // The filtered list is the single source of order: it is grouped by kind
  // (matching the menu sections), so the highlighted item and the item picked
  // by Enter are always the same. Array#sort is stable within a kind.
  return commands
    .filter(
      (c) =>
        c.name.startsWith(q) ||
        (q.length > 0 && c.name.includes(q)) ||
        c.description.toLowerCase().includes(q),
    )
    .sort((a, b) => KIND_ORDER[a.kind] - KIND_ORDER[b.kind]);
}

/** Clamp a command-list cursor to a valid index (0 when the list is empty). */
export function clampCommandIndex(filtered: CommandInfo[], index: number): number {
  const count = filtered.length;
  if (count === 0) return 0;
  return Math.min(Math.max(index, 0), count - 1);
}

/**
 * The id a request should send: the entry the user picked, for as long as the
 * text is still that command. Anything else means they typed over it, and a
 * leftover id must never expand a prompt it no longer describes.
 */
export function activeCommandId(
  picked: CommandSelection | null,
  text: string,
): string | null {
  if (!picked) return null;
  const trimmed = text.trimStart();
  if (!trimmed.startsWith(`/${picked.name}`)) return null;
  const rest = trimmed.slice(picked.name.length + 1);
  return rest === "" || /^\s/.test(rest) ? picked.id : null;
}

/**
 * Single source of truth for the command-menu boundary logic. Both the
 * CommandMenu component and the composer's `onKeyDown` use this, so the
 * ArrowUp / ArrowDown clamp behavior cannot drift between the two.
 * @param filtered the filtered command list under the current query
 * @param index    the current cursor index
 * @param delta    +1 (ArrowDown) or -1 (ArrowUp)
 */
export function moveCommandCursor(filtered: CommandInfo[], index: number, delta: -1 | 1): number {
  const count = filtered.length;
  if (count === 0) return 0;
  if (delta > 0) return Math.min(index + 1, count - 1);
  return Math.max(index - 1, 0);
}
