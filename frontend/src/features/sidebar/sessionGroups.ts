// Sidebar section derivation: which conversation belongs where.
import type { SessionSummary, WorkspaceProject } from "../../api";
import { basename } from "../../lib/paths";

export interface ProjectGroup {
  root: string;
  name: string;
  pinned: boolean;
  /** Every member: ordering, running state and the project's own actions read
   *  this list, so pinned members still count as belonging here. */
  sessions: SessionSummary[];
  /** What the group actually lists: pinned members are shown in their own
   *  section instead of a second time under their project. */
  visibleSessions: SessionSummary[];
}

export interface SessionGroups {
  /** Pinned conversations, flat and newest-pinned first. */
  pinned: SessionSummary[];
  /** Every known working directory, pinned ones first, each with its own conversations. */
  projects: ProjectGroup[];
  /** Conversations without a working directory. */
  recents: SessionSummary[];
}

const created = (s: SessionSummary) => Date.parse(s.created_at);

export function groupSessions(
  sessions: SessionSummary[],
  projects: WorkspaceProject[],
): SessionGroups {
  const byRoot = new Map<string, SessionSummary[]>();
  const recents: SessionSummary[] = [];
  const pinned: SessionSummary[] = [];
  for (const s of sessions) {
    // A pinned conversation is listed in the pinned section instead of the
    // project or recents list it came from, but a project keeps it as a member
    // either way: the group's own ordering, running state and actions count it.
    if (s.pinned) pinned.push(s);
    if (s.root) {
      const list = byRoot.get(s.root);
      if (list) list.push(s);
      else byRoot.set(s.root, [s]);
    } else if (!s.pinned) {
      recents.push(s);
    }
  }

  const configured = new Map<string, WorkspaceProject>();
  for (const p of projects) if (p.root) configured.set(p.root, p);
  // Configured projects outlive their conversations: a project with none still
  // belongs in the list, and a root only known from sessions still shows up.
  const roots = new Set<string>([...configured.keys(), ...byRoot.keys()]);
  const groups: ProjectGroup[] = [...roots].map((root) => {
    const project = configured.get(root);
    const sessions = [...(byRoot.get(root) ?? [])].sort((a, b) => created(b) - created(a));
    return {
      root,
      name: project?.name || basename(root),
      pinned: project?.pinned ?? false,
      sessions,
      visibleSessions: sessions.filter((s) => !s.pinned),
    };
  });

  const recency = (g: ProjectGroup) => (g.sessions.length ? created(g.sessions[0]) : 0);
  groups.sort((a, b) => {
    if (a.pinned !== b.pinned) return a.pinned ? -1 : 1;
    return recency(b) - recency(a);
  });

  pinned.sort((a, b) => (b.pinned_at ?? b.created_at).localeCompare(a.pinned_at ?? a.created_at));
  recents.sort((a, b) => created(b) - created(a));

  return { pinned, projects: groups, recents };
}
