import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import type { Dispatch, RefObject, SetStateAction } from "react";
import { Modal } from "./components/Modal";
import { useDismiss } from "./lib/useDismiss";
import type { AddModelBody, ModelsInfo, UpdateModelBody } from "./api";
import { addModel, deleteModel, fetchModel, fetchModels, switchModel, updateModel } from "./api";

//: Mirrors the design cap on `.model-menu`: the menu never grows past this
//: even when the trigger has more room above it.
const MENU_MAX_HEIGHT = 460;
//: Clearance kept between the menu and the window edges.
const MENU_MARGIN = 12;
//: Mirrors `.model-menu-detail`: the hover card is this wide at most.
const DETAIL_WIDTH = 320;
//: Below this the card would be too narrow to read, so it is not shown.
const DETAIL_MIN_WIDTH = 200;

const PROVIDERS = ["bailian", "deepseek", "openox", "rightcode", "openai", "anthropic", "openrouter", "custom"];

const API_FORMATS: Array<{ value: string; label: string }> = [
  { value: "openai_responses", label: "OpenAI Responses" },
  { value: "openai_compatible", label: "OpenAI Compatible" },
  { value: "anthropic", label: "Anthropic" },
  { value: "bedrock", label: "Amazon Bedrock" },
  { value: "gemini", label: "Google (Gemini)" },
];

function apiFormatLabel(value: string): string {
  return API_FORMATS.find((f) => f.value === value)?.label ?? value;
}

interface ModelForm {
  alias: string;
  model: string;
  provider: string;
  base_url: string;
  api_key: string;
  api_format: string;
  clear_key: boolean;
}

