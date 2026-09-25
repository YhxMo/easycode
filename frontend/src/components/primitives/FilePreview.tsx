import { useEffect, useState } from "react";
import { fetchFileContent } from "../../api";

type State =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; path: string; text: string; total: number; truncated: boolean };

interface Props {
  sessionId: string;
  path: string;
  onBack: () => void;
}

/** A read-only file view with line numbers, for following a context card. */
export function FilePreview({ sessionId, path, onBack }: Props) {
  const [state, setState] = useState<State>({ status: "loading" });

  // A new path arrives as a new `key`, so the loading state is the initial
  // state rather than something an effect has to reset.
  useEffect(() => {
    let live = true;
    fetchFileContent(sessionId, path)
      .then(
        (r) =>
          live &&
          setState({
            status: "ready",
            path: r.path,
            text: r.text,
            total: r.total_lines,
            truncated: r.truncated,
          }),
      )
      .catch((e: unknown) =>
        live &&
        setState({ status: "error", message: e instanceof Error ? e.message : String(e) }),
      );
    return () => {
      live = false;
    };
  }, [sessionId, path]);

  return (
    <div className="file-preview">
      <header className="pane-group-title">
        <button type="button" className="pane-back" onClick={onBack}>
          ‹ 返回
        </button>
        <span className="file-preview-name" title={state.status === "ready" ? state.path : path}>
          {path}
        </span>
        {state.status === "ready" && (
          <span className="pane-count tabular">{state.total} 行</span>
        )}
      </header>
      {state.status === "loading" && <p className="pane-empty">正在读取…</p>}
      {state.status === "error" && <p className="pane-empty">无法预览：{state.message}</p>}
      {state.status === "ready" && (
        <>
          <pre className="code-body file-preview-body">
            {state.text.split("\n").map((line, index) => (
              <span key={index} className="code-line">
                <span className="code-gutter" aria-hidden="true">
                  {index + 1}
                </span>
                <span className="code-text">{line || " "}</span>
              </span>
            ))}
          </pre>
          {state.truncated && <p className="pane-empty">文件较大，仅显示开头部分。</p>}
        </>
      )}
    </div>
  );
}
