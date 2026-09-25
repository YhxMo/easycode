import { isValidElement, memo, type ReactNode } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { CodeBlock } from "./primitives/CodeBlock";

/**
 * Renders assistant message text as GitHub-flavored markdown.
 *
 * Streaming re-parses on every chunk, so the component is memoized on the
 * raw text and the parser is created once at module scope.
 */
const remarkPlugins = [remarkGfm];

/** Fenced blocks go through CodeBlock (line numbers, copy, Code/Diff switch). */
function PreBlock({ children }: { children?: ReactNode }) {
  const child = Array.isArray(children) ? children[0] : children;
  if (!isValidElement(child)) return <pre>{children}</pre>;
  const props = child.props as { className?: string; children?: ReactNode };
  const lang = /language-([\w-]+)/.exec(props.className ?? "")?.[1];
  const code = String(props.children ?? "").replace(/\n$/, "");
  return <CodeBlock code={code} lang={lang} />;
}

const components = { pre: PreBlock };

export const Markdown = memo(function Markdown({ text }: { text: string }) {
  return (
    <div className="markdown-body">
      <ReactMarkdown remarkPlugins={remarkPlugins} components={components}>
        {text}
      </ReactMarkdown>
    </div>
  );
});