/** Fields shared by the add and edit dialogs (API Key row differs). */
function ModelFields({
  form,
  setForm,
  formatRef,
  formatOpen,
  setFormatOpen,
}: {
  form: ModelForm;
  setForm: Dispatch<SetStateAction<ModelForm>>;
  formatRef: RefObject<HTMLDivElement | null>;
  formatOpen: boolean;
  setFormatOpen: (open: boolean) => void;
}) {
  return (
    <>
      <label>
        别名
        <input
          value={form.alias}
          placeholder="如 my-gpt"
          onChange={(e) => setForm({ ...form, alias: e.target.value })}
        />
      </label>
      <label>
        模型名
        <input
          value={form.model}
          placeholder="如 gpt-4o"
          onChange={(e) => setForm({ ...form, model: e.target.value })}
        />
      </label>
      <div className="modal-field">
        <label>
          供应商
          <input
            value={form.provider}
            placeholder="如 bailian、rightcode"
            onChange={(e) => setForm({ ...form, provider: e.target.value })}
          />
        </label>
      </div>
      <div className="modal-field">
        <span className="modal-field-label">接口格式</span>
        <div className="custom-dropdown" ref={formatRef}>
          <button
            type="button"
            className="custom-dropdown-trigger form-trigger"
            onClick={() => setFormatOpen(!formatOpen)}
          >
            <span className="dropdown-value">{apiFormatLabel(form.api_format)}</span>
            <span className="dropdown-caret" aria-hidden="true">
              ⌄
            </span>
          </button>
          {formatOpen && (
            <div className="custom-dropdown-menu provider-menu">
              {API_FORMATS.map((f) => (
                <button
                  type="button"
                  key={f.value}
                  className={`custom-dropdown-item ${f.value === form.api_format ? "active" : ""}`}
                  onClick={() => {
                    setForm({ ...form, api_format: f.value });
                    setFormatOpen(false);
                  }}
                >
                  <span className="dropdown-item-check" aria-hidden="true">
                    {f.value === form.api_format ? "✓" : ""}
                  </span>
                  <span className="dropdown-item-text">{f.label}</span>
                </button>
              ))}
            </div>
          )}
        </div>
      </div>
      <label>
        Base URL
        <input
          value={form.base_url}
          placeholder="https://api.example.com/v1"
          onChange={(e) => setForm({ ...form, base_url: e.target.value })}
        />
      </label>
    </>
  );
}

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
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path d="m4.5 4.5 15 15" />
      <path d="M10.7 5.9c.4-.1.9-.2 1.3-.2 5.8 0 9.5 6.3 9.5 6.3a17.6 17.6 0 0 1-2.5 3.3" />
      <path d="M6.6 6.7C4 8.7 2.5 12 2.5 12s3.7 6.3 9.5 6.3c1.2 0 2.3-.3 3.3-.7" />
      <path d="M9.9 10.1a3.1 3.1 0 0 0 4.2 4.2" />
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
  const [showAdd, setShowAdd] = useState(false);
  const [showEdit, setShowEdit] = useState(false);
  const [formatOpen, setFormatOpen] = useState(false);
  const [revealKey, setRevealKey] = useState(false);
  const [toast, setToast] = useState<{ kind: "success"; text: string } | null>(null);
  const [hasStoredKey, setHasStoredKey] = useState(false);
  const [hovered, setHovered] = useState<{ alias: string; top: number } | null>(null);
  // Where the menu fits: it is anchored to the trigger, which sits wherever the
  // composer happens to be, so the room actually available decides its size —
  // and which side the hover detail can open on.
  const [placement, setPlacement] = useState<{
    maxHeight: number;
    left: number;
    detail: { side: "left" | "right"; width: number } | null;
  } | null>(null);
  const [editingAlias, setEditingAlias] = useState<string | null>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const menuPanelRef = useRef<HTMLDivElement>(null);
  const formatRef = useRef<HTMLDivElement>(null);
  const hoverTimer = useRef<number | null>(null);
  const toastTimer = useRef<number | null>(null);

  const [form, setForm] = useState({
    alias: "",
    model: "",
    provider: "openai",
    base_url: "",
    api_key: "",
    api_format: "openai_compatible",
    clear_key: false,
  });

  useDismiss(menuRef, () => setOpen(false), open);
  useDismiss(formatRef, () => setFormatOpen(false), formatOpen);

  const placeMenu = useCallback(() => {
    const panel = menuPanelRef.current;
    const anchor = menuRef.current?.getBoundingClientRect();
    if (!panel || !anchor) return;
    // Opens upward, so the usable height ends at the top of the window; the
    // list scrolls inside whatever is left instead of running off-screen.
    // Floor keeps the header and a row usable on a very short window; below
    // that the list scrolls rather than the menu spilling off the top.
    const maxHeight = Math.max(96, Math.min(anchor.top - MENU_MARGIN - 8, MENU_MAX_HEIGHT));
    const width = panel.offsetWidth;
    const left = Math.max(
      MENU_MARGIN,
      Math.min(anchor.left, window.innerWidth - MENU_MARGIN - width),
    );
    // The hover detail opens beside the menu, on the roomier side and no wider
    // than that gap; with neither side usable it stays hidden rather than
    // spilling past the window.
    const roomRight = window.innerWidth - MENU_MARGIN - (left + width) - 8;
    const roomLeft = left - MENU_MARGIN - 8;
    const side = roomRight >= roomLeft ? "right" : "left";
    const room = Math.max(roomRight, roomLeft);
    setPlacement({
      maxHeight,
      left: left - anchor.left,
      detail: room >= DETAIL_MIN_WIDTH ? { side, width: Math.min(DETAIL_WIDTH, room) } : null,
    });
  }, []);

  // Before paint, so the first frame the reader sees is already in place.
  useLayoutEffect(() => {
    if (open) placeMenu();
  }, [open, placeMenu]);

  useEffect(() => {
    if (!open) return;
    const onResize = () => placeMenu();
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, [open, placeMenu]);

  useEffect(() => {
    const onFocus = () => fetchModels().then(onChange).catch(() => {});
    window.addEventListener("focus", onFocus);
    return () => window.removeEventListener("focus", onFocus);
  }, [onChange]);

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
    setOpen(false);
    setBusy(true);
    try {
      onChange(await switchModel(alias));
    } catch (error) {
      const message = error instanceof Error ? error.message : "请求失败";
      // DEC-F1: one error channel — the App toast. `current` is left
      // untouched, so the old model stays highlighted (onChange only runs on
      // success).
      onError?.(`切换失败：${message}`);
      fetchModels().then(onChange).catch(() => {});
    } finally {
      setBusy(false);
    }
  };

  const submitAdd = async () => {
    if (!form.alias.trim() || !form.model.trim()) return;
    setBusy(true);
    try {
      const body: AddModelBody = { alias: form.alias.trim(), model: form.model.trim() };
      if (form.provider) body.provider = form.provider;
      if (form.base_url.trim()) body.base_url = form.base_url.trim();
      if (form.api_key.trim()) body.api_key = form.api_key.trim();
      if (form.api_format) body.api_format = form.api_format;
      onChange(await addModel(body));
      setShowAdd(false);
      flash("添加成功");
      setForm({ alias: "", model: "", provider: "openai", base_url: "", api_key: "", api_format: "openai_compatible", clear_key: false });
    } catch (error) {
      onError?.(`添加失败：${error instanceof Error ? error.message : "请求失败"}`);
    } finally {
      setBusy(false);
    }
  };

  const openEdit = async (alias = activeModel) => {
    if (!alias) return;
    setOpen(false);
    setBusy(true);
    try {
      const detail = await fetchModel(alias);
      setForm({
        alias: detail.alias,
        model: detail.model,
        provider: detail.provider ?? "openai",
        base_url: detail.base_url ?? "",
        api_key: "",
        api_format: detail.api_format ?? "openai_compatible",
        clear_key: false,
      });
      setHasStoredKey(detail.has_api_key);
      setEditingAlias(alias);
      setRevealKey(false);
      setShowEdit(true);
    } catch {
      // Keep the picker usable when the model disappears concurrently.
      fetchModels().then(onChange).catch(() => {});
    } finally {
      setBusy(false);
    }
  };

  const submitEdit = async () => {
    if (!editingAlias || !form.alias.trim() || !form.model.trim()) return;
    setBusy(true);
    try {
      const body: UpdateModelBody = {
        model: form.model.trim(),
        new_alias: form.alias.trim(),
        provider: form.provider,
        base_url: form.base_url.trim(),
        api_format: form.api_format,
        clear_key: form.clear_key,
      };
      const newKey = form.api_key.trim();
      if (newKey) body.api_key = newKey;
      onChange(await updateModel(editingAlias, body));
      setShowEdit(false);
      setRevealKey(false);
      setEditingAlias(null);
      flash("保存成功");
    } catch (error) {
      onError?.(`保存失败：${error instanceof Error ? error.message : "请求失败"}`);
    } finally {
      setBusy(false);
    }
  };

  const removeModel = async (alias = activeModel) => {
    if (!alias || !models.models[alias]) return;
    setBusy(true);
    try {
      onChange(await deleteModel(alias));
      setOpen(false);
      setHovered(null);
    } catch {
      fetchModels().then(onChange).catch(() => {});
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
  const previewAlias = hovered && models.models[hovered.alias] ? hovered.alias : "";
  const previewEntry = previewAlias ? models.models[previewAlias] : undefined;
  const previewLimits = previewAlias ? models.limits?.[previewAlias] : undefined;

  const hoverModel = (alias: string, el: HTMLElement) => {
    if (hoverTimer.current !== null) window.clearTimeout(hoverTimer.current);
    const panel = menuPanelRef.current;
    if (!panel) return;
    const panelRect = panel.getBoundingClientRect();
    // Measured against the panel, not `offsetTop`: the list scrolls inside it,
    // and the card must follow the row the reader is actually pointing at.
    const relative = el.getBoundingClientRect().top - panelRect.top - 6;
    const maxTop = Math.max(4, panelRect.height - 190);
    setHovered({ alias, top: Math.max(4, Math.min(relative, maxTop)) });
  };

  const clearHover = () => {
    if (hoverTimer.current !== null) window.clearTimeout(hoverTimer.current);
    hoverTimer.current = window.setTimeout(() => setHovered(null), 80);
  };

  return (
    <div className="model-picker-row">
      {toast && (
        <div className={`model-toast ${toast.kind}`} role="status">
          {toast.text}
        </div>
      )}
      <div className="custom-dropdown model-dropdown-wrap" ref={menuRef}>
        <button
          type="button"
          className="custom-dropdown-trigger model-trigger"
          disabled={busy}
          onClick={() => {
            setHovered(null);
            if (!open) fetchModels().then(onChange).catch(() => {});
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

        {open && (
          <div
            className="custom-dropdown-menu model-menu"
            ref={menuPanelRef}
            style={
              placement
                ? { maxHeight: placement.maxHeight, left: placement.left }
                : undefined
            }
          >
            <div className="model-menu-list">
              <div className="dropdown-menu-header">
                <span>切换模型</span>
                <small>
                  {switchingBlocked
                    ? "有会话正在运行，结束后才能切换"
                    : "切换会影响所有已有会话及新会话"}
                </small>
              </div>
              {orderedProviders.map((provider) => (
                <div className="model-provider-group" key={provider}>
                  <div className="model-provider-label">{providerLabel(provider)}</div>
                  {providerGroups[provider].map((alias) => (
                    <div
                      className={`model-choice-row ${alias === current ? "active" : ""}`}
                      key={alias}
                      onMouseEnter={(e) => hoverModel(alias, e.currentTarget)}
                      onMouseLeave={clearHover}
                    >
                      <button
                        type="button"
                        className="custom-dropdown-item model-choice"
                        disabled={switchingBlocked}
                        onClick={() => handleSwitch(alias)}
                      >
                        <span className="dropdown-item-check" aria-hidden="true">
                          {alias === current ? "✓" : ""}
                        </span>
                        <span className="dropdown-item-text">{displayNameFor(alias)}</span>
                      </button>
                      <span className="model-row-actions">
                        <button
                          type="button"
                          className="model-detail-icon edit"
                          title={`编辑 ${displayNameFor(alias)}`}
                          aria-label={`编辑 ${displayNameFor(alias)}`}
                          disabled={busy}
                          onClick={() => openEdit(alias)}
                        >
                          <EditIcon />
                        </button>
                        <button
                          type="button"
                          className="model-detail-icon delete"
                          title={`删除 ${displayNameFor(alias)}`}
                          aria-label={`删除 ${displayNameFor(alias)}`}
                          disabled={busy}
                          onClick={() => removeModel(alias)}
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
              <button
                type="button"
                onClick={() => {
                  setOpen(false);
                  setShowAdd(true);
                }}
              >
                <span aria-hidden="true">＋</span> 添加模型
              </button>
            </div>
            {previewAlias && previewEntry && placement?.detail && (
              <aside
                className={`model-menu-detail side-${placement.detail.side}`}
                aria-label={`${previewAlias} 模型详情`}
                style={{ top: hovered?.top ?? 4, width: placement.detail.width }}
              >
                <dl>
                  <div>
                    <dt>模型</dt>
                    <dd>{previewEntry.model}</dd>
                  </div>
                  <div>
                    <dt>提供商</dt>
                    <dd>{providerLabel(providerFor(previewAlias))}</dd>
                  </div>
                  <div>
                    <dt>上下文</dt>
                    <dd>{previewLimits?.context ? previewLimits.context.toLocaleString() : "未知"}</dd>
                  </div>
                  <div>
                    <dt>凭据</dt>
                    <dd>{previewEntry.key_id ? "已配置" : "未配置"}</dd>
                  </div>
                </dl>
              </aside>
            )}
          </div>
        )}
      </div>

      <Modal
        open={showAdd}
        onClose={() => setShowAdd(false)}
        title="添加模型"
        variant="model-form-modal"
        actions={
          <>
            <button type="button" className="modal-cancel" onClick={() => setShowAdd(false)}>
              取消
            </button>
            <button
              type="button"
              className="primary"
              onClick={submitAdd}
              disabled={busy || !form.alias.trim() || !form.model.trim()}
            >
              {busy ? "…" : "添加"}
            </button>
          </>
        }
      >
        <ModelFields
          form={form}
          setForm={setForm}
          formatRef={formatRef}
          formatOpen={formatOpen}
          setFormatOpen={setFormatOpen}
        />
        <label>
          API Key
          <input
            type="password"
            value={form.api_key}
            placeholder="输入该模型专用 API Key"
            onChange={(e) => setForm({ ...form, api_key: e.target.value })}
          />
        </label>
      </Modal>

      <Modal
        open={showEdit}
        onClose={() => setShowEdit(false)}
        title="编辑模型"
        variant="model-form-modal"
        actions={
          <>
            <button type="button" className="modal-cancel" onClick={() => setShowEdit(false)}>
              取消
            </button>
            <button
              type="button"
              className="primary"
              onClick={submitEdit}
              disabled={busy || !form.alias.trim() || !form.model.trim()}
            >
              {busy ? "…" : "保存"}
            </button>
          </>
        }
      >
        <ModelFields
          form={form}
          setForm={setForm}
          formatRef={formatRef}
          formatOpen={formatOpen}
          setFormatOpen={setFormatOpen}
        />
        <label>
          API Key
          <span className="secret-input-row">
            <input
              type={revealKey ? "text" : "password"}
              value={form.api_key}
              placeholder="输入该模型专用 API Key"
              onChange={(e) => setForm({ ...form, api_key: e.target.value, clear_key: false })}
            />
            <button
              type="button"
              className="secret-toggle"
              title={revealKey ? "隐藏 API Key" : "显示 API Key"}
              aria-label={revealKey ? "隐藏 API Key" : "显示 API Key"}
              onClick={() => setRevealKey(!revealKey)}
            >
              {revealKey ? <EyeOffIcon /> : <EyeIcon />}
            </button>
          </span>
        </label>
        <div className="modal-inline-actions">
          <button
            type="button"
            className="clear-key"
            disabled={busy || (!hasStoredKey && !form.api_key)}
            onClick={() => setForm({ ...form, api_key: "", clear_key: true })}
          >
            清除密钥
          </button>
          {form.clear_key && <span>保存后将删除该模型凭据</span>}
        </div>
      </Modal>
    </div>
  );
}
