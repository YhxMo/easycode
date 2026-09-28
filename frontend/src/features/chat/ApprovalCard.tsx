import type { Item } from "../../types";
import { toolStep } from "./trace";

type ApprovalItem = Extract<Item, { kind: "approval" }>;

// The decision this card records is the *request's* outcome. Whether the call
// then succeeded is a separate fact, and it belongs to the tool row below — an
// approval the tool still had to refuse must not read as "allowed".
const DECIDED: Record<string, string> = {
  approved: "已批准执行请求",
  denied: "已拒绝请求",
  expired: "未响应，请求已失效",
};

interface Props {
  item: ApprovalItem;
  /** 1-based position among this turn's approvals, for the n/m counter. */
  position: number;
  total: number;
  onDecide: (approve: boolean, always: boolean) => void;
}

/** An approval request, inline in the stream exactly where the turn paused. */
export function ApprovalCard({ item, position, total, onDecide }: Props) {
  const step = toolStep({
    kind: "tool",
    id: item.toolCallId,
    name: item.name,
    args: item.args,
    done: true,
  });

  if (item.state !== "pending") {
    return (
      <div className={`approval-card decided ${item.state}`}>
        <span className="approval-verdict">{DECIDED[item.state] ?? item.state}</span>
        <span className="approval-what">{step.label}</span>
        {step.detail && <code className="approval-detail">{step.detail}</code>}
      </div>
    );
  }

  return (
    <section className="approval-card pending" aria-label="需要批准">
      <header className="approval-head">
        <span className="approval-title">需要批准</span>
        {total > 1 && (
          <span className="approval-count tabular">
            {position}/{total}
          </span>
        )}
      </header>
      <div className="approval-body">
        {step.detail ? (
          <>
            <span className="approval-what">{step.label}</span>
            <code className="approval-detail">{step.detail}</code>
          </>
        ) : (
          <span className="approval-what">{step.label}</span>
        )}
        {item.reason && <p className="approval-reason">{item.reason}</p>}
      </div>
      <div className="approval-actions">
        <button type="button" className="approval-deny" onClick={() => onDecide(false, false)}>
          拒绝
        </button>
        <button type="button" className="approval-always" onClick={() => onDecide(true, true)}>
          始终允许
        </button>
        <button type="button" className="approval-once primary" onClick={() => onDecide(true, false)}>
          允许一次
        </button>
      </div>
    </section>
  );
}
