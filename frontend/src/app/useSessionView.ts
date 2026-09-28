import { useCallback, useState } from "react";
import type { RefObject } from "react";
import type { ToolItem } from "../types";
import type { SessionLoad } from "./useSessionLifecycle";

/**
 * The conversation in front, and everything that describes it.
 *
 * Separate from the session list (which the server owns) because none of this
 * is stored: which conversation is on screen, which directory a new one would
 * run in, and what has been loaded for it so far. The lifecycle hook writes
 * these; the view reads them.
 */
export function useSessionView(viewTokenRef: RefObject<number>) {
  const [currentId, setCurrentId] = useState<string | null>(null);
  // One pair of states serves both the new-session draft and the open session:
  // a session event updates them in place, and openSession resets them.
  const [chosenRoot, setChosenRoot] = useState<string | null>(null);
  const [secondary, setSecondary] = useState<string[]>([]);
  // Foreground session load state: while loading (or after a failed load) the
  // composer, permission picker and secondary editor are disabled, so no
  // request can be sent against a target whose state is not confirmed yet.
  const [sessionLoad, setSessionLoad] = useState<SessionLoad | null>(null);
  // File-tool records per conversation, as fetched with the session. They are
  // what the pane reads once a refresh (or a compaction) has taken the tool
  // results out of the message history.
  const [artifactRecords, setArtifactRecords] = useState<Record<string, ToolItem[]>>({});
  /** Bumped whenever the extensions dialog installs or saves something: the `/`
   *  menu reads its sources again, so a new skill is selectable without a reload. */
  const [extensionsRevision, setExtensionsRevision] = useState(0);

  // Choosing a directory is a view move even for a draft that is not stored yet,
  // so it invalidates replies still on their way from the previous one.
  const chooseRoot = useCallback(
    (root: string | null) => {
      ++viewTokenRef.current;
      setChosenRoot(root);
    },
    [viewTokenRef],
  );

  return {
    currentId,
    setCurrentId,
    chosenRoot,
    chooseRoot,
    secondary,
    setSecondary,
    sessionLoad,
    setSessionLoad,
    artifactRecords,
    setArtifactRecords,
    extensionsRevision,
    setExtensionsRevision,
  };
}
