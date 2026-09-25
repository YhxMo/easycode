import { useEffect, useRef } from "react";
import type { KeyboardEvent, ReactNode, RefObject } from "react";
import { PermissionPicker } from "../../PermissionPicker";
import { FIELD_MIN_HEIGHT, useAutoGrow } from "../../lib/useAutoGrow";

/** `.composer-tall .composer-field` starts taller than the docked field. */
const TALL_MIN_HEIGHT = 72;
/** Clearance the composer's popovers keep from the top of the window. */
const POPOVER_GAP = 10;
const POPOVER_MARGIN = 12;

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
  /** Combobox wiring for that menu, so the field announces its candidates. */
  menuOpen?: boolean;
  menuId?: string;
  activeOptionId?: string;
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
  menuOpen = false,
  menuId,
  activeOptionId,
}: Props) {
  // The centred first-run field starts taller but shares the growth logic; the
  // heights mirror `.composer-field` / `.composer-tall .composer-field`.
  useAutoGrow(fieldRef, value, variant === "tall" ? TALL_MIN_HEIGHT : FIELD_MIN_HEIGHT);

  // The chat card clips its own overflow, so every panel that opens upward from
  // here (command, mention, permission) would lose its top on a short window.
  // The room left above the composer is published as a custom property and each
  // panel caps its own height with it. Written straight to the node: this has to
  // keep up with scrolling, and a re-render per scroll frame is not worth it.
  const boxRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const box = boxRef.current;
    if (!box) return;
    const publish = () => {
      const room = box.getBoundingClientRect().top - POPOVER_GAP - POPOVER_MARGIN;
      box.style.setProperty("--popover-room", `${Math.max(0, Math.round(room))}px`);
    };
    publish();
    window.addEventListener("resize", publish);
    window.addEventListener("scroll", publish, true);
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(publish);
    observer?.observe(box);
    return () => {
      window.removeEventListener("resize", publish);
      window.removeEventListener("scroll", publish, true);
      observer?.disconnect();
    };
  }, []);

  return (
    <div className={`composer composer-${variant}`} ref={boxRef}>
      <div className="cmd-wrap">
        {menu}
        <textarea
          ref={fieldRef}
          className="composer-field"
          value={value}
          aria-label={ariaLabel}
          placeholder={placeholder}
          rows={1}
          role="combobox"
          aria-expanded={menuOpen}
          aria-controls={menuId}
          aria-activedescendant={activeOptionId}
          aria-autocomplete="list"
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
