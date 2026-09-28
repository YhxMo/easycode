import { useRef, useState } from "react";
import { useDismiss } from "../../lib/useDismiss";

const MODES = [
  {
    value: "ask",
    label: "请求批准",
    hint: "敏感操作执行前先询问你",
    icon: "◇",
  },
  {
    value: "auto-review",
    label: "自动审查",
    hint: "越界操作执行前由独立 reviewer 审批",
    icon: "✓",
  },
  {
    value: "allow-all",
    label: "完全访问",
    hint: "关闭沙箱与审批，允许宿主机完整访问（破坏性命令不再拦截）",
    icon: "∞",
  },
] as const;

export function PermissionPicker({
  value,
  disabled,
  compact,
  onChange,
}: {
  value: string;
  disabled: boolean;
  compact?: boolean;
  onChange: (mode: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const current = MODES.find((m) => m.value === value) ?? MODES[0];

  useDismiss(ref, () => setOpen(false), open);

  return (
    <div
      className={`perm-picker ${compact ? "compact" : ""}`}
      ref={ref}
    >
      {!compact && <label className="picker-label">权限</label>}
      <button
        type="button"
        className="perm-trigger"
        title={current.hint}
        aria-haspopup="menu"
        aria-expanded={open}
        disabled={disabled}
        onClick={() => setOpen(!open)}
      >
        <span className="perm-trigger-icon" aria-hidden="true">{current.icon}</span>
        <span className="perm-trigger-label">{current.label}</span>
        <span className="perm-caret" aria-hidden="true">⌄</span>
      </button>
      {open && (
        <div className="perm-menu" role="menu" aria-label="选择权限模式">
          <div className="perm-menu-head">
            <strong>权限模式</strong>
            <span>控制 Easy code 可以自动执行哪些操作</span>
          </div>
          {MODES.map((m) => (
            <button
              type="button"
              key={m.value}
              className={`perm-item ${m.value === value ? "active" : ""}`}
              title={m.hint}
              role="menuitemradio"
              aria-checked={m.value === value}
              onClick={() => {
                onChange(m.value);
                setOpen(false);
              }}
            >
              <span className="perm-item-icon" aria-hidden="true">{m.icon}</span>
              <span className="perm-item-copy">
                <strong>{m.label}</strong>
                <small>{m.hint}</small>
              </span>
              <span className="perm-radio" aria-hidden="true">
                {m.value === value && <span />}
              </span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
