/** +/- line counts for a unified diff (headers excluded). */
function diffStats(diff: string) {
  let added = 0;
  let removed = 0;
  for (const line of diff.split("\n")) {
    if (line.startsWith("+++") || line.startsWith("---")) continue;
    if (line.startsWith("+")) added += 1;
    else if (line.startsWith("-")) removed += 1;
  }
  return { added, removed };
}

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
    </div>
  );
}
