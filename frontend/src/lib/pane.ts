// What the right-hand pane shows for the current turn: the context the agent
// pulled in, and the files it changed.
//
// Cards are built from what the tools actually returned, never from the call
// arguments alone: a search pattern is not a file, and a call that has not
// answered yet has produced no context.
import type { Item } from "../types";
import { basename } from "./paths";
import { num, parseResult, resultStatus, str } from "./toolResult";

/** Search hits offered as preview links before the rest are summarised. */
export const MAX_HITS = 5;

export interface ContextHit {
  /** What a preview request should open. */
  target: string;
  /** File path as shown to the reader. */
  label: string;
  /** Name of the root the path is relative to, when the hit names one. */
  rootLabel?: string;
  text: string;
  line: number;
}

export interface ContextCard {
  id: string;
  /** What the card is about: a path, or the pattern a search ran with. */
  sourceLabel: string;
  title: string;
  /** Body excerpt from the tool result. */
  excerpt: string;
  /** Right-aligned meta: line count or match count. */
  meta: string;
  kind: "read" | "search";
  /** Set when the call failed or matched nothing. */
  status: "ok" | "empty" | "error";
  /** The single file a preview should open; absent when the card means many. */
  previewTarget?: string;
  /** Why no preview link is offered, when one cannot be opened at all. */
  previewBlocked?: string;
  /** Name of the workspace root the file belongs to, when it has one. */
  rootLabel?: string;
  /** Individual files a search found, each previewable on its own. */
  hits?: ContextHit[];
}

export interface ChangeCard {
  id: string;
  path: string;
  diff: string;
  /** False for a `dry_run` preview: the patch was never written. */
  applied: boolean;
}

export interface PaneData {
  context: ContextCard[];
  changes: ChangeCard[];
}

/**
 * The file a result refers to, when the caller can be sure which one it means.
 *
 * A display path may be relative to any of the session's roots, so the explicit
 * target comes first; an absolute path in the call arguments is the fallback
 * for history recorded before results carried one.
 */
function targetOf(data: Record<string, unknown> | null, args: Record<string, unknown>): string {
  const explicit = str(data?.absolute_path);
  if (explicit) return explicit;
  const fromArgs = str(args.path);
  if (fromArgs.startsWith("/")) return fromArgs;
  const path = str(data?.path);
  if (path.startsWith("/")) return path;
  // Relative to whichever root holds it; the backend resolves it against the
  // session's roots, which is what every read of this file already went through.
  return path || fromArgs;
}

/** A search hit's file: the recorded root only appears for non-primary roots. */
function hitTarget(file: string, root: unknown): string {
  if (file.startsWith("/")) return file;
  const base = str(root);
  return base ? `${base.replace(/\/$/, "")}/${file}` : file;
}

/**
 * The workspace root a display path is relative to, or "" when it has none.
 *
 * The read tool reports a display path that is relative exactly when the file
 * lives under one of the session's roots; `absolute_path` then ends with it, so
 * the part before that suffix names the root.
 */
function rootOf(absolute: string, display: string): string {
  if (!absolute || !display || display.startsWith("/")) return "";
  const suffix = `/${display}`;
  return absolute.endsWith(suffix) ? absolute.slice(0, -suffix.length) : "";
}

/**
 * The size a read card shows: lines read against the file's own line count.
 *
 * `chars` is deliberately not used — it counts the tool's formatted output,
 * line numbers and footer included, so it is not the file's length.
 */
function readMeta(data: Record<string, unknown> | null, failed: boolean): string {
  if (failed) return "失败";
  if (typeof data?.total_lines !== "number") return "";
  const total = num(data.total_lines);
  return total === 0 ? "空文件" : `${num(data.lines)}/${total} 行`;
}

function isError(data: Record<string, unknown> | null): boolean {
  return resultStatus(data) === "error";
}

