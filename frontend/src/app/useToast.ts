import { useCallback, useEffect, useRef, useState } from "react";

/** A transient message at the top of the shell. */
export interface Toast {
  kind: "ok" | "err";
  text: string;
}

const TOAST_MS = 3500;

/**
 * One message at a time, replaced rather than queued: each call restarts the
 * timer, so a burst of project actions shows only what happened last.
 */
export function useToast(): [Toast | null, (kind: "ok" | "err", text: string) => void] {
  const [toast, setToast] = useState<Toast | null>(null);
  const timer = useRef<number | null>(null);

  useEffect(
    () => () => {
      if (timer.current) window.clearTimeout(timer.current);
    },
    [],
  );

  const show = useCallback((kind: "ok" | "err", text: string) => {
    setToast({ kind, text });
    if (timer.current) window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => setToast(null), TOAST_MS);
  }, []);

  return [toast, show];
}
