// What the right-hand pane shows for the current turn: the context the agent
// pulled in, and the files it changed.
import type { Item } from "../types";

export interface ContextCard {
  id: string;
  /** File path or search pattern, as shown in the card's source line. */
  source: string;
  title: string;
  /** Body excerpt from the tool result. */
  excerpt: string;
  /** Right-aligned meta: character or match count. */
  meta: string;
  kind: "read" | "search";
}

export interface ChangeCard {
  id: string;
  path: string;
  diff: string;
}

export interface PaneData {
  context: ContextCard[];
  changes: ChangeCard[];
}

function parse(raw: string | undefined): Record<string, unknown> | null {
  if (!raw) return null;
  try {
    const value: unknown = JSON.parse(raw);
    return typeof value === "object" && value !== null ? (value as Record<string, unknown>) : null;
  } catch {
    return null;
  }
}

const text = (value: unknown): string => (typeof value === "string" ? value : "");

/** Read/search calls become context cards; edits become change cards. */
export function paneData(turn: Item[]): PaneData {
  const context: ContextCard[] = [];
  const changes: ChangeCard[] = [];
  for (const item of turn) {
    if (item.kind !== "tool") continue;
    const result = parse(item.result);
    if (item.name === "edit_file" || item.name === "write_file") {
      const diff = text(result?.diff);
      if (diff) changes.push({ id: item.id, path: text(result?.path) || text(item.args.path), diff });
      continue;
    }
    if (item.name === "read_file" || item.name === "glob") {
      const path = text(result?.path) || text(item.args.path) || text(item.args.pattern);
      const content = text(result?.content);
      const chars = Number(result?.chars ?? 0) || content.length;
      context.push({
        id: item.id,
        source: path,
        title: item.name === "glob" ? "文件列表" : (path.split("/").pop() ?? path),
        excerpt: content || text(result?.matches) || "",
        meta: item.name === "glob" ? `${Number(result?.count ?? 0)} 个文件` : `${chars} 字符`,
        kind: "read",
      });
      continue;
    }
    if (item.name === "grep") {
      const matches = Array.isArray(result?.matches) ? result.matches : [];
      const first = matches[0] as { path?: string; line?: number; text?: string } | undefined;
      context.push({
        id: item.id,
        source: text(item.args.pattern),
        title: "搜索",
        excerpt: first ? `${first.path}:${first.line} ${first.text ?? ""}` : "",
        meta: `${matches.length} 处匹配`,
        kind: "search",
      });
    }
  }
  return { context, changes };
}
