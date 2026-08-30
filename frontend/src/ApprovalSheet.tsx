export interface ApprovalSheetProps {
  open: boolean;
  position: number; // 1-based index within the pending queue
  total: number;
  name: string;
  reason?: string;
  scope?: string;
  onDecide: (approve: boolean, always: boolean) => void;
  onDismiss: () => void;
}

export function ApprovalSheet({
  open,
  position,
  total,
  name,
  reason,
  scope,
  onDecide,
  onDismiss,
}: ApprovalSheetProps) {
  // Closed sheet stays mounted so the slide-up/down animation is preserved,
  // but it is removed from the accessibility tree and keyboard tab order so a
  // hidden allow/deny control can never be activated by accident.
  const interactiveTab = open ? undefined : -1;
  return (
    <div
      className={`approval-sheet ${open ? "open" : ""}`}
      role="alertdialog"
      aria-modal="false"
      aria-label="需要权限"
      aria-hidden={open ? undefined : true}
      inert={open ? undefined : true}
    >
      <div className="approval-sheet-head">
        <span className="approval-sheet-count">
          {position}/{total} 个问题
        </span>
        <button
          type="button"
          className="approval-sheet-close"
          aria-label="收起"
          title="收起"
          tabIndex={interactiveTab}
          onClick={onDismiss}
        >
          ×
        </button>
      </div>
      <div className="approval-sheet-card">
        <div className="approval-sheet-title">
          <span className="approval-sheet-icon" aria-hidden="true">⚠</span>
          <strong>需要权限</strong>
        </div>
        <div className="approval-sheet-reason">{reason ?? `请求执行 ${name}`}</div>
        {scope && <pre className="approval-sheet-scope">{scope}</pre>}
      </div>
      <div className="approval-sheet-actions">
        <button type="button" className="deny" tabIndex={interactiveTab} onClick={() => onDecide(false, false)}>
          拒绝
        </button>
        <button type="button" className="always" tabIndex={interactiveTab} onClick={() => onDecide(true, true)}>
          始终允许
        </button>
        <button type="button" className="allow" tabIndex={interactiveTab} onClick={() => onDecide(true, false)}>
          允许一次
        </button>
      </div>
    </div>
  );
}
