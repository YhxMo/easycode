// `@` file references in the composer.

export interface MentionToken {
  /** Index of the `@` that opened the token. */
  start: number;
  /** Text typed after it (may be empty). */
  query: string;
}

/**
 * The `@` token the caret currently sits in, if any.
 *
 * A token opens at `@` that starts a word and runs to the caret with no
 * whitespace between — so "see @src/ap" is a token while "a@b" and
 * "@src/ap done" are not.
 */
export function mentionToken(value: string, caret: number): MentionToken | null {
  const upto = value.slice(0, Math.max(0, Math.min(caret, value.length)));
  const at = upto.lastIndexOf("@");
  if (at === -1) return null;
  const before = at === 0 ? "" : upto[at - 1];
  if (before && !/\s/.test(before)) return null;
  const query = upto.slice(at + 1);
  if (/\s/.test(query)) return null;
  return { start: at, query };
}

/** Replace a token with the picked path, returning the caret position after it. */
export function applyMention(
  value: string,
  token: MentionToken,
  path: string,
): { value: string; caret: number } {
  const end = token.start + 1 + token.query.length;
  const rest = value.slice(end);
  // keep a single separator: the user may have typed on past the token already
  const separator = rest === "" || rest.startsWith(" ") ? "" : " ";
  const next = `${value.slice(0, token.start)}@${path}${separator}${rest}`;
  return { value: next, caret: token.start + path.length + 1 };
}
