import type { CommandInfo } from "../api";

/** Display order of the command-menu sections; also the cursor order. */
const KIND_ORDER: Record<CommandInfo["kind"], number> = { skill: 0, template: 1, builtin: 2 };

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
 * Single source of truth for the command-menu boundary logic. Both the
 * CommandMenu component and App's textarea onKeyDown use this so the ArrowUp /
 * ArrowDown clamp behavior cannot drift between the two implementations.
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