function errorText(data: Record<string, unknown> | null, raw: string | undefined): string {
  return str(data?.message) || raw || "工具执行失败";
}

/** Read/search calls become context cards; edits become change cards. */
export function paneData(turn: Item[]): PaneData {
  const context: ContextCard[] = [];
  const changes: ChangeCard[] = [];
  for (const item of turn) {
    if (item.kind !== "tool") continue;
    // A call that has not answered has produced nothing to show.
    if (!item.done || item.result === undefined) continue;
    const data = parseResult(item.result);
    if (item.name === "write_file" || item.name === "edit_file") {
      const diff = str(data?.diff);
      if (!diff) continue;
      changes.push({
        id: item.id,
        path: str(data?.path) || str(item.args.path),
        diff,
        // `dry_run` returns a patch preview and leaves the file untouched.
        applied: data?.dry_run !== true,
      });
      continue;
    }
    if (item.name === "read_file") {
      const failed = isError(data);
      const display = str(data?.path) || str(item.args.path);
      // An absolute display path means the file is under no root of this
      // session: the read-only preview API would refuse it, so the card keeps
      // the excerpt but must not offer a link that can only fail.
      const outside = display.startsWith("/");
      const root = outside ? "" : rootOf(str(data?.absolute_path), display);
      context.push({
        id: item.id,
        sourceLabel: display,
        title: str(data?.path).split("/").pop() || str(item.args.path),
        excerpt: failed ? errorText(data, item.result) : str(data?.content),
        meta: readMeta(data, failed),
        kind: "read",
        status: failed ? "error" : str(data?.content) ? "ok" : "empty",
        previewTarget: failed || outside ? undefined : targetOf(data, item.args),
        previewBlocked:
          !failed && outside
            ? "这个文件在工作区之外，本轮读取已获批准；面板只显示上面的摘录，不能预览。"
            : undefined,
        rootLabel: root ? basename(root) : undefined,
      });
      continue;
    }
    if (item.name === "grep") {
      const failed = isError(data);
      const rows = Array.isArray(data?.matches) ? data.matches : [];
      const hits: ContextHit[] = rows.map((row) => {
        const match = (row ?? {}) as Record<string, unknown>;
        const file = str(match.file);
        const root = str(match.root);
        return {
          target: hitTarget(file, root),
          label: file,
          // The same relative path can exist under several roots; the one that
          // recorded its root is what tells the two apart.
          rootLabel: root ? basename(root) : undefined,
          text: str(match.text),
          line: num(match.line),
        };
      });
      context.push({
        id: item.id,
        sourceLabel: str(item.args.pattern),
        title: "搜索",
        // The snippets live in the hit rows below, so the body only carries a
        // failure reason.
        excerpt: failed ? errorText(data, item.result) : "",
        meta: failed ? "失败" : `${hits.length} 处匹配`,
        kind: "search",
        status: failed ? "error" : hits.length ? "ok" : "empty",
        hits,
      });
      continue;
    }
    if (item.name === "glob") {
      const failed = isError(data);
      const rows = Array.isArray(data?.matches) ? data.matches : [];
      const hits: ContextHit[] = rows.map((row) => {
        if (typeof row === "string") {
          return { target: row, label: row, text: "", line: 0 };
        }
        const entry = (row ?? {}) as Record<string, unknown>;
        const path = str(entry.path);
        const root = str(entry.root);
        return {
          target: hitTarget(path, root),
          label: path,
          rootLabel: root ? basename(root) : undefined,
          text: "",
          line: 0,
        };
      });
      context.push({
        id: item.id,
        sourceLabel: str(item.args.pattern),
        title: "文件列表",
        excerpt: failed ? errorText(data, item.result) : "",
        meta: failed ? "失败" : `${hits.length} 个文件`,
        kind: "read",
        status: failed ? "error" : hits.length ? "ok" : "empty",
        hits,
      });
    }
  }
  return { context, changes };
}
