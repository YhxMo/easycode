import { useMemo, useState } from "react";
import type { Trace, TraceStep } from "./trace";
import { parseResult, str } from "./toolResult";
import { DiffView } from "../../components/primitives/DiffView";
import { ToolChips } from "./ToolChips";

function StatusIcon({ status }: { status: TraceStep["status"] }) {
  if (status === "running") return <span className="step-dot" aria-hidden="true" />;
  if (status === "error") {
    return (
      <svg className="step-icon error" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
        <path d="M7 7l10 10M17 7L7 17" />
      </svg>
    );
  }
  return (
    <svg className="step-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <path d="m5 12.5 4.5 4.5L19 7" />
    </svg>
  );
}

/** The indented payload, built only when this is the branch that renders. */
function PrettyResult({
  parsed,
  raw,
}: {
  parsed: Record<string, unknown> | null;
  raw: string;
}) {
  return <pre className="step-result">{parsed ? JSON.stringify(parsed, null, 2) : raw}</pre>;
}

/** A tool result, rendered as a patch, a payload body, or plain text. */
function StepResult({ step }: { step: TraceStep }) {
  // Parsed only while expanded: a collapsed step never pays for it.
  const raw = step.raw ?? "";
  const parsed = useMemo(() => parseResult(raw), [raw]);
  const diff = str(parsed?.diff);
  if (diff) {
    return <DiffView path={str(parsed?.path) || undefined} diff={diff} />;
  }
  const message = str(parsed?.message) && !parsed?.content ? str(parsed?.message) : "";
  if (message) return <pre className="step-result">{message}</pre>;
  return <PrettyResult parsed={parsed} raw={raw} />;
}

function StepRow({ step }: { step: TraceStep }) {
  const [open, setOpen] = useState(false);
  const expandable = Boolean(step.raw);
  return (
    <li className={`trace-step ${step.status}`}>
      <button
        type="button"
        className="step-head"
        disabled={!expandable}
        aria-expanded={expandable ? open : undefined}
        onClick={() => expandable && setOpen((v) => !v)}
      >
        <StatusIcon status={step.status} />
        <span className="step-label">{step.label}</span>
        {step.detail && (
          <span className="step-detail" title={step.detail}>
            {step.detail}
          </span>
        )}
      </button>
      {open && step.raw && <StepResult step={step} />}
    </li>
  );
}

/** One turn's tool calls: collapsed to chips, expandable to each step's output. */
export function ThinkingTrace({ trace }: { trace: Trace }) {
  const [open, setOpen] = useState(false);
  if (!trace.steps.length) return null;
  return (
    <div className={`trace${trace.running ? " running" : ""}`}>
      <button
        type="button"
        className="trace-head"
        aria-expanded={open}
        aria-label={open ? "收起工具调用" : "展开工具调用"}
        onClick={() => setOpen((v) => !v)}
      >
        <span className={`side-caret${open ? " open" : ""}`} aria-hidden="true">
          <svg viewBox="0 0 24 24" focusable="false">
            <path d="M9 5.5 15.5 12 9 18.5" />
          </svg>
        </span>
        <ToolChips trace={trace} />
        {trace.running && <span className="trace-live" aria-hidden="true" />}
      </button>
      {open && (
        <ol className="trace-steps">
          {trace.steps.map((step) => (
            <StepRow key={step.id} step={step} />
          ))}
        </ol>
      )}
    </div>
  );
}
