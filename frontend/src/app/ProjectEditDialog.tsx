import { useState } from "react";
import { saveProject } from "../api";
import type { WorkspaceProject } from "../api";
import { Modal } from "../components/Modal";
import { SecondaryEditor } from "../features/sidebar/SecondaryEditor";

/**
 * Rename a project and edit the directories bound to it.
 *
 * Mounted only while a target is open, and keyed by its root, so the name field
 * starts from that project's own name and a rename cannot leak into the next
 * project the user edits.
 */
export function ProjectEditDialog({
  target,
  secondary,
  viewTokenRef,
  onClose,
  onProjects,
  onSaved,
  onError,
  onSessionsChanged,
}: {
  target: { root: string | null; name: string };
  secondary: string[];
  viewTokenRef: React.RefObject<number>;
  onClose: () => void;
  onProjects: (projects: WorkspaceProject[]) => void;
  onSaved: () => void;
  onError: (message: string) => void;
  onSessionsChanged: () => void;
}) {
  const [name, setName] = useState(target.name);

  return (
    <Modal
      open
      onClose={onClose}
      title="编辑项目"
      variant="project-edit-modal"
      actions={
        <>
          <button type="button" className="modal-cancel" onClick={onClose}>
            取消
          </button>
          <button
            type="button"
            className="primary"
            onClick={async () => {
              try {
                const r = await saveProject(target.root, secondary, undefined, name);
                onProjects(r.projects);
                onSessionsChanged();
                onSaved();
              } catch (e) {
                onError(e instanceof Error ? e.message : String(e));
              }
              onClose();
            }}
          >
            保存
          </button>
        </>
      }
    >
      <label className="modal-field">
        <span>项目名称</span>
        <input type="text" value={name} onChange={(e) => setName(e.target.value)} autoFocus />
      </label>
      <SecondaryEditor
        root={target.root}
        secondary={secondary}
        disabled={false}
        viewToken={viewTokenRef}
        onSecondary={() => {}}
        onProjects={onProjects}
        onError={onError}
      />
    </Modal>
  );
}
