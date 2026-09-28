import { useCallback, useMemo } from "react";
import type { SessionSummary, WorkspaceProject, WorkspacesInfo } from "../api";
import { groupSessions } from "../features/sidebar/sessionGroups";
import { basename, DEFAULT_PROJECT } from "../lib/paths";

/**
 * What the registered projects mean to the rest of the view.
 *
 * All of it is derived from the workspace list the server returned: a lookup by
 * root, the name to show for a directory, the sidebar's sections, and a
 * signature that changes whenever the set of roots does (the `/` menu re-reads
 * its sources on that).
 */
export function useProjectLabels(workspaces: WorkspacesInfo, sessions: SessionSummary[]) {
  const projects = workspaces.projects;
  const projectMeta = useMemo(() => {
    const map = new Map<string | null, WorkspaceProject>();
    for (const p of projects ?? []) map.set(p.root ?? null, p);
    return map;
  }, [projects]);

  /**
   * What to call a directory: its configured project name, else the folder's.
   * A project's own name comes first — the directory it points at is what the
   * path says.
   */
  const projectLabel = useCallback(
    (root: string | null) =>
      projectMeta.get(root)?.name ||
      (root ? basename(root) : workspaces.default ? basename(workspaces.default) : DEFAULT_PROJECT),
    [projectMeta, workspaces.default],
  );

  // Sidebar sections: directories, pinned conversations, projects, recents.
  const groups = useMemo(() => groupSessions(sessions, projects ?? []), [sessions, projects]);

  const signature = (projects ?? []).map((p) => p.root ?? "").join("\u0000");

  return { projectMeta, projectLabel, groups, signature };
}
