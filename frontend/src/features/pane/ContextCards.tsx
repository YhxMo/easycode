import { useState } from "react";
import type { ContextCard, ContextHit } from "./pane";
import { MAX_HITS } from "./pane";

function SourceIcon() {
  return (
    <svg className="ctx-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <path d="M6.5 3.5h7l4 4v13h-11zM13.5 3.5V8H17.5" />
    </svg>
  );
}

function SearchIcon() {
  return (
    <svg className="ctx-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <path d="M11 5a6 6 0 1 0 0 12 6 6 0 0 0 0-12ZM15.4 15.4 19.5 19.5" />
    </svg>
  );
}

interface Props {
  cards: ContextCard[];
  onOpen: (target: string) => void;
}

/**
 * The files one card found.
 *
 * A search can hit far more files than are worth listing by default, so the
 * rest stay one click away — the count is never the end of the list.
 */
function HitList({
  hits,
  kind,
  onOpen,
}: {
  hits: ContextHit[];
  kind: ContextCard["kind"];
  onOpen: (target: string) => void;
}) {
  const [all, setAll] = useState(false);
  const shown = all ? hits : hits.slice(0, MAX_HITS);
  return (
    <div className="ctx-hits">
      {shown.map((hit) => (
        <button
          type="button"
          key={hit.target}
          className="ctx-source"
          title={`预览 ${hit.label}`}
          onClick={() => onOpen(hit.target)}
        >
          {kind === "search" ? <SearchIcon /> : <SourceIcon />}
          <span className="ctx-source-path">
            {hit.label}
            {hit.line ? `:${hit.line}` : ""}
          </span>
          {hit.rootLabel && <span className="ctx-root">{hit.rootLabel}</span>}
          <span className="ctx-arrow" aria-hidden="true">
            ↗
          </span>
        </button>
      ))}
      {hits.length > MAX_HITS && (
        <button
          type="button"
          className="ctx-source ctx-more"
          aria-expanded={all}
          onClick={() => setAll(!all)}
        >
          {all ? "收起" : `还有 ${hits.length - MAX_HITS} 个 · 展开全部`}
        </button>
      )}
    </div>
  );
}

/**
 * The context this conversation pulled in.
 *
 * A file card links to its own preview; a search card lists the files it hit.
 * The pattern itself is a label, never a link — it names a query, not a file.
 */
export function ContextCards({ cards, onOpen }: Props) {
  if (!cards.length) {
    return <p className="pane-empty">这场会话还没有读取文件或搜索内容。</p>;
  }
  return (
    <div className="ctx-cards">
      <div className="pane-group-title">
        全部片段
        <span className="pane-count">{cards.length}</span>
      </div>
      {cards.map((card) => {
        const hits = card.hits ?? [];
        return (
          <article key={card.id} className={`ctx-card ${card.status}`}>
            <header className="ctx-head">
              <span className="ctx-title">{card.title}</span>
              {card.rootLabel && <span className="ctx-root">{card.rootLabel}</span>}
              <span className="ctx-meta tabular">{card.meta}</span>
            </header>
            {card.kind === "search" && card.sourceLabel && (
              <p className="ctx-query" title={card.sourceLabel}>
                {card.sourceLabel}
              </p>
            )}
            {card.excerpt && <p className="ctx-body">{card.excerpt}</p>}
            {card.status === "empty" && <p className="ctx-body muted">未找到匹配</p>}
            {hits.length > 0 && <HitList hits={hits} kind={card.kind} onOpen={onOpen} />}
            {!hits.length && card.previewTarget && (
              <button
                type="button"
                className="ctx-source"
                title={`预览 ${card.sourceLabel}`}
                onClick={() => onOpen(card.previewTarget as string)}
              >
                <SourceIcon />
                <span className="ctx-source-path">{card.sourceLabel}</span>
                <span className="ctx-arrow" aria-hidden="true">
                  ↗
                </span>
              </button>
            )}
            {/* A link that can only be refused is worse than none: the excerpt
                above is what this turn actually read. */}
            {!hits.length && card.previewBlocked && (
              <p className="ctx-blocked">{card.previewBlocked}</p>
            )}
          </article>
        );
      })}
    </div>
  );
}
