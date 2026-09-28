import { createPortal } from "react-dom";

/**
 * The Skills tab of the extensions dialog.
 *
 * The panel contract is already the one the list and the folder importer will
 * use: kept mounted while its tab is hidden, with its buttons portalled into
 * the dialog's footer row.
 */
export function SkillsSettings({
  active,
  actionsHost,
  onClose,
}: {
  root: string | null;
  active: boolean;
  actionsHost: HTMLElement | null;
  onClose: () => void;
  onChanged: () => void;
  onToast: (kind: "ok" | "err", text: string) => void;
}) {
  return (
    <>
      <p className="modal-desc">正在读取 Skill…</p>
      {active && actionsHost
        ? createPortal(
            <button type="button" className="modal-cancel" onClick={onClose}>
              关闭
            </button>,
            actionsHost,
          )
        : null}
    </>
  );
}
