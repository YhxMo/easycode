import type { CommandInfo } from "../api";

export function filterCommands(commands: CommandInfo[], query: string): CommandInfo[] {
  const q = (query.startsWith("/") ? query.slice(1) : query).toLowerCase();
  return commands.filter(
    (c) =>
      c.name.startsWith(q) ||
      (q.length > 0 && c.name.includes(q)) ||
      c.description.toLowerCase().includes(q),
  );
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
