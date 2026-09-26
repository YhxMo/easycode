import { diffStats } from "../../lib/pane";

function lineClass(line: string): string {
  if (line.startsWith("+++") || line.startsWith("---") || line.startsWith("diff ")) return "meta";
  if (line.startsWith("@@")) return "hunk";
  if (line.startsWith("+")) return "add";
  if (line.startsWith("-")) return "del";
  return "ctx";
}

/** The `+++ b/<path>` header a unified diff carries, when the caller has none. */
function pathFromDiff(diff: string): string | undefined {
  const match = /^\+\+\+ [ab]\/(.+)$/m.exec(diff) ?? /^\+\+\+ (.+)$/m.exec(diff);
  return match?.[1]?.trim() || undefined;
}

/** Just the diff's lines, for a caller that draws its own file header. */
export function DiffBody({ diff }: { diff: string }) {
  return (
    <pre className="diff-body">
      {diff
        .replace(/\n$/, "")
        .split("\n")
        .map((line, index) => (
          <span key={index} className={`diff-line ${lineClass(line)}`}>
            {line || " "}
          </span>
        ))}
    </pre>
  );
}

interface Props {
  path?: string;
  diff: string;
  stats?: { added: number; removed: number };
  /** False for a `dry_run` patch: the file was never touched. */
  applied?: boolean;
}

/** Unified diff: file identity above, +/- coloured lines below. */
export function DiffView({ path, diff, stats, applied = true }: Props) {
  const counts = stats ?? diffStats(diff);
  const label = path ?? pathFromDiff(diff);
  return (
    <div className={`diff-view${applied ? "" : " dry-run"}`}>
      <div className="diff-head">
        <span className="diff-path" title={label}>
          {label ?? "改动"}
        </span>
        {!applied && <span className="diff-badge">未应用</span>}
        <span className="diff-stats tabular">
          <span className="add">+{counts.added}</span>
          <span className="del">−{counts.removed}</span>
        </span>
      </div>
      <DiffBody diff={diff} />
    </div>
  );
}
