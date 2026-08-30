import { memo } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

/**
 * Renders assistant message text as GitHub-flavored markdown.
 *
 * Streaming re-parses on every chunk, so the component is memoized on the
 * raw text and the parser is created once at module scope.
 */
const remarkPlugins = [remarkGfm];

export const Markdown = memo(function Markdown({ text }: { text: string }) {
  return (
    <div className="markdown-body">
      <ReactMarkdown remarkPlugins={remarkPlugins}>{text}</ReactMarkdown>
    </div>
  );
});
