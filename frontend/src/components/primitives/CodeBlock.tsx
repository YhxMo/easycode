import { useState } from "react";
import { DiffView } from "./DiffView";
import { NumberedLines } from "./NumberedLines";

const DIFF_START = /^(?:diff --git|--- |\+\+\+ |@@ )/m;

/** Does this block read as a patch rather than as source? */
function looksLikeDiff(code: string, lang?: string): boolean {
  if (lang === "diff" || lang === "patch") return true;
  return DIFF_START.test(code);
}

interface Props {
  code: string;
  lang?: string;
  /** Shown as the diff's file identity when the block is a patch. */
  path?: string;
}

/** Fenced code with line numbers, copy, and a Code/Diff switch for patches. */
export function CodeBlock({ code, lang, path }: Props) {
  const isDiff = looksLikeDiff(code, lang);
  const [diffMode, setDiffMode] = useState(isDiff);
  const [copied, setCopied] = useState(false);

  const copy = () => {
    const write = navigator.clipboard?.writeText(code);
    if (!write) return;
    void write.then(() => {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1600);
    });
  };

  return (
    <div className={`code-block${diffMode ? " is-diff" : ""}`}>
      <div className="code-head">
        <span className="code-lang">{lang || (isDiff ? "diff" : "text")}</span>
        <div className="code-actions">
          {isDiff && (
            <div className="code-switch" role="group" aria-label="显示方式">
              <button
                type="button"
                className={diffMode ? "" : "on"}
                aria-pressed={!diffMode}
                onClick={() => setDiffMode(false)}
              >
                Code
              </button>
              <button
                type="button"
                className={diffMode ? "on" : ""}
                aria-pressed={diffMode}
                onClick={() => setDiffMode(true)}
              >
                Diff
              </button>
            </div>
          )}
          <button
            type="button"
            className="code-copy"
            aria-label={copied ? "已复制" : "复制代码"}
            title={copied ? "已复制" : "复制代码"}
            onClick={copy}
          >
            {copied ? "已复制" : "复制"}
          </button>
        </div>
      </div>
      {diffMode && isDiff ? (
        <DiffView diff={code} path={path} />
      ) : (
        <pre className="code-body">
          <NumberedLines lines={code.replace(/\n$/, "").split("\n")} />
        </pre>
      )}
    </div>
  );
}
