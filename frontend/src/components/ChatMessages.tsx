import { Fragment, useCallback, useState } from "react";
import type { Item } from "../types";
import { currentTimeLabel, formatClock, formatDuration } from "../lib/history";
import { Markdown } from "./Markdown";
import { ToolCard } from "../ToolCard";

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

export function ChatMessages({ items, busy, currentModelName }: {
  items: Item[];
  busy: boolean;
  currentModelName: string;
}) {
  const [copiedMessage, setCopiedMessage] = useState<number | null>(null);
  const copyMessage = useCallback((text: string, index: number) => {
    const write = navigator.clipboard?.writeText(text);
    if (!write) return;
    void write.then(() => {
      setCopiedMessage(index);
      window.setTimeout(() => setCopiedMessage((current) => (current === index ? null : current)), 2000);
    });
  }, []);

  return (
    <>
      {items.map((it, i) => {
        if (it.kind === "user") {
          return (
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
            </div>
          );
        }
        if (it.kind === "assistant") {
          if (!it.text) return null;
          const metaParts: string[] = [];
          if (it.model) metaParts.push(it.model);
          if (typeof it.durationMs === "number") metaParts.push(formatDuration(it.durationMs));
          return (
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
            </div>
          );
        }
        if (it.kind === "tool") {
          return (
          <ToolCard key={i} tool={{ id: it.id || String(i), name: it.name, arguments: it.args }} result={it.result} done={it.done} />
          );
        }
        if (it.kind === "approval") {
          const label =
            it.state === "approved"
              ? "已允许"
              : it.state === "denied"
                ? "已拒绝"
                : it.state === "expired"
                  ? "已过期"
                  : "等待批准";
          return (
            <div key={i} className={`approval-line ${it.state}`}>
              <span className="approval-line-dot" aria-hidden="true">!</span>
              <span className="approval-line-text">
                {label} · {it.name}
                {it.scope ? <code>{it.scope}</code> : null}
              </span>
            </div>
          );
        }
        if (it.kind === "review") {
          return (
            <details key={i} className="review-card">
              <summary>自动审查与变更记录</summary>
              <pre>{it.text}</pre>
            </details>
          );
        }
        if (it.kind === "notice") {
          return (
            <div key={i} className="msg notice">
              {it.text}
            </div>
          );
        }
        return (
          <div key={i} className="msg error">
            {it.text}
          </div>
        );
      })}
    </>
  );
}
