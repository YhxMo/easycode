import { useState } from "react";
import type { Trace, TraceStep } from "../../lib/trace";
import { DiffView } from "./DiffView";
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

function parseJsonObject(raw: string | undefined): Record<string, unknown> | null {
  if (!raw) return null;
  try {
    const value: unknown = JSON.parse(raw);
    return typeof value === "object" && value !== null ? (value as Record<string, unknown>) : null;
  } catch {
    return null;
  }
}

/** A tool result, rendered as a patch, a payload body, or plain text. */
function StepResult({ step }: { step: TraceStep }) {
  const parsed = parseJsonObject(step.result);
  const diff = typeof parsed?.diff === "string" ? parsed.diff : null;
  if (diff) {
    const path = typeof parsed?.path === "string" ? parsed.path : undefined;
    return <DiffView path={path} diff={diff} />;
  }
  const message = typeof parsed?.message === "string" && !parsed?.content ? parsed.message : null;
  if (message) return <pre className="step-result">{message}</pre>;
  return <pre className="step-result">{step.result}</pre>;
}

function StepRow({ step }: { step: TraceStep }) {
  const [open, setOpen] = useState(false);
  const expandable = Boolean(step.result);
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
      {open && step.result && <StepResult step={step} />}
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
