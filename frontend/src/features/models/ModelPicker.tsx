import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { CSSProperties } from "react";
import { createPortal } from "react-dom";
import { Modal } from "../../components/Modal";
import { useDismiss } from "../../lib/useDismiss";
import { placeMenu, type MenuPlacement } from "../../lib/menuPlacement";
import type { ModelsInfo } from "../../api";
import { deleteModel, fetchModel, fetchModels, switchModel } from "../../api";
import { ModelDialog, type ModelDialogTarget } from "./ModelDialog";

//: Design cap on the menu height; the room actually left on screen wins.
const MENU_MAX_HEIGHT = 460;
//: Preferred menu width, narrowed when the window cannot hold it.
const MENU_WIDTH = 270;
//: Clearance kept between the menu and the window edges, and its trigger.
const MENU_MARGIN = 12;
const MENU_GAP = 8;

const PROVIDERS = ["bailian", "deepseek", "openox", "rightcode", "openai", "anthropic", "openrouter", "custom"];

function providerLabel(provider: string): string {
  return {
    openai: "OpenAI",
    anthropic: "Anthropic",
    bailian: "阿里云百炼",
    deepseek: "DeepSeek",
    openox: "OpenOX",
    openrouter: "OpenRouter",
    rightcode: "RightCode",
    custom: "自定义",
  }[provider] ?? provider;
}

function EditIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path d="M4 16.5V20h3.5L18 9.5 14.5 6 4 16.5Z" />
      <path d="m13.5 7 3.5 3.5" />
      <path d="m15.5 5 1-1a2.1 2.1 0 0 1 3 3l-1 1" />
    </svg>
  );
}

function TrashIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path d="M4 7h16" />
      <path d="M10 11v6M14 11v6" />
      <path d="M6.5 7 7.4 20h9.2l.9-13" />
      <path d="M9 7V4h6v3" />
    </svg>
  );
}

