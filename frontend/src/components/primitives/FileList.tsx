import type { FileRow } from "../../lib/pane";

function FileIcon() {
  return (
    <svg className="ctx-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <path d="M6.5 3.5h7l4 4v13h-11zM13.5 3.5V8H17.5" />
    </svg>
  );
}

interface Props {
  rows: FileRow[];
  onOpen: (target: string) => void;
}

/**
 * The files a conversation used, and the ones its working tree changed.
 *
 * A row that names a real file opens it; one that does not (a read the session
 * was not allowed to preview, a file that was deleted) stays a plain row rather
 * than offering a link that can only be refused.
 */
export function FileList({ rows, onOpen }: Props) {
  if (!rows.length) return null;
  return (
    <div className="file-list">
      {rows.map((row) => {
        const body = (
          <>
            <FileIcon />
            <span className="ctx-source-path">
              {row.label}
              {row.meta && <span className="file-uses">{row.meta}</span>}
            </span>
            {row.git && <span className="file-git">{row.git}</span>}
            {row.rootLabel && <span className="ctx-root">{row.rootLabel}</span>}
            {row.target && (
              <span className="ctx-arrow" aria-hidden="true">
                ↗
              </span>
            )}
          </>
        );
        return row.target ? (
          <button
            type="button"
            key={row.key}
            className="ctx-source"
            title={`预览 ${row.label}`}
            onClick={() => onOpen(row.target as string)}
          >
            {body}
          </button>
        ) : (
          <div key={row.key} className="ctx-source static" title={row.label}>
            {body}
          </div>
        );
      })}
    </div>
  );
}
