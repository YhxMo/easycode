// What the right-hand pane shows for the current turn: the context the agent
// pulled in, and the files it changed.
//
// Cards are built from what the tools actually returned, never from the call
// arguments alone: a search pattern is not a file, and a call that has not
// answered yet has produced no context.
import type { GitFileState } from "../api";
import type { Item, ToolItem } from "../types";
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
  /** Canonical file the patch touched, so the file list can open it. */
  target?: string;
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

/** The tools this pane has a card for; every other result is left unparsed. */
const PANE_TOOLS = new Set(["write_file", "edit_file", "read_file", "grep", "glob"]);

/**
 * Read/search calls become context cards; edits become change cards.
 *
 * ``fullAccess`` reports whether the session currently runs in full access.
 * The preview follows the same boundary as the tools: a host path a
 * full-access read could open is previewable too, while a sandboxed session
 * keeps the excerpt-only card for anything outside its roots (the read-only
 * preview API refuses those).
 */
export function paneData(turn: Item[], opts: { fullAccess?: boolean } = {}): PaneData {
  const context: ContextCard[] = [];
  const changes: ChangeCard[] = [];
  for (const item of turn) {
    if (item.kind !== "tool") continue;
    // A call that has not answered has produced nothing to show, and results
    // from other tools (shell, MCP, delegation) never become cards here — so
    // they must not pay for a JSON parse on every render either.
    if (!item.done || item.result === undefined) continue;
    if (!PANE_TOOLS.has(item.name)) continue;
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
        target: targetOf(data, item.args),
      });
      continue;
    }
    if (item.name === "read_file") {
      const failed = isError(data);
      const display = str(data?.path) || str(item.args.path);
      // An absolute display path means the file is under no root of this
      // session. A sandboxed session's preview API refuses those, so the card
      // keeps the excerpt and must not offer a link that can only fail; full
      // access previews exactly what its reads may open.
      const outside = display.startsWith("/") && !opts.fullAccess;
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

/**
 * The pane's source for a conversation: its persisted tool records, plus the
 * calls this view holds that are not recorded yet.
 *
 * A record stands in for the live item of the same call: the message history's
 * copy may already have been trimmed by a compaction — which is exactly what
 * the record is kept for — and it always carries the file's canonical path,
 * while a live write/edit result names it only relatively.
 */
export function sessionToolItems(records: ToolItem[], items: Item[]): ToolItem[] {
  const recorded = new Set(records.map((record) => record.id));
  const out = [...records];
  for (const item of items) {
    if (item.kind === "tool" && !recorded.has(item.id)) out.push(item);
  }
  return out;
}

/** One file the conversation touched, deduplicated by its canonical target. */
export interface SessionFile {
  key: string;
  /** What a preview opens; empty when nothing can be opened. */
  target: string;
  label: string;
  rootLabel?: string;
  reads: number;
  searches: number;
  changes: number;
  /** Position of the last mention: the most recent files come first. */
  last: number;
}

/**
 * Files a conversation read, searched or changed, newest use first.
 *
 * Global file listings are deliberately left out: a `glob` names everything
 * that matches a pattern, which is not the same as the session having used it.
 */
export function sessionFiles(cards: ContextCard[], changes: ChangeCard[]): SessionFile[] {
  const files = new Map<string, SessionFile>();
  let order = 0;
  const touch = (
    label: string,
    target: string | undefined,
    rootLabel: string | undefined,
    use: "read" | "search" | "change",
  ) => {
    if (!label) return;
    // One canonical target identifies a file; without one, the display path
    // plus its root is all there is (the same relative name can exist twice).
    const key =
      target && target.startsWith("/") ? target : `${rootLabel ?? ""}\u0000${label}`;
    let file = files.get(key);
    if (!file) {
      file = { key, target: "", label, rootLabel, reads: 0, searches: 0, changes: 0, last: 0 };
      files.set(key, file);
    }
    order += 1;
    file.last = order;
    if (target && (!file.target || (target.startsWith("/") && !file.target.startsWith("/")))) {
      file.target = target;
    }
    if (rootLabel && !file.rootLabel) file.rootLabel = rootLabel;
    if (use === "read") file.reads += 1;
    else if (use === "search") file.searches += 1;
    else file.changes += 1;
  };

  for (const card of cards) {
    if (card.kind === "search") {
      for (const hit of card.hits ?? []) touch(hit.label, hit.target, hit.rootLabel, "search");
      continue;
    }
    if (card.kind === "read" && !card.hits?.length) {
      touch(card.sourceLabel, card.previewTarget, card.rootLabel, "read");
    }
  }
  for (const change of changes) touch(change.path, change.target, undefined, "change");
  return [...files.values()].sort((a, b) => b.last - a.last);
}

const GIT_CODE: Record<string, string> = {
  M: "修改",
  A: "新增",
  D: "删除",
  R: "重命名",
  C: "复制",
  T: "类型变更",
};

/** +/- line counts for a unified diff (headers excluded). */
export function diffStats(diff: string): { added: number; removed: number } {
  let added = 0;
  let removed = 0;
  for (const line of diff.split("\n")) {
    if (line.startsWith("+++") || line.startsWith("---")) continue;
    if (line.startsWith("+")) added += 1;
    else if (line.startsWith("-")) removed += 1;
  }
  return { added, removed };
}

/** One file's uncommitted state, as the working tree reports it right now. */
export function gitStateLabel(file: GitFileState): string {
  if (file.untracked) return "未跟踪";
  const parts: string[] = [];
  if (file.staged) parts.push(`已暂存${GIT_CODE[file.staged] ?? ""}`);
  if (file.unstaged) parts.push(`未暂存${GIT_CODE[file.unstaged] ?? ""}`);
  return parts.join(" · ") || "已修改";
}

/**
 * A row of the change section: one file's counts, and its diff underneath.
 *
 * The two sources of the section — what this conversation changed, and what the
 * working tree holds — produce the same rows, so a reader compares them the
 * same way; only the diff's own provenance differs, and that stays with the
 * group they are listed under.
 */
export interface ChangeRow {
  key: string;
  label: string;
  rootLabel?: string;
  added: number;
  removed: number;
  /** Extra state beside the counts: 未应用 / 未跟踪 / 已删除. */
  meta?: string;
  /** The diff to show below the row; absent when there is none to show. */
  diff?: string;
  /** Why there is no diff text, shown instead of one. */
  note?: string;
  /**
   * A working-tree row that can ask for its diff: the repository it lives in,
   * plus the file itself. Session-record rows carry their diff already, and a
   * file git no longer has (deleted) has nothing to open.
   */
  gitSource?: { repo: string; path: string; absolutePath: string };
}

/** Line totals over a set of rows, for the section's collapsed summary. */
export function rowStats(rows: ChangeRow[]): { added: number; removed: number } {
  return rows.reduce(
    (acc, row) => ({ added: acc.added + row.added, removed: acc.removed + row.removed }),
    { added: 0, removed: 0 },
  );
}

/** The files this conversation changed, newest first, as the store recorded them. */
export function sessionChangeRows(cards: ChangeCard[]): ChangeRow[] {
  return cards.map((card) => ({
    key: card.id,
    label: card.path,
    ...diffStats(card.diff),
    // A `dry_run` patch was previewed, never written.
    meta: card.applied ? undefined : "未应用",
    diff: card.diff,
  }));
}

/** The working tree's uncommitted files, with the diff each one carries. */
export function workingTreeRows(
  files: GitFileState[],
  diffs: Record<string, string> = {},
): ChangeRow[] {
  return files.map((file) => ({
    key: `git:${file.absolute_path}`,
    label: file.path,
    rootLabel: basename(file.repo),
    added: file.added ?? 0,
    removed: file.removed ?? 0,
    meta: gitStateLabel(file),
    // The stats request carries no diff text; one arrives when the reader opens
    // the row. An empty diff says nothing, so the row keeps just its counts.
    diff: file.diff || diffs[file.absolute_path],
    note: file.diff_note ?? undefined,
    // Nothing to open for a file git no longer has, or one whose contents are
    // not lines: the row says what it knows instead.
    gitSource:
      file.exists && !file.binary
        ? { repo: file.repo, path: file.path, absolutePath: file.absolute_path }
        : undefined,
  }));
}

/** A row of the pane's file list. */
export interface FileRow {
  key: string;
  label: string;
  rootLabel?: string;
  /** How the session used it, empty for a file only the working tree changed. */
  meta: string;
  target?: string;
  /** Uncommitted state, when the file is in one of the session's repositories. */
  git?: string;
}

/**
 * The file section: what the conversation touched, plus what is uncommitted.
 *
 * The two are not the same claim, so a file that only appears in the working
 * tree's changes is listed after the ones this session used, with its git state
 * as the reason it is there.
 */
export function fileRows(files: SessionFile[], gitFiles: GitFileState[]): FileRow[] {
  const dirty = new Map<string, GitFileState>();
  for (const file of gitFiles) dirty.set(file.absolute_path, file);
  const claimed = new Set<string>();
  const rows: FileRow[] = files.map((file) => {
    const state = file.target.startsWith("/") ? dirty.get(file.target) : undefined;
    if (state) claimed.add(state.absolute_path);
    const uses: string[] = [];
    if (file.reads) uses.push(`读 ${file.reads}`);
    if (file.searches) uses.push(`命中 ${file.searches}`);
    if (file.changes) uses.push(`改 ${file.changes}`);
    return {
      key: file.key,
      label: file.label,
      rootLabel: file.rootLabel,
      meta: uses.join(" · "),
      target: file.target || undefined,
      git: state ? gitStateLabel(state) : undefined,
    };
  });
  for (const file of gitFiles) {
    if (claimed.has(file.absolute_path)) continue;
    rows.push({
      key: `git:${file.absolute_path}`,
      label: file.path,
      rootLabel: basename(file.repo),
      meta: "",
      // A deleted file has nothing to open; the row still says it is gone.
      target: file.exists ? file.absolute_path : undefined,
      git: gitStateLabel(file),
    });
  }
  return rows;
}
