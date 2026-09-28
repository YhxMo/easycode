import type { SessionSummary } from "../api";
import { Modal } from "../components/Modal";
import { basename, DEFAULT_PROJECT } from "../lib/paths";

/** What removing a project would take with it. */
export interface RemoveTarget {
  root: string | null;
  count: number;
}

export interface ConfirmDialogsProps {
  /** Full access is being asked for and has not been confirmed yet. */
  fullAccess: boolean;
  onDismissFullAccess: () => void;
  onConfirmFullAccess: () => void;
  /** The conversation whose permanent deletion is waiting on a second click. */
  deleteTarget: SessionSummary | null;
  onCancelDelete: () => void;
  onConfirmDelete: () => void;
  removeTarget: RemoveTarget | null;
  onCancelRemove: () => void;
  onConfirmRemove: () => void;
}

/**
 * The dialogs that stand between a click and something irreversible.
 *
 * Full access is the one setting that removes every boundary, so it is the one
 * the user has to name out loud: the confirmation is what states the consent,
 * and only its own button sends it to the server.
 */
export function ConfirmDialogs({
  fullAccess,
  onDismissFullAccess,
  onConfirmFullAccess,
  deleteTarget,
  onCancelDelete,
  onConfirmDelete,
  removeTarget,
  onCancelRemove,
  onConfirmRemove,
}: ConfirmDialogsProps) {
  return (
    <>
      <Modal
        open={fullAccess}
        onClose={onDismissFullAccess}
        title="确认完全访问"
        variant="permission-warning-modal"
        actions={
          <>
            <button type="button" className="modal-cancel" onClick={onDismissFullAccess}>
              取消
            </button>
            <button type="button" className="danger" onClick={onConfirmFullAccess}>
              确认完全访问
            </button>
          </>
        }
      >
        <p className="modal-desc">
          完全访问会关闭文件沙箱和审批，让 Easy code 可以读写宿主机上的文件并执行命令。
          仅在你确认模型和任务可信时使用。
        </p>
      </Modal>

      <Modal
        open={deleteTarget !== null}
        onClose={onCancelDelete}
        title="删除会话"
        variant="project-remove-modal"
        actions={
          <>
            <button type="button" className="modal-cancel" onClick={onCancelDelete}>
              取消
            </button>
            <button type="button" className="danger" onClick={onConfirmDelete}>
              确认删除
            </button>
          </>
        }
      >
        {deleteTarget && (
          <p className="modal-desc">
            将永久删除「{deleteTarget.title}」及其全部消息（不可恢复）。
          </p>
        )}
      </Modal>

      <Modal
        open={removeTarget !== null}
        onClose={onCancelRemove}
        title="移除项目"
        variant="project-remove-modal"
        actions={
          <>
            <button type="button" className="modal-cancel" onClick={onCancelRemove}>
              取消
            </button>
            <button type="button" className="danger" onClick={onConfirmRemove}>
              确认移除
            </button>
          </>
        }
      >
        {removeTarget && (
          <p className="modal-desc">
            将删除「{removeTarget.root ? basename(removeTarget.root) : DEFAULT_PROJECT}」的绑定，
            并删除其下 {removeTarget.count} 条会话（不可恢复）。
          </p>
        )}
      </Modal>
    </>
  );
}
