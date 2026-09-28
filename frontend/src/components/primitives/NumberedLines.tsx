/** Lines of text with a line-number gutter; callers own the `<pre>` and any preprocessing. */
export function NumberedLines({ lines }: { lines: string[] }) {
  return lines.map((line, index) => (
    <span key={index} className="code-line">
      <span className="code-gutter" aria-hidden="true">
        {index + 1}
      </span>
      <span className="code-text">{line || " "}</span>
    </span>
  ));
}
