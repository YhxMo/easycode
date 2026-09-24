import type { Trace } from "../../lib/trace";

/** Compact summary of what a turn's tools did, shown next to the trace header. */
export function ToolChips({ trace }: { trace: Trace }) {
  const shells = trace.steps.filter((s) => s.name === "execute_shell").length;
  const edits = trace.steps.filter((s) => s.name === "edit_file" || s.name === "write_file").length;
  const chips: string[] = [];
  if (trace.reads) chips.push(`读取 ${trace.reads}`);
  if (trace.searches) chips.push(`搜索 ${trace.searches}`);
  if (edits) chips.push(`修改 ${edits}`);
  if (shells) chips.push(`Shell ${shells}`);
  if (!chips.length) {
    return <span className="chip">{trace.steps.length} 次工具调用</span>;
  }
  return (
    <>
      {chips.map((text) => (
        <span key={text} className="chip">
          {text}
        </span>
      ))}
    </>
  );
}
