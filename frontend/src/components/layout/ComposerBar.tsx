import type { KeyboardEvent, ReactNode, RefObject } from "react";
import { PermissionPicker } from "../../PermissionPicker";

interface Props {
  /** `tall` is the centred first-run card; `docked` floats over the message stream. */
  variant?: "tall" | "docked";
  value: string;
  placeholder: string;
  ariaLabel: string;
  busy: boolean;
  sendBlocked: boolean;
  permission: string;
  permissionDisabled: boolean;
  onPermission: (mode: string) => void;
  /** `caret` is the field's cursor after the edit, for @-mention detection. */
  onChange: (value: string, caret: number) => void;
  onKeyDown: (event: KeyboardEvent<HTMLTextAreaElement>) => void;
  onSelectionChange: (caret: number) => void;
  onSend: () => void;
  onStop: () => void;
  /** Lets the app put the caret back in the field (mention pick, quoting). */
  fieldRef?: RefObject<HTMLTextAreaElement | null>;
  /** The model picker, kept beside the field it applies to. */
  model?: ReactNode;
  voice?: { supported: boolean; listening: boolean; toggle: () => void };
  /** Command or mention menu, anchored above the field. */
  menu?: ReactNode;
  hint?: string;
}

export function ComposerBar({
  variant = "docked",
  value,
  placeholder,
  ariaLabel,
  busy,
  sendBlocked,
  permission,
  permissionDisabled,
  onPermission,
  onChange,
  onKeyDown,
  onSelectionChange,
  onSend,
  onStop,
  fieldRef,
  model,
  voice,
  menu,
  hint,
}: Props) {
  return (
    <div className={`composer composer-${variant}`}>
      <div className="cmd-wrap">
        {menu}
        <textarea
          ref={fieldRef}
          className="composer-field"
          value={value}
          aria-label={ariaLabel}
          placeholder={placeholder}
          rows={variant === "tall" ? 3 : 1}
          onChange={(e) => onChange(e.target.value, e.target.selectionStart ?? 0)}
          onKeyDown={onKeyDown}
          onSelect={(e) => onSelectionChange(e.currentTarget.selectionStart ?? 0)}
        />
      </div>
      <div className="composer-toolbar">
        <div className="composer-options">
          <div className="chat-input-perm">
            <PermissionPicker
              compact
              value={permission}
              disabled={permissionDisabled}
              onChange={onPermission}
            />
          </div>
          {hint && <span className="composer-hint">{hint}</span>}
        </div>
        <div className="composer-trailing">
          {model}
          {voice?.supported && (
            <button
              type="button"
              className={`icon-btn${voice.listening ? " on" : ""}`}
              aria-label={voice.listening ? "停止语音输入" : "语音输入"}
              aria-pressed={voice.listening}
              title={voice.listening ? "停止语音输入" : "语音输入"}
              onClick={voice.toggle}
            >
              <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
                <rect x="9" y="3.5" width="6" height="10" rx="3" />
                <path d="M6 11.5a6 6 0 0 0 12 0M12 17.5V21" />
              </svg>
            </button>
          )}
          <button
            type="button"
            className={`send-btn ${busy ? "stop" : ""}`}
            aria-label={busy ? "停止生成" : "发送消息"}
            title={busy ? "停止生成" : "发送消息"}
            onClick={busy ? onStop : onSend}
            disabled={busy ? false : !value.trim() || sendBlocked}
          >
            {busy ? <span className="stop-square" /> : <span aria-hidden="true">↑</span>}
          </button>
        </div>
      </div>
    </div>
  );
}
