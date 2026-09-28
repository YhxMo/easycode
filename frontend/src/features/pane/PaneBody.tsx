import { ChangeList } from "./ChangeList";
import { ContextCards } from "./ContextCards";
import { FileList } from "./FileList";
import { FilePreview } from "./FilePreview";
import { TaskRows } from "./TaskRows";
import type { usePane } from "./usePane";

/**
 * What the open pane shows, one section at a time. Presentational: the section,
 * the preview and the working tree all come from :func:`usePane`, so nothing
 * here decides when to read or fetch.
 */
export function PaneBody({
  pane,
  draftKey,
  sessionId,
}: {
  pane: ReturnType<typeof usePane>;
  draftKey: string;
  sessionId: string | null;
}) {
  const {
    section,
    todos,
    listIsStale,
    contextCards,
    openFile,
    changesOpen,
    toggleChanges,
    treeKnown,
    changeFiles,
    changeStats,
    changeRows,
    git,
    treeRows,
    loadGitDiff,
    gitStatsVersion,
    preview,
    setPreview,
    paneFiles,
  } = pane;

  return (
    <>
      {section === "tasks" && <TaskRows todos={todos} stale={listIsStale} />}
      {section === "context" && <ContextCards cards={contextCards} onOpen={openFile} />}
      {section === "changes" && (
        <>
          {/* The section opens as one line: what moved, and by how much. The
              files and their diffs are what the reader opens next. */}
          <button
            type="button"
            className={`change-summary${changesOpen ? " open" : ""}`}
            aria-expanded={changesOpen}
            title={
              treeKnown ? "当前工作区相对各仓库 HEAD 的未提交改动" : "这场会话自己修改的文件"
            }
            onClick={toggleChanges}
          >
            <span className="change-caret" aria-hidden="true">
              ▸
            </span>
            <span className="change-summary-label">
              {treeKnown ? "当前工作区改动" : "会话改动"}
            </span>
            <span className="change-summary-files">{changeFiles} 个文件</span>
            <span className="change-stats tabular">
              <span className="add">+{changeStats.added}</span>
              <span className="del">−{changeStats.removed}</span>
            </span>
          </button>
          {changesOpen && (
            <>
              <div className="pane-group-title">
                会话操作记录
                <span className="pane-count">{changeRows.length}</span>
              </div>
              {/* Keyed by conversation: which file a reader opened is about
                  that conversation, and the same path in another one is not
                  the same file. */}
              {changeRows.length ? (
                <ChangeList key={`session:${draftKey}`} rows={changeRows} />
              ) : (
                <p className="pane-empty">这场会话还没有修改文件。</p>
              )}
              <div className="pane-group-title pane-group-next">
                当前工作区未提交改动
                <span className="pane-count">{git?.files.length ?? 0}</span>
              </div>
              {!git && (
                <p className="pane-empty">
                  {sessionId ? "正在读取工作区状态…" : "开始会话后可查看工作区状态。"}
                </p>
              )}
              {git?.error && <p className="pane-empty">无法读取 Git 状态：{git.error}</p>}
              {git && !git.error && !git.repos.length && (
                <p className="pane-empty">会话所在目录不在 Git 仓库中。</p>
              )}
              {git && !git.error && git.repos.length > 0 && !git.files.length && (
                <p className="pane-empty">工作区没有未提交的改动。</p>
              )}
              <ChangeList
                key={`tree:${draftKey}`}
                rows={treeRows}
                loadDiff={loadGitDiff}
                statsVersion={gitStatsVersion}
              />
              {git?.truncated && treeRows.length > 0 && (
                <p className="pane-empty">改动较多，只列出前 {treeRows.length} 个文件。</p>
              )}
            </>
          )}
        </>
      )}
      {section === "file" &&
        (preview && sessionId ? (
          // The key carries the conversation as well as the path: the same
          // relative name in another session must never show the old text.
          <FilePreview
            key={`${sessionId}:${preview}`}
            sessionId={sessionId}
            path={preview}
            onBack={() => setPreview(null)}
          />
        ) : paneFiles.length ? (
          <>
            <div className="pane-group-title">
              会话涉及的文件
              <span className="pane-count">{paneFiles.length}</span>
            </div>
            <FileList rows={paneFiles} onOpen={openFile} />
          </>
        ) : (
          <p className="pane-empty">这场会话还没有涉及文件。</p>
        ))}
    </>
  );
}
