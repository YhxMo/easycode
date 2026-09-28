import { useCallback, useState } from "react";
import { setSessionPermission } from "../api";

export interface PermissionInputs {
  sessionId: string | null;
  /** The foreground session is loading or failed: no request may change it. */
  sessionBlocked: boolean;
  /** The view token: a reply from a superseded view must not be applied. */
  viewToken: React.RefObject<number>;
  onError: (kind: "err", message: string) => void;
}

/**
 * The session's permission mode, as this view holds it.
 *
 * The server owns the mode; this is the confirmed copy, plus the "one request
 * at a time" guard and the gate in front of full access. `sendBlocked` is what
 * the rest of the view reads: while a change is in flight, sending is refused
 * so a turn can never run under a mode the server has not confirmed.
 */
export function usePermission({ sessionId, sessionBlocked, viewToken, onError }: PermissionInputs) {
  const [mode, setMode] = useState("ask");
  const [pending, setPending] = useState(false);
  // Full host access is a security boundary, so selecting it opens an explicit
  // risk confirmation before the server-side mode changes.
  const [confirming, setConfirming] = useState(false);
  const sendBlocked = sessionBlocked || pending;

  const change = useCallback(
    async (next: string, confirmFullAccess = false) => {
      // One permission request at a time: the picker is disabled while pending,
      // so a second request can never race the first (no request token needed).
      if (sendBlocked) return;
      const id = sessionId;
      // The draft's mode is a local choice for the next send: update at once.
      if (id === null) {
        setMode(next);
        return;
      }
      const token = viewToken.current;
      // The view keeps the confirmed mode until the server answers; a failed
      // request therefore leaves the old value in place with no rollback.
      setPending(true);
      try {
        const r = await setSessionPermission(id, next, confirmFullAccess);
        if (viewToken.current !== token) return;
        setMode(r.permission_mode);
      } catch (e) {
        if (viewToken.current !== token) return;
        onError("err", `修改权限失败: ${e instanceof Error ? e.message : String(e)}`);
      } finally {
        // A response from a superseded view must not release a newer request.
        if (viewToken.current === token) setPending(false);
      }
    },
    [sessionId, onError, sendBlocked, viewToken],
  );

  const request = useCallback(
    (next: string) => {
      // Entering full access is the one change that removes every boundary, so
      // it goes through the risk dialog first; only that dialog's own button
      // states the consent to the server.
      if (next === "allow-all" && mode !== "allow-all") {
        setConfirming(true);
        return;
      }
      void change(next, next === "allow-all");
    },
    [change, mode],
  );

  /** Drop an in-flight change: it belonged to the view that started it. */
  const clearPending = useCallback(() => setPending(false), []);

  /** Adopt the mode a session reports, without asking the server for it. */
  const reset = useCallback((next: string) => {
    setMode(next);
    setPending(false);
  }, []);

  return {
    mode,
    pending,
    sendBlocked,
    confirming,
    setConfirming,
    change,
    request,
    reset,
    clearPending,
  };
}
