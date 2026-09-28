import { useRef } from "react";
import type { FileEntry } from "../../api";
import { basename } from "../../lib/paths";
import { useRevealActive } from "../../lib/useRevealActive";

/** Id of the listbox the composer field points at with aria-controls. */
export const MENTION_LIST_ID = "composer-mention-list";
/** Prefix of each option's id, for the field's aria-activedescendant. */
export const MENTION_OPTION_PREFIX = "composer-mention-option-";

/**
 * A file query for one (scope, query) pair. The states are distinct on purpose:
 * "still looking" and "nothing matched" must not look the same, and a failure
 * must not read as an empty workspace. A token with nothing typed yet asks
 * instead of listing, so a bare `@` never dumps the whole tree at the reader.
 */
export type MentionState =
  | { status: "prompt"; scope: string; query: string }
  | { status: "loading"; scope: string; query: string }
  | { status: "error"; scope: string; query: string; message: string }
  | { status: "ready"; scope: string; query: string; files: FileEntry[]; total: number };

/** The answer for one (scope, query) pair, without the "still looking" case. */
export type MentionAnswer =
  | { status: "error"; message: string }
  | { status: "ready"; files: FileEntry[]; total: number };

function FileIcon() {
  return (
    <svg
      className="command-icon"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.7"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="M6.5 3.5h7l4 4v13h-11zM13.5 3.5V8H17.5" />
    </svg>
  );
}

export function MentionMenu({
  state,
  query,
  index,
  onPick,
  onClose,
  onRetry,
}: {
  /** The result for the token under the caret, or null while it is stale. */
  state: MentionState | null;
  query: string;
  index: number;
  onPick: (file: FileEntry) => void;
  onClose: () => void;
  onRetry: () => void;
}) {
  const boxRef = useRef<HTMLDivElement>(null);
  const files = state?.status === "ready" ? state.files : [];
  const active = files.length ? Math.min(index, files.length - 1) : -1;
  useRevealActive(boxRef, ".command-item.active", `${index}:${files.length}`);

  return (
    <div className="command-menu">
      <div className="command-menu-scroll" ref={boxRef} id={MENTION_LIST_ID} role="listbox" aria-label="工作区文件">
        <div className="command-section">
          <div className="command-section-title">
            文件
            {state?.status === "ready" && state.total > files.length && (
              <span className="mention-more">显示前 {files.length} 个</span>
            )}
          </div>
          <div className="command-section-items">
            {files.map((file, i) => (
              <button
                type="button"
                role="option"
                id={`${MENTION_OPTION_PREFIX}${i}`}
                aria-selected={i === active}
                key={file.absolute_path}
                className={`command-item ${i === active ? "active" : ""}`}
                onMouseDown={(e) => {
                  e.preventDefault();
                  onPick(file);
                }}
              >
                <div className="command-item-left">
                  <FileIcon />
                  <span className="command-name">{file.name}</span>
                  {file.dir && <span className="command-desc">{file.dir}</span>}
                </div>
                <div className="command-source" title={file.root}>
                  {basename(file.root)}
                </div>
              </button>
            ))}
            {state?.status === "prompt" && (
              <div className="command-empty">输入文件名搜索</div>
            )}
            {state?.status === "loading" && (
              <div className="command-empty" role="status">
                正在查找文件…
              </div>
            )}
            {state?.status === "error" && (
              <div className="command-empty error" role="alert">
                <span>查找文件失败：{state.message}</span>
                <button type="button" className="command-retry" onMouseDown={(e) => e.preventDefault()} onClick={onRetry}>
                  重试
                </button>
              </div>
            )}
            {state?.status === "ready" && files.length === 0 && (
              <div className="command-empty">没有匹配「{query}」的文件</div>
            )}
          </div>
        </div>
      </div>
      <div className="command-scrim" onClick={onClose} />
    </div>
  );
}
