import { useCallback, useEffect, useRef, useState } from "react";
import { NARROW_CHAT_PX } from "./constants";

/**
 * What the chat column measures about itself.
 *
 * Two readings of the same element tree, both taken with a ResizeObserver: the
 * column's width decides whether the pane shares the row or covers the stream,
 * and the floating composer's height is the room the stream keeps clear at its
 * bottom so a grown input never hides the last reply.
 */
export function useChatLayout() {
  // Measured from the element the CSS container query measures, so the two
  // agree on when the pane turns into a drawer.
  const chatRef = useRef<HTMLElement>(null);
  const [narrow, setNarrow] = useState(false);
  useEffect(() => {
    const el = chatRef.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const measure = () => setNarrow(el.clientWidth < NARROW_CHAT_PX);
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  const [composerHeight, setComposerHeight] = useState(0);
  /** Ref callback for the docked composer's container. */
  const attachComposer = useCallback((el: HTMLDivElement | null) => {
    if (!el || typeof ResizeObserver === "undefined") return;
    const publish = () => setComposerHeight(el.offsetHeight);
    const observer = new ResizeObserver(publish);
    observer.observe(el);
    publish();
    return () => observer.disconnect();
  }, []);

  return { chatRef, narrow, composerHeight, attachComposer };
}
