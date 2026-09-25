// Composer field sizing: the box grows with its content up to a cap, then
// scrolls inside instead.
import { useCallback, useEffect, useRef } from "react";
import type { RefObject } from "react";

/** Caps shared with `.composer-field` in the stylesheet. */
export const FIELD_MAX_HEIGHT = 220;
export const FIELD_MIN_HEIGHT = 44;

export function useAutoGrow(
  fieldRef: RefObject<HTMLTextAreaElement | null> | undefined,
  value: string,
  minHeight: number = FIELD_MIN_HEIGHT,
) {
  const measure = useCallback(() => {
    const field = fieldRef?.current;
    if (!field) return;
    // Measure from the content: clearing the height first lets the box shrink
    // again when text is deleted instead of only ever growing.
    field.style.setProperty("height", "auto");
    const content = field.scrollHeight;
    field.style.setProperty(
      "height",
      `${Math.min(FIELD_MAX_HEIGHT, Math.max(minHeight, content))}px`,
    );
    field.style.setProperty("overflow-y", content > FIELD_MAX_HEIGHT ? "auto" : "hidden");
  }, [fieldRef, minHeight]);

  // Typing, pasting, quoting a selection, restoring a draft.
  useEffect(measure, [measure, value]);

  // Width changes rewrap the text, so the same content needs a different
  // height. Only width is a trigger — reacting to our own height changes would
  // loop.
  const width = useRef<number | null>(null);
  useEffect(() => {
    const field = fieldRef?.current;
    if (!field || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver((entries) => {
      const next = entries[0]?.contentRect.width;
      if (next === undefined || next === width.current) return;
      width.current = next;
      measure();
    });
    observer.observe(field);
    return () => observer.disconnect();
  }, [fieldRef, measure]);
}
