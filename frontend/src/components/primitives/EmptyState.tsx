import { useState, type ReactNode } from "react";

/** First-run prompts: real coding tasks for the current workspace. */
const SUGGESTIONS = [
  { icon: "fix", text: "修复当前项目的失败测试" },
  { icon: "tree", text: "解释这个仓库的目录结构" },
  { icon: "refactor", text: "找出最需要重构的模块" },
  { icon: "test", text: "为最近改动的模块补充单元测试" },
  { icon: "trace", text: "梳理一个功能的完整调用链" },
  { icon: "search", text: "定位最近一次报错的根因" },
  { icon: "docs", text: "找出缺少说明的公开接口" },
  { icon: "review", text: "检查未提交改动的边界情况" },
];

const VISIBLE = 3;

function SuggestionIcon({ name }: { name: string }) {
  const paths: Record<string, string> = {
    fix: "M12 4.5a7.5 7.5 0 1 0 0 15 7.5 7.5 0 0 0 0-15ZM12 8.5v4.5M12 16h.01",
    tree: "M5 4h5.5v5H5zM13.5 15h5.5v5h-5.5zM7.75 9v6a2 2 0 0 0 2 2h3.75",
    refactor: "M4.5 4.5H10V10H4.5zM14 4.5h5.5V10H14zM4.5 14H10v5.5H4.5zM14 14h5.5v5.5H14z",
    test: "M9.5 3.5h5M10.5 3.5v6l-4 7.4A1.6 1.6 0 0 0 7.9 19h8.2a1.6 1.6 0 0 0 1.4-2.1l-4-7.4v-6",
    trace: "M5 5.5h6v4H5zM13 14.5h6v4h-6zM8 9.5v5a2 2 0 0 0 2 2h3",
    search: "M11 5a6 6 0 1 0 0 12 6 6 0 0 0 0-12ZM15.4 15.4 19.5 19.5",
    docs: "M6.5 3.5h7l4 4v13h-11zM13.5 3.5V8H17.5M9.5 12.5h5M9.5 16h5",
    review: "M4.5 12.5 9 17 19.5 6.5",
  };
  return (
    <svg className="hint-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <path d={paths[name] ?? paths.docs} />
    </svg>
  );
}

interface Props {
  onPick: (text: string) => void;
  /**
   * Where the next conversation will run. When a workspace is already in play
   * the note names it instead of asking for one that is already chosen.
   */
  context?: string | null;
  children?: ReactNode;
}

export function EmptyState({ onPick, context, children }: Props) {
  const [offset, setOffset] = useState(0);
  const shown = Array.from({ length: VISIBLE }, (_, i) => SUGGESTIONS[(offset + i) % SUGGESTIONS.length]);

  return (
    <div className="empty-stage">
      <div className="empty-block">
        <p className="empty-greeting">你好，欢迎回来</p>
        <h1 className="empty-ask">想让我帮你做什么？</h1>
        {children}
        <ul className="hint-list">
          {shown.map((s) => (
            <li key={s.text}>
              <button type="button" className="hint-row" onClick={() => onPick(s.text)}>
                <SuggestionIcon name={s.icon} />
                <span>{s.text}</span>
              </button>
            </li>
          ))}
        </ul>
        <div className="hint-foot">
          <span className="hint-note">
            {context ? `本次会话的上下文：${context}` : "在左侧选择工作区，获得更准确的上下文"}
          </span>
          <button
            type="button"
            className="hint-shuffle"
            onClick={() => setOffset((o) => (o + VISIBLE) % SUGGESTIONS.length)}
          >
            <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
              <path d="M4 8h4l8 8h4M4 16h4l2-2M16 8l2-2h2M17.5 4.5 20 7l-2.5 2.5M17.5 14.5 20 17l-2.5 2.5" />
            </svg>
            换一批
          </button>
        </div>
      </div>
    </div>
  );
}
