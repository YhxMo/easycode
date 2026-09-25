// Shared reading of one tool result payload.
//
// A tool result is a JSON object for our own tools, but MCP servers may return
// plain text, and a truncated stream can leave invalid JSON. Callers always
// keep the raw text for fallback rendering; this only answers "what object, if
// any, can be read out of it".

/** The result object when the text is a JSON object, else null. */
export function parseResult(raw: string | undefined): Record<string, unknown> | null {
  if (!raw) return null;
  try {
    const value: unknown = JSON.parse(raw);
    if (typeof value !== "object" || value === null || Array.isArray(value)) return null;
    return value as Record<string, unknown>;
  } catch {
    return null;
  }
}

export const str = (value: unknown): string => (typeof value === "string" ? value : "");

export const num = (value: unknown): number =>
  typeof value === "number" && Number.isFinite(value) ? value : 0;

export type ResultStatus = "ok" | "error" | "unknown";

/** The status a result declares, for deciding how a card should read. */
export function resultStatus(data: Record<string, unknown> | null): ResultStatus {
  const status = data?.status;
  return status === "ok" || status === "error" ? status : "unknown";
}
