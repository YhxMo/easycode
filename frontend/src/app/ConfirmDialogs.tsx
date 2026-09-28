import { Modal } from "../components/Modal";

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
}: {
  fullAccess: boolean;
  onDismissFullAccess: () => void;
  onConfirmFullAccess: () => void;
}) {
  return (
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
  );
}
