// Scroll ownership for the message pane: follow the live edge only while the
// reader is at it, and remember where each conversation was left.
import { useCallback, useEffect, useRef, useState } from "react";
import type { RefObject } from "react";

/** Distance from the bottom (px) that still counts as following the stream. */
export const FOLLOW_THRESHOLD = 80;

interface Place {
  scrollTop: number;
  atBottom: boolean;
}

export interface StickToBottom {
  /** Content arrived above the fold while the reader was looking elsewhere. */
  unread: boolean;
  /** Return to the newest message and follow again. */
  stick: () => void;
  /** Bring one element in the pane into view (a deliberate jump). */
  reveal: (selector: string) => void;
}

/**
 * `content` is any value that changes when the stream grows (the item list).
 * `key` names the conversation: leaving one keeps its place, arriving at one
 * restores it, and a first visit starts at the newest message.
 */
export function useStickToBottom(
  paneRef: RefObject<HTMLElement | null>,
  key: string,
  content: unknown,
): StickToBottom {
  const [unread, setUnread] = useState(false);
  const following = useRef(true);
  const currentKey = useRef(key);
  const places = useRef(new Map<string, Place>());
  const frame = useRef<number | null>(null);

  // Position immediately rather than smoothly: a scroll animation restarted on
  // every streamed token never settles, and streaming updates are coalesced
  // into one frame.
  const schedule = useCallback(
    (place: (pane: HTMLElement) => void) => {
      if (frame.current !== null) window.cancelAnimationFrame(frame.current);
      frame.current = window.requestAnimationFrame(() => {
        frame.current = null;
        const pane = paneRef.current;
        if (pane) place(pane);
      });
    },
    [paneRef],
  );

  // Re-checked at frame time: a scroll the reader made between scheduling and
  // the frame must win over a pin that was queued before it.
  const pinToBottom = useCallback(() => {
    schedule((pane) => {
      if (!following.current) return;
      pane.scrollTop = pane.scrollHeight;
    });
  }, [schedule]);

  useEffect(
    () => () => {
      if (frame.current !== null) window.cancelAnimationFrame(frame.current);
    },
    [],
  );

  // Follow state is the reader's: it is set by scrolling, never inferred from
  // the content.
  useEffect(() => {
    const pane = paneRef.current;
    if (!pane) return;
    const onScroll = () => {
      const distance = pane.scrollHeight - pane.scrollTop - pane.clientHeight;
      const next = distance <= FOLLOW_THRESHOLD;
      following.current = next;
      if (next) setUnread(false);
    };
    pane.addEventListener("scroll", onScroll, { passive: true });
    return () => pane.removeEventListener("scroll", onScroll);
  }, [paneRef]);

  useEffect(() => {
    const pane = paneRef.current;
    if (!pane) return;
    if (currentKey.current !== key) {
      places.current.set(currentKey.current, {
        scrollTop: pane.scrollTop,
        atBottom: following.current,
      });
      currentKey.current = key;
      const place = places.current.get(key);
      const follow = place ? place.atBottom : true;
      following.current = follow;
      setUnread(false);
      if (follow) pinToBottom();
      else if (place) schedule((p) => (p.scrollTop = place.scrollTop));
      return;
    }
    if (!following.current) {
      setUnread(true);
      return;
    }
    pinToBottom();
  }, [key, content, paneRef, schedule, pinToBottom]);

  // The pane itself resizes when the composer grows or the pane opens: a
  // followed view must keep its edge at the bottom through that.
  useEffect(() => {
    const pane = paneRef.current;
    if (!pane || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => {
      if (!following.current) return;
      pinToBottom();
    });
    observer.observe(pane);
    return () => observer.disconnect();
  }, [paneRef, pinToBottom]);

  const stick = useCallback(() => {
    following.current = true;
    setUnread(false);
    pinToBottom();
  }, [pinToBottom]);

  const reveal = useCallback(
    (selector: string) => {
      const pane = paneRef.current;
      const target = pane?.querySelector<HTMLElement>(selector);
      if (!pane || !target) {
        stick();
        return;
      }
      // An explicit jump names its destination: put the element in the upper
      // third instead of assuming it is the last thing in the stream.
      const offset =
        pane.scrollTop + target.getBoundingClientRect().top - pane.getBoundingClientRect().top - 24;
      const top = Math.max(0, offset);
      following.current = pane.scrollHeight - top - pane.clientHeight <= FOLLOW_THRESHOLD;
      if (following.current) setUnread(false);
      schedule((p) => (p.scrollTop = top));
    },
    [paneRef, schedule, stick],
  );

  return { unread, stick, reveal };
}
