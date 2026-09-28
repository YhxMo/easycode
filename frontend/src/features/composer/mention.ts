// `@` file references in the composer.

export interface MentionToken {
  /** Index of the `@` that opened the token. */
  start: number;
  /** Index just past the token text: what a pick replaces. */
  end: number;
  /** Text typed after it (may be empty; the quotes are not included). */
  query: string;
}

/** A path is quoted when it contains whitespace, so the token stays one word. */
export function mentionRef(path: string): string {
  return /\s/.test(path) ? `@"${path}"` : `@${path}`;
}

/**
 * The `@` token the caret currently sits in, if any.
 *
 * A bare token opens at an `@` that starts a word and runs to the caret with no
 * whitespace between — so "see @src/ap" is a token while "a@b" and "@src/ap
 * done" are not. A quoted token (`@"…"`) may contain whitespace and runs to its
 * closing quote, or to the caret while it is still being typed.
 */
export function mentionToken(value: string, caret: number): MentionToken | null {
  const upto = value.slice(0, Math.max(0, Math.min(caret, value.length)));
  const at = upto.lastIndexOf("@");
  if (at === -1) return null;
  const before = at === 0 ? "" : upto[at - 1];
  if (before && !/\s/.test(before)) return null;
  const raw = upto.slice(at + 1);
  if (raw.startsWith('"')) {
    const close = raw.indexOf('"', 1);
    return close === -1
      ? { start: at, end: upto.length, query: raw.slice(1) }
      : { start: at, end: at + close + 2, query: raw.slice(1, close) };
  }
  if (/\s/.test(raw)) return null;
  return { start: at, end: upto.length, query: raw };
}

/**
 * Replace a token with the picked path, returning the caret position after it.
 *
 * The caret lands after a space so continuing to type never runs into the file
 * name. Picking a reference only inserts text: the backend does not turn `@`
 * into file contents.
 */
export function applyMention(
  value: string,
  token: MentionToken,
  path: string,
): { value: string; caret: number } {
  const rest = value.slice(token.end);
  const separator = rest.startsWith(" ") ? "" : " ";
  const ref = mentionRef(path);
  const next = `${value.slice(0, token.start)}${ref}${separator}${rest}`;
  return { value: next, caret: token.start + ref.length + separator.length };
}