export function ModelPicker({
  models,
  current,
  sessionAlias,
  switchingBlocked,
  onChange,
  onError,
}: {
  models: ModelsInfo;
  /** Alias the global default points at: the checkmark and the no-op check. */
  current: string;
  /** Alias the conversation on screen really runs on, when it has one. */
  sessionAlias: string | null;
  /** A turn is running somewhere on this page: switching has to wait. */
  switchingBlocked: boolean;
  onChange: (m: ModelsInfo) => void;
  onError?: (msg: string) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [open, setOpen] = useState(false);
  // One dialog serves both write paths: the fields, the busy state and the
  // modal chrome are identical, and only the request bodies differ. Which path
  // it is and what it is editing are one state, so the form cannot be open for
  // an edit whose target is missing.
  const [target, setTarget] = useState<ModelDialogTarget | null>(null);
  const [toast, setToast] = useState<{ kind: "success"; text: string } | null>(null);
  // Where the menu fits, in viewport coordinates: it is anchored to the trigger,
  // which sits wherever the composer happens to be, so the room actually
  // available decides its size and which side it opens on.
  const [placement, setPlacement] = useState<MenuPlacement | null>(null);
  // Deleting is confirmed: it takes the credential with it and leaves existing
  // sessions without a model they can send to.
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuPanelRef = useRef<HTMLDivElement>(null);
  const toastTimer = useRef<number | null>(null);
  // The panel is portalled out of the trigger's subtree, so the dismiss region
  // has to name both halves.
  const dismissRefs = useMemo(() => [triggerRef, menuPanelRef], []);

  const closeMenu = useCallback(() => {
    setOpen(false);
    // The menu may hold focus (its rows are the last thing clicked): hand it
    // back to the trigger, which is the control that stays on screen.
    triggerRef.current?.focus();
  }, []);

  useDismiss(dismissRefs, closeMenu, open);

  // One refresh entry (opening the menu) and one rule for who may write the
  // result: a response only lands while it is still the newest request, so a
  // slow read cannot undo a switch or an edit that finished after it started.
  const modelSeq = useRef(0);
  const refreshModels = useCallback(async () => {
    const ticket = ++modelSeq.current;
    try {
      const next = await fetchModels();
      if (modelSeq.current === ticket) onChange(next);
    } catch (error) {
      if (modelSeq.current === ticket) {
        onError?.(`读取模型列表失败：${error instanceof Error ? error.message : "请求失败"}`);
      }
    }
  }, [onChange, onError]);

  const measureMenu = useCallback(() => {
    const anchor = triggerRef.current?.getBoundingClientRect();
    if (!anchor) return;
    setPlacement(
      placeMenu({
        anchor,
        viewport: { width: window.innerWidth, height: window.innerHeight },
        gap: MENU_GAP,
        margin: MENU_MARGIN,
        width: MENU_WIDTH,
        maxHeight: MENU_MAX_HEIGHT,
      }),
    );
  }, []);

  // Before paint, so the first frame the reader sees is already in place.
  useLayoutEffect(() => {
    if (open) measureMenu();
  }, [open, measureMenu]);

  // The composer moves with the window, the chat column (right pane opening and
  // closing) and any scrolling container, so the anchor is re-measured rather
  // than trusted for as long as the menu stays up.
  useEffect(() => {
    if (!open) return;
    const reposition = () => measureMenu();
    window.addEventListener("resize", reposition);
    window.addEventListener("scroll", reposition, true);
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(reposition);
    if (triggerRef.current) observer?.observe(triggerRef.current);
    return () => {
      window.removeEventListener("resize", reposition);
      window.removeEventListener("scroll", reposition, true);
      observer?.disconnect();
    };
  }, [open, measureMenu]);

  useEffect(() => {
    return () => {
      if (toastTimer.current !== null) window.clearTimeout(toastTimer.current);
    };
  }, []);

  const flash = (text: string) => {
    if (toastTimer.current !== null) window.clearTimeout(toastTimer.current);
    setToast({ kind: "success", text });
    toastTimer.current = window.setTimeout(() => {
      setToast(null);
      toastTimer.current = null;
    }, 2400);
  };

  const handleSwitch = async (alias: string) => {
    if (switchingBlocked || alias === current) return;
    closeMenu();
    setBusy(true);
    modelSeq.current += 1; // an in-flight refresh must not undo this switch
    try {
      onChange(await switchModel(alias));
    } catch (error) {
      const message = error instanceof Error ? error.message : "请求失败";
      // DEC-F1: one error channel — the App toast. `current` is left
      // untouched, so the old model stays highlighted (onChange only runs on
      // success).
      onError?.(`切换失败：${message}`);
      void refreshModels();
    } finally {
      setBusy(false);
    }
  };

  const openAdd = () => {
    // Hand focus to the trigger before the menu row unmounts: the dialog records
    // whatever owns focus when it opens and returns it there on close.
    closeMenu();
    setTarget({ kind: "add" });
  };

  const openEdit = async (alias: string) => {
    closeMenu();
    setBusy(true);
    modelSeq.current += 1;
    try {
      const detail = await fetchModel(alias);
      setTarget({ kind: "edit", alias, hasKey: detail.has_api_key, keyTail: detail.key_tail, detail });
    } catch (error) {
      // Reading one model failed: say so instead of opening an empty form.
      onError?.(`读取失败：${error instanceof Error ? error.message : "请求失败"}`);
      void refreshModels();
    } finally {
      setBusy(false);
    }
  };

  const handleSaved = (next: ModelsInfo) => {
    modelSeq.current += 1; // an in-flight refresh must not undo this write
    onChange(next);
    flash(target?.kind === "add" ? "添加成功" : "保存成功");
    setTarget(null);
  };

  const removeModel = async (alias: string) => {
    if (!alias || !models.models[alias]) return;
    setBusy(true);
    modelSeq.current += 1;
    try {
      onChange(await deleteModel(alias));
      closeMenu();
    } catch (error) {
      // The dialog is already gone, so the reason goes to the app toast.
      onError?.(`删除失败：${error instanceof Error ? error.message : "请求失败"}`);
      void refreshModels();
    } finally {
      setBusy(false);
    }
  };

  const activeModel = models.models[current] !== undefined ? current : Object.keys(models.models)[0] || "";
  // The field belongs to the conversation in front of it, so it names that
  // conversation's model — never the global default masquerading as one.
  const triggerAlias = sessionAlias ?? activeModel;
  const displayNameFor = (alias: string) => models.models[alias]?.model || alias;
  const providerFor = (alias: string) => models.providers[alias] ?? "custom";
  const providerGroups = Object.keys(models.models)
    .sort()
    .reduce<Record<string, string[]>>((groups, alias) => {
      const provider = providerFor(alias);
      (groups[provider] ??= []).push(alias);
      return groups;
    }, {});
  const orderedProviders = Object.keys(providerGroups).sort((a, b) => {
    const ai = PROVIDERS.indexOf(a);
    const bi = PROVIDERS.indexOf(b);
    return (ai < 0 ? PROVIDERS.length : ai) - (bi < 0 ? PROVIDERS.length : bi) || a.localeCompare(b);
  });

  // The menu is portalled out of the composer, which clips its own overflow and
  // would otherwise crop the list; the placement keeps it on screen instead.
  const menuStyle: CSSProperties | undefined = placement
    ? {
        left: placement.left,
        width: placement.width,
        maxHeight: placement.maxHeight,
        top: placement.side === "down" ? placement.top : "auto",
        bottom: placement.side === "up" ? placement.bottom : "auto",
      }
    : undefined;

  return (
    <div className="model-picker-row">
      {toast && (
        <div className={`model-toast ${toast.kind}`} role="status">
          {toast.text}
        </div>
      )}
      <div className="custom-dropdown model-dropdown-wrap">
        <button
          ref={triggerRef}
          type="button"
          className="custom-dropdown-trigger model-trigger"
          disabled={busy}
          onClick={() => {
            if (!open) void refreshModels();
            setOpen(!open);
          }}
        >
          <span
            className="dropdown-value"
            title={triggerAlias ? `${triggerAlias} · ${displayNameFor(triggerAlias)}` : undefined}
          >
            {triggerAlias ? displayNameFor(triggerAlias) : "选择模型"}
          </span>
          <span className="dropdown-caret" aria-hidden="true">
            ⌄
          </span>
        </button>

        {open &&
          createPortal(
            <div className="custom-dropdown-menu model-menu" ref={menuPanelRef} style={menuStyle}>
              {/* Header and footer sit outside the scroll area: what the menu is
                  and how to add to it stay put while the list scrolls. */}
              <div className="dropdown-menu-header">
                <span>切换模型</span>
                <small>
                  {switchingBlocked
                    ? "有会话正在运行，结束后才能切换"
                    : "切换会影响所有已有会话及新会话"}
                </small>
              </div>
              <div className="model-menu-list">
                {orderedProviders.map((provider) => (
                  <div className="model-provider-group" key={provider}>
                    <div className="model-provider-label">{providerLabel(provider)}</div>
                    {providerGroups[provider].map((alias) => (
                      <div
                        className={`model-choice-row ${alias === current ? "active" : ""}`}
                        key={alias}
                      >
                        <button
                          type="button"
                          className="custom-dropdown-item model-choice"
                          disabled={switchingBlocked}
                          // The alias is the name every operation keys on, so it
                          // is what the row says; the model it resolves to stays
                          // in the tooltip and the edit dialog.
                          title={`${alias} · ${displayNameFor(alias)}`}
                          onClick={() => handleSwitch(alias)}
                        >
                          <span className="dropdown-item-check" aria-hidden="true">
                            {alias === current ? "✓" : ""}
                          </span>
                          <span className="dropdown-item-text">{alias}</span>
                        </button>
                        <span className="model-row-actions">
                          <button
                            type="button"
                            className="model-detail-icon edit"
                            title={`编辑 ${alias}`}
                            aria-label={`编辑 ${alias}`}
                            disabled={busy}
                            onClick={() => openEdit(alias)}
                          >
                            <EditIcon />
                          </button>
                          <button
                            type="button"
                            className="model-detail-icon delete"
                            title={`删除 ${alias}`}
                            aria-label={`删除 ${alias}`}
                            disabled={busy}
                            onClick={() => {
                              // The dialog covers the page: leaving the menu open
                              // behind the scrim only makes its state ambiguous.
                              closeMenu();
                              setConfirmDelete(alias);
                            }}
                          >
                            <TrashIcon />
                          </button>
                        </span>
                      </div>
                    ))}
                  </div>
                ))}
              </div>
              <div className="model-menu-footer">
                <button type="button" onClick={openAdd}>
                  <span aria-hidden="true">＋</span> 添加模型
                </button>
              </div>
            </div>,
            document.body,
          )}
      </div>

      <Modal
        open={confirmDelete !== null}
        onClose={() => setConfirmDelete(null)}
        title="删除模型"
        actions={
          <>
            <button type="button" className="modal-cancel" onClick={() => setConfirmDelete(null)}>
              取消
            </button>
            <button
              type="button"
              className="danger"
              onClick={() => {
                const alias = confirmDelete;
                setConfirmDelete(null);
                if (alias) void removeModel(alias);
              }}
            >
              确认删除
            </button>
          </>
        }
      >
        {confirmDelete && (
          <>
            <p className="modal-desc">
              将删除「{confirmDelete}」及其连接凭据（{displayNameFor(confirmDelete)}）。
            </p>
            <p className="modal-desc">
              已经用过它的会话保留全部历史，但下次发送前需要切换到其他可用模型。
            </p>
          </>
        )}
      </Modal>

      {target && (
        <ModelDialog
          target={target}
          taken={(alias) => Boolean(models.models[alias])}
          onClose={() => setTarget(null)}
          onSaved={handleSaved}
        />
      )}
    </div>
  );
}
