import type { FileEntry } from "./api";

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
  files,
  open,
  query,
  index,
  total,
  onPick,
  onClose,
}: {
  files: FileEntry[];
  open: boolean;
  query: string;
  index: number;
  total: number;
  onPick: (file: FileEntry) => void;
  onClose: () => void;
}) {
  if (!open) return null;
  const active = files.length ? Math.min(index, files.length - 1) : -1;

  return (
    <div className="command-menu">
      <div className="command-menu-scroll">
        <div className="command-section">
          <div className="command-section-title">
            文件
            {total > files.length && <span className="mention-more">显示前 {files.length} 个</span>}
          </div>
          <div className="command-section-items">
            {files.map((file, i) => (
              <div
                key={`${file.root}/${file.path}`}
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
              </div>
            ))}
            {files.length === 0 && (
              <div className="command-empty">
                {query ? `没有匹配「${query}」的文件` : "工作区中没有可引用的文件"}
              </div>
            )}
          </div>
        </div>
      </div>
      <div className="command-scrim" onClick={onClose} />
    </div>
  );
}
