import { useCallback, useState } from "react";
import {
  archiveProjectChats,
  createWorktree,
  pinProject,
  pinSession,
  revealInFinder,
  setSessionArchived,
} from "../api";
import type { SessionSummary, WorkspaceProject } from "../api";
import type { ExtensionTarget } from "../features/extensions/ExtensionsSettings";
import type { ProjectAction } from "../features/sidebar/ProjectMenu";
import { basename, DEFAULT_PROJECT } from "../lib/paths";

export interface ProjectActionsInputs {
  /** The registered projects, keyed by root (null is the default workspace). */
  projectMeta: Map<string | null, WorkspaceProject>;
  sessions: SessionSummary[];
  setProjects: (projects: WorkspaceProject[]) => void;
  setArchived: (update: (prev: SessionSummary[]) => SessionSummary[]) => void;
  refreshSessions: () => void;
  refreshArchived: () => void;
  /** The conversation on screen: only it may carry its id into the dialog. */
  currentSession: SessionSummary | undefined;
  currentId: string | null;
  createAndOpen: (root: string | null) => Promise<void>;
  /** Where the remove-project confirmation lives; it is opened, not answered, here. */
  setRemoveTarget: (target: { root: string | null; count: number } | null) => void;
  toast: (kind: "ok" | "err", text: string) => void;
}

/**
 * What the sidebar's project rows and conversation rows do.
 *
 * Every one of these names a project (or a conversation) taken from the row that
 * was clicked, never from whatever happens to be on screen: the sidebar and the
 * view move independently, and acting on the wrong one is invisible until the
 * damage is done.
 */
export function useProjectActions({
  projectMeta,
  sessions,
  setProjects,
  setArchived,
  refreshSessions,
  refreshArchived,
  currentSession,
  currentId,
  createAndOpen,
  setRemoveTarget,
  toast,
}: ProjectActionsInputs) {
  /** The project whose settings dialog is open, and the name it shows. */
  const [editTarget, setEditTarget] = useState<{ root: string | null; name: string } | null>(null);
  /** The project whose skills and servers the extensions dialog is editing. */
  const [extensionTarget, setExtensionTarget] = useState<ExtensionTarget | null>(null);
  const [showArchived, setShowArchived] = useState(false);

  const newChatInProject = useCallback(
    (root: string | null) => {
      // This row's project is the requested one, whatever is on screen now.
      void createAndOpen(root);
    },
    [createAndOpen],
  );

  const togglePin = useCallback(
    async (target: SessionSummary) => {
      try {
        await pinSession(target.id, !target.pinned);
        // The server owns the flag: re-read the list rather than guessing.
        refreshSessions();
      } catch (e) {
        toast("err", `置顶失败: ${e instanceof Error ? e.message : String(e)}`);
      }
    },
    [refreshSessions, toast],
  );

  const restoreArchived = useCallback(
    async (session: SessionSummary) => {
      try {
        await setSessionArchived(session.id, false);
      } catch (e) {
        // A failed restore leaves the entry exactly where it was, with the
        // reason on screen: it must not look like it moved and came back.
        toast("err", `恢复会话失败: ${e instanceof Error ? e.message : String(e)}`);
        return;
      }
      setArchived((prev) => prev.filter((x) => x.id !== session.id));
      refreshSessions();
    },
    [refreshSessions, setArchived, toast],
  );

  /**
   * Open the extensions dialog for one project.
   *
   * The target is fixed here, at the moment of the click: the dialog then
   * describes one project's skills and servers even if the view moves on. A
   * session is carried along only when it really runs in that project — its
   * connection status says nothing about another project's servers.
   */
  const openExtensions = useCallback(
    (initialTab: ExtensionTarget["initialTab"], root: string | null) => {
      // Read from the session list rather than the view's own root: a session
      // whose summary has not loaded yet must not be mistaken for a match.
      const sameProject = currentSession !== undefined && (currentSession.root ?? null) === root;
      setExtensionTarget({
        root,
        sessionId: sameProject && currentId ? currentId : null,
        initialTab,
      });
    },
    [currentId, currentSession],
  );

  const runProjectAction = useCallback(
    async (root: string | null, action: ProjectAction) => {
      try {
        if (action === "edit") {
          const proj = root ? projectMeta.get(root) : projectMeta.get(null);
          setEditTarget({ root, name: proj?.name ?? (root ? basename(root) : DEFAULT_PROJECT) });
        } else if (action === "extensions") {
          // The project whose row was acted on, not whatever is on screen.
          openExtensions("skills", root);
        } else if (action === "pin" || action === "unpin") {
          const r = await pinProject(root, action === "pin");
          setProjects(r.projects);
          refreshSessions();
          toast("ok", action === "pin" ? "已置顶" : "已取消置顶");
        } else if (action === "reveal") {
          const r = await revealInFinder(root);
          if (!r.ok) toast("err", r.error ?? "无法打开目录");
        } else if (action === "worktree") {
          if (!root) {
            toast("err", "默认项目没有目录");
            return;
          }
          const r = await createWorktree(root);
          setProjects(r.projects);
          refreshSessions();
          if (r.warnings?.length) {
            toast("err", `工作树已创建，但有提示：${r.warnings.join("；")}`);
          } else {
            toast("ok", `已创建永久工作树 ${r.name ?? ""}`);
          }
        } else if (action === "archive") {
          const r = await archiveProjectChats(root);
          refreshSessions();
          refreshArchived();
          toast("ok", `已归档 ${r.archived_sessions} 条聊天`);
        } else if (action === "remove") {
          setRemoveTarget({ root, count: sessions.filter((s) => (s.root ?? null) === root).length });
        }
      } catch (e) {
        toast("err", e instanceof Error ? e.message : String(e));
      }
    },
    [
      openExtensions,
      projectMeta,
      refreshSessions,
      refreshArchived,
      sessions,
      setProjects,
      setRemoveTarget,
      toast,
    ],
  );

  return {
    editTarget,
    setEditTarget,
    extensionTarget,
    setExtensionTarget,
    showArchived,
    toggleArchived: () => setShowArchived((v) => !v),
    newChatInProject,
    togglePin,
    restoreArchived,
    openExtensions,
    runProjectAction,
  };
}
