import { useCallback, useMemo, useRef, useState } from "react";
import { fetchArchivedSessions, fetchSessions, fetchWorkspaces } from "../api";
import type { SessionSummary, WorkspaceProject, WorkspacesInfo } from "../api";

export interface SessionListInputs {
  /** Newest list loaded: the ids the server still lists. */
  onListLoaded: (known: Set<string>) => void;
  /** Archived conversations loaded: they left the main list on purpose. */
  onArchivedLoaded: (gone: Set<string>) => void;
}

/**
 * What the sidebar lists and what the current conversation is bound to.
 *
 * Two reads answer two different questions — the main list (with the projects
 * behind it) and the archived one — and both hand their ids to the tab list, so
 * a conversation the server no longer lists cannot keep a tab open. Neither
 * touches the tabs itself; that is the caller's rule to state.
 */
export function useSessionList({ onListLoaded, onArchivedLoaded }: SessionListInputs) {
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [archived, setArchived] = useState<SessionSummary[]>([]);
  const [workspaces, setWorkspaces] = useState<WorkspacesInfo>({ projects: [] });
  const [workspacesLoaded, setWorkspacesLoaded] = useState(false);
  // Newest session-list request: an out-of-order reply must not prune tabs the
  // list it lost to still has.
  const seqRef = useRef(0);

  const sessionById = useMemo(() => new Map(sessions.map((s) => [s.id, s])), [sessions]);

  const refresh = useCallback(() => {
    const seq = ++seqRef.current;
    fetchSessions()
      .then((list) => {
        if (seq !== seqRef.current) return;
        setSessions(list);
        onListLoaded(new Set(list.map((s) => s.id)));
      })
      .catch(() => {});
    fetchWorkspaces()
      .then(setWorkspaces)
      .catch(() => {})
      .finally(() => setWorkspacesLoaded(true));
  }, [onListLoaded]);

  const refreshArchived = useCallback(() => {
    fetchArchivedSessions()
      .then((list) => {
        setArchived(list);
        onArchivedLoaded(new Set(list.map((s) => s.id)));
      })
      .catch(() => {});
  }, [onArchivedLoaded]);

  const setProjects = useCallback(
    (projects: WorkspaceProject[]) => setWorkspaces((w) => ({ ...w, projects })),
    [],
  );

  return {
    sessions,
    setSessions,
    archived,
    setArchived,
    workspaces,
    setWorkspaces,
    workspacesLoaded,
    setProjects,
    sessionById,
    refresh,
    refreshArchived,
  };
}
