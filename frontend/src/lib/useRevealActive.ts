// Keep a menu's keyboard highlight inside that menu's own scroll box.
import { useEffect } from "react";
import type { RefObject } from "react";

export function useRevealActive(
  containerRef: RefObject<HTMLElement | null>,
  activeSelector: string,
  signal: unknown,
) {
  useEffect(() => {
    const box = containerRef.current;
    const active = box?.querySelector<HTMLElement>(activeSelector);
    if (!box || !active) return;
    // Move this box only: scrollIntoView would also walk up to the page
    // scroller, which is not what a keyboard highlight should do.
    const boxRect = box.getBoundingClientRect();
    const itemRect = active.getBoundingClientRect();
    if (itemRect.top < boxRect.top) box.scrollTop -= boxRect.top - itemRect.top;
    else if (itemRect.bottom > boxRect.bottom) box.scrollTop += itemRect.bottom - boxRect.bottom;
  }, [containerRef, activeSelector, signal]);
}
