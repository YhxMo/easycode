import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { CSSProperties } from "react";
import { createPortal } from "react-dom";
import { Modal } from "./components/Modal";
import { useDismiss } from "./lib/useDismiss";
import { placeMenu, type MenuPlacement } from "./lib/menuPlacement";
import type { AddModelBody, ModelsInfo, UpdateModelBody } from "./api";
import { addModel, deleteModel, fetchModel, fetchModels, switchModel, updateModel } from "./api";

//: Design cap on the menu height; the room actually left on screen wins.
const MENU_MAX_HEIGHT = 460;
//: Preferred menu width, narrowed when the window cannot hold it.
const MENU_WIDTH = 270;
//: Clearance kept between the menu and the window edges, and its trigger.
const MENU_MARGIN = 12;
const MENU_GAP = 8;

const PROVIDERS = ["bailian", "deepseek", "openox", "rightcode", "openai", "anthropic", "openrouter", "custom"];

const API_FORMATS: Array<{ value: string; label: string }> = [
  { value: "openai_responses", label: "OpenAI Responses" },
  { value: "openai_compatible", label: "OpenAI Compatible" },
  { value: "anthropic", label: "Anthropic" },
  { value: "bedrock", label: "Amazon Bedrock" },
  { value: "gemini", label: "Google (Gemini)" },
];

interface ModelForm {
  alias: string;
  model: string;
  /** Optional list-grouping label; empty means "infer it". */
  provider: string;
  base_url: string;
  api_key: string;
  api_format: string;
  clear_key: boolean;
}

const EMPTY_FORM: ModelForm = {
  alias: "",
  model: "",
  provider: "",
  base_url: "",
  api_key: "",
  api_format: "openai_compatible",
  clear_key: false,
};

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

function EyeIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path d="M2.5 12S6.2 5.7 12 5.7 21.5 12 21.5 12 17.8 18.3 12 18.3 2.5 12 2.5 12Z" />
      <circle cx="12" cy="12" r="3.1" />
    </svg>
  );
}

