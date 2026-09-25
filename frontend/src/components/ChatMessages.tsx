import { Fragment, useCallback, useState, type ReactNode } from "react";
import type { Item } from "../types";
import { currentTimeLabel, formatClock, formatDuration } from "../lib/history";
import { traceOf } from "../lib/trace";
import { currentTurn } from "../chatStream";
import { Markdown } from "./Markdown";
import { ApprovalCard } from "./primitives/ApprovalCard";
import { ThinkingTrace } from "./primitives/ThinkingTrace";
import { TaskSummary } from "./primitives/TaskRows";

type ApprovalItem = Extract<Item, { kind: "approval" }>;

function CopyIcon({ copied = false }: { copied?: boolean }) {
  if (copied) {
    return (
      <svg className="message-action-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
        <path d="m5 12 4 4L19 6" />
      </svg>
    );
  }
  return (
    <svg className="message-action-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <rect x="8" y="8" width="11" height="11" rx="1.5" />
      <path d="M16 8V5.5A1.5 1.5 0 0 0 14.5 4h-9A1.5 1.5 0 0 0 4 5.5v9A1.5 1.5 0 0 0 5.5 16H8" />
    </svg>
  );
}

export function ChatMessages({
  items,
  busy,
  currentModelName,
  onDecide,
  onOpenTasks,
  onContinue,
}: {
  items: Item[];
  busy: boolean;
  currentModelName: string;
  onDecide: (item: ApprovalItem, approve: boolean, always: boolean) => void;
  onOpenTasks: () => void;
  /** Fill the composer with a continuation prompt; never sends on its own. */
  onContinue: () => void;
}) {
  const [copiedMessage, setCopiedMessage] = useState<number | null>(null);
  const copyMessage = useCallback((text: string, index: number) => {
    const write = navigator.clipboard?.writeText(text);
    if (!write) return;
    void write.then(() => {
      setCopiedMessage(index);
      window.setTimeout(
        () => setCopiedMessage((current) => (current === index ? null : current)),
        2000,
      );
    });
  }, []);

  // "1/3" counters are scoped to this turn: restored history approvals must not
  // inflate the position of the one being answered now.
  const approvalsThisTurn = currentTurn(items).filter(
    (it): it is ApprovalItem => it.kind === "approval",
  );
  const positionOf = (id: string) => approvalsThisTurn.findIndex((a) => a.id === id) + 1;

  // Consecutive tool calls collapse into one trace; text and approvals break it.
  const rows: ReactNode[] = [];
  let tools: Extract<Item, { kind: "tool" }>[] = [];
  const flushTools = () => {
    if (!tools.length) return;
    const trace = traceOf(tools);
    rows.push(<ThinkingTrace key={`trace-${tools[0].id || rows.length}`} trace={trace} />);
    tools = [];
  };

  items.forEach((it, i) => {
    if (it.kind === "tool") {
      tools.push(it);
      return;
    }
    flushTools();
    if (it.kind === "user") {
      rows.push(
        <div key={i} className="msg-row user">
          <div className="msg user">{it.text}</div>
          <div className="msg-meta">
            <span>{currentModelName}</span>
            <span className="msg-meta-separator">·</span>
            <time>{it.time ? formatClock(it.time) : currentTimeLabel()}</time>
            <button
              className="msg-action"
              title="复制消息"
              aria-label="复制消息"
              onClick={() => copyMessage(it.text, i)}
            >
              <CopyIcon copied={copiedMessage === i} />
            </button>
          </div>
        </div>,
      );
      return;
    }
    if (it.kind === "assistant") {
      if (!it.text) return;
      const metaParts: string[] = [];
      if (it.model) metaParts.push(it.model);
      if (typeof it.durationMs === "number") metaParts.push(formatDuration(it.durationMs));
      rows.push(
        <div key={i} className="msg-row assistant">
          <div className="msg assistant">
            <Markdown text={it.text} />
            {busy && i === items.length - 1 && <span className="cursor" />}
          </div>
          <div className="msg-meta">
            <button
              className="msg-action"
              title="复制回复"
              aria-label="复制回复"
              onClick={() => copyMessage(it.text, i)}
            >
              <CopyIcon copied={copiedMessage === i} />
            </button>
            {metaParts.map((part, pi) => (
              <Fragment key={pi}>
                <span className="msg-meta-separator">·</span>
                <span>{part}</span>
              </Fragment>
            ))}
          </div>
        </div>,
      );
      return;
    }
    if (it.kind === "approval") {
      rows.push(
        <ApprovalCard
          key={it.id}
          item={it}
          position={positionOf(it.id)}
          total={approvalsThisTurn.length}
          onDecide={(approve, always) => onDecide(it, approve, always)}
        />,
      );
      return;
    }
    if (it.kind === "todo") {
      rows.push(<TaskSummary key={i} todos={it.todos} onOpen={onOpenTasks} />);
      return;
    }
    if (it.kind === "review") {
      rows.push(
        <details key={i} className="review-card">
          <summary>自动审查与变更记录</summary>
          <pre>{it.text}</pre>
        </details>,
      );
      return;
    }
    if (it.kind === "notice") {
      rows.push(
        <div key={i} className="msg notice">
          {it.text}
        </div>,
      );
      return;
    }
    rows.push(
      <div key={i} className="msg error">
        <span>{it.text}</span>
        {/* A turn the server ended on purpose (a configured ceiling, say) is
            unfinished business: offering to pick it up is not the same as
            resending it, so this only fills the composer. */}
        {it.code && (
          <button
            type="button"
            className="msg-continue"
            title="把继续提示填入输入框（不会自动发送）；发送前先核对工作区现状"
            onClick={onContinue}
          >
            继续未完成任务
          </button>
        )}
      </div>,
    );
  });
  flushTools();

  return <>{rows}</>;
}
