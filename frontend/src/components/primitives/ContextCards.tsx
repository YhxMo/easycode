import type { ContextCard } from "../../lib/pane";

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
  onOpen: (source: string) => void;
}

/** The context this turn pulled in, each card linking to its file preview. */
export function ContextCards({ cards, onOpen }: Props) {
  if (!cards.length) {
    return <p className="pane-empty">这一轮还没有读取文件或搜索内容。</p>;
  }
  return (
    <div className="ctx-cards">
      <div className="pane-group-title">
        全部片段
        <span className="pane-count">{cards.length}</span>
      </div>
      {cards.map((card) => (
        <article key={card.id} className="ctx-card">
          <header className="ctx-head">
            <span className="ctx-title">{card.title}</span>
            <span className="ctx-meta tabular">{card.meta}</span>
          </header>
          {card.excerpt && <p className="ctx-body">{card.excerpt}</p>}
          <button
            type="button"
            className="ctx-source"
            title={`预览 ${card.source}`}
            onClick={() => onOpen(card.source)}
          >
            {card.kind === "search" ? <SearchIcon /> : <SourceIcon />}
            <span className="ctx-source-path">{card.source}</span>
            <span className="ctx-arrow" aria-hidden="true">
              ↗
            </span>
          </button>
        </article>
      ))}
    </div>
  );
}