function EyeOffIcon() {
  return (
    // Same eye as `EyeIcon` with a slash across it: the two states stay the same
    // shape at a glance, and the slash is what reads at 15px.
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path d="M2.5 12S6.2 5.7 12 5.7 21.5 12 21.5 12 17.8 18.3 12 18.3 2.5 12 2.5 12Z" />
      <circle cx="12" cy="12" r="3.1" />
      <path d="m4 4 16 16" />
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
  // modal chrome are identical, and only the request bodies differ.
  const [mode, setMode] = useState<"add" | "edit" | null>(null);
  const [revealKey, setRevealKey] = useState(false);
  const [toast, setToast] = useState<{ kind: "success"; text: string } | null>(null);
  // Where the menu fits, in viewport coordinates: it is anchored to the trigger,
  // which sits wherever the composer happens to be, so the room actually
  // available decides its size and which side it opens on.
  const [placement, setPlacement] = useState<MenuPlacement | null>(null);
  // The alias being edited plus what the backend knows about its credential —
  // never the key itself.
  const [editing, setEditing] = useState<{
    alias: string;
    hasKey: boolean;
    keyTail: string;
  } | null>(null);
  const [errors, setErrors] = useState<{ alias?: string; model?: string }>({});
  const [formError, setFormError] = useState<string | null>(null);
  // Deleting is confirmed: it takes the credential with it and leaves existing
  // sessions without a model they can send to.
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuPanelRef = useRef<HTMLDivElement>(null);
  const toastTimer = useRef<number | null>(null);
  // The panel is portalled out of the trigger's subtree, so the dismiss region
  // has to name both halves.
  const dismissRefs = useMemo(() => [triggerRef, menuPanelRef], []);

  const [form, setForm] = useState<ModelForm>(EMPTY_FORM);

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
    setForm(EMPTY_FORM);
    setEditing(null);
    setErrors({});
    setFormError(null);
    setRevealKey(false);
    setMode("add");
  };

  const openEdit = async (alias: string) => {
    closeMenu();
    setBusy(true);
    modelSeq.current += 1;
    try {
      const detail = await fetchModel(alias);
      setForm({
        alias: detail.alias,
        model: detail.model,
        provider: detail.provider ?? "",
        base_url: detail.base_url ?? "",
        // Never the stored key: the field only carries a new one, and an empty
        // field leaves the old key in place.
        api_key: "",
        api_format: detail.api_format ?? "openai_compatible",
        clear_key: false,
      });
      setEditing({ alias, hasKey: detail.has_api_key, keyTail: detail.key_tail });
      setErrors({});
      setFormError(null);
      setRevealKey(false);
      setMode("edit");
    } catch (error) {
      // Reading one model failed: say so instead of opening an empty form.
      onError?.(`读取失败：${error instanceof Error ? error.message : "请求失败"}`);
      void refreshModels();
    } finally {
      setBusy(false);
    }
  };

  const closeForm = () => {
    setMode(null);
    setEditing(null);
    setErrors({});
    setFormError(null);
    setRevealKey(false);
  };

  const submitForm = async () => {
    const alias = form.alias.trim();
    const model = form.model.trim();
    const next: { alias?: string; model?: string } = {};
    if (!alias) next.alias = "请填写别名";
    if (!model) next.model = "请填写模型 ID";
    setErrors(next);
    setFormError(null);
    if (next.alias || next.model) return;
    // An edit with nothing to edit would otherwise close as if it had saved.
    if (mode === "edit" && !editing) return;

    setBusy(true);
    modelSeq.current += 1; // an in-flight refresh must not undo this write
    try {
      if (mode === "add") {
        const body: AddModelBody = { alias, model, api_format: form.api_format };
        if (form.provider.trim()) body.provider = form.provider.trim();
        if (form.base_url.trim()) body.base_url = form.base_url.trim();
        if (form.api_key.trim()) body.api_key = form.api_key.trim();
        onChange(await addModel(body));
        flash("添加成功");
      } else if (editing) {
        const body: UpdateModelBody = {
          model,
          new_alias: alias,
          provider: form.provider.trim(),
          base_url: form.base_url.trim(),
          api_format: form.api_format,
          clear_key: form.clear_key,
        };
        if (form.api_key.trim()) body.api_key = form.api_key.trim();
        onChange(await updateModel(editing.alias, body));
        flash("保存成功");
      }
      closeForm();
    } catch (error) {
      // The reason belongs next to the fields that caused it: the dialog stays
      // open with everything typed so far still in place.
      setFormError(error instanceof Error ? error.message : "请求失败");
    } finally {
      setBusy(false);
    }
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

      <Modal
        open={mode !== null}
        onClose={closeForm}
        title={mode === "add" ? "添加模型" : "编辑模型"}
        variant="model-form-modal"
        actions={
          <>
            <button type="button" className="modal-cancel" onClick={closeForm}>
              取消
            </button>
            <button type="button" className="primary" onClick={submitForm} disabled={busy}>
              {busy ? "…" : mode === "add" ? "添加" : "保存"}
            </button>
          </>
        }
      >
        <div className="modal-section">
          <div className="modal-section-title">模型信息</div>
          <div className="modal-field">
            <label>
              别名
              <input
                value={form.alias}
                placeholder="如 my-gpt"
                onChange={(e) => setForm({ ...form, alias: e.target.value })}
              />
            </label>
            <small>只在本机标识这个模型，切换、编辑和删除都用它。</small>
            {/* Required-field complaints clear themselves as soon as the field
                has something in it. */}
            {errors.alias && !form.alias.trim() && (
              <small className="modal-field-error">{errors.alias}</small>
            )}
            {mode === "add" && models.models[form.alias.trim()] && (
              <small className="modal-field-error">
                「{form.alias.trim()}」已存在，添加会覆盖它原有的配置和凭据。
              </small>
            )}
          </div>
          <div className="modal-field">
            <label>
              模型 ID
              <input
                value={form.model}
                placeholder="如 gpt-4o"
                onChange={(e) => setForm({ ...form, model: e.target.value })}
              />
            </label>
            <small>会原样发给服务商，通常是提供方文档里的模型名。</small>
            {errors.model && !form.model.trim() && (
              <small className="modal-field-error">{errors.model}</small>
            )}
          </div>
        </div>

        <div className="modal-section">
          <div className="modal-section-title">连接设置</div>
          <label>
            接口格式
            <select
              value={form.api_format}
              onChange={(e) => setForm({ ...form, api_format: e.target.value })}
            >
              {API_FORMATS.map((f) => (
                <option key={f.value} value={f.value}>
                  {f.label}
                </option>
              ))}
            </select>
          </label>
          <label>
            Base URL
            <input
              value={form.base_url}
              placeholder="https://api.example.com/v1"
              onChange={(e) => setForm({ ...form, base_url: e.target.value })}
            />
          </label>
          <div className="modal-field">
            <label>
              API Key
              <span className="secret-input-row">
                <input
                  type={revealKey ? "text" : "password"}
                  value={form.api_key}
                  placeholder={mode === "edit" ? "留空则沿用已有密钥" : "输入该模型专用 API Key"}
                  onChange={(e) => setForm({ ...form, api_key: e.target.value, clear_key: false })}
                />
                <button
                  type="button"
                  className="secret-toggle"
                  title={revealKey ? "隐藏 API Key" : "显示 API Key"}
                  aria-label={revealKey ? "隐藏 API Key" : "显示 API Key"}
                  onClick={() => setRevealKey(!revealKey)}
                >
                  {/* The glyph names the state the field is in: dots are marked by
                      the struck-through eye, plain text by the open one. The
                      label names what pressing it does. */}
                  {revealKey ? <EyeIcon /> : <EyeOffIcon />}
                </button>
              </span>
            </label>
            {mode === "edit" && (
              <small>
                {editing?.hasKey
                  ? `已有密钥${editing.keyTail ? `（尾号 ${editing.keyTail}）` : ""}，留空保存不会改动它。`
                  : "尚未配置密钥。"}
              </small>
            )}
            {form.api_key.trim() && mode === "edit" && <small>保存后替换为新输入的密钥。</small>}
          </div>
          {mode === "edit" && (
            <div className="modal-inline-actions">
              <button
                type="button"
                className="clear-key"
                disabled={busy || (!editing?.hasKey && !form.base_url)}
                onClick={() => setForm({ ...form, api_key: "", clear_key: true })}
              >
                清除连接凭据
              </button>
              {form.clear_key && <span>保存后删除该模型的 API Key 与 Base URL</span>}
            </div>
          )}
        </div>

        <div className="modal-section">
          <div className="modal-section-title">列表分组（可选）</div>
          <div className="modal-field">
            <label>
              供应商
              <input
                value={form.provider}
                placeholder="留空则按模型 ID 推断"
                onChange={(e) => setForm({ ...form, provider: e.target.value })}
              />
            </label>
            <small>只决定它显示在模型菜单的哪一组，不影响调用方式。</small>
          </div>
        </div>

        {formError && <p className="modal-error">{formError}</p>}
      </Modal>
    </div>
  );
}
