import { useState } from "react";
import { Modal } from "../../components/Modal";
import type { AddModelBody, EditableModel, ModelsInfo, UpdateModelBody } from "../../api";
import { addModel, updateModel } from "../../api";

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

/** Which write path the dialog is open on, and on what. */
export type ModelDialogTarget =
  | { kind: "add" }
  | { kind: "edit"; alias: string; hasKey: boolean; keyTail: string; detail: EditableModel };

function formFor(target: ModelDialogTarget): ModelForm {
  if (target.kind === "add") return EMPTY_FORM;
  const d = target.detail;
  return {
    alias: d.alias,
    model: d.model,
    provider: d.provider ?? "",
    base_url: d.base_url ?? "",
    // Never the stored key: the field only carries a new one, and an empty
    // field leaves the old key in place.
    api_key: "",
    api_format: d.api_format ?? "openai_compatible",
    clear_key: false,
  };
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

/**
 * Add / edit form for one model.
 *
 * Mounted only while it is open, so every opening starts from a fresh form: the
 * fields, the validation and the busy state all belong to the dialog and cannot
 * leak into the menu that opened it.
 */
export function ModelDialog({
  target,
  taken,
  onClose,
  onSaved,
}: {
  target: ModelDialogTarget;
  /** Whether an alias already exists: the add path warns but still overwrites. */
  taken: (alias: string) => boolean;
  onClose: () => void;
  onSaved: (models: ModelsInfo) => void;
}) {
  const [form, setForm] = useState<ModelForm>(() => formFor(target));
  const [errors, setErrors] = useState<{ alias?: string; model?: string }>({});
  const [formError, setFormError] = useState<string | null>(null);
  const [revealKey, setRevealKey] = useState(false);
  const [busy, setBusy] = useState(false);

  const submitForm = async () => {
    const alias = form.alias.trim();
    const model = form.model.trim();
    const next: { alias?: string; model?: string } = {};
    if (!alias) next.alias = "请填写别名";
    if (!model) next.model = "请填写模型 ID";
    setErrors(next);
    setFormError(null);
    if (next.alias || next.model) return;

    setBusy(true);
    try {
      if (target.kind === "add") {
        const body: AddModelBody = { alias, model, api_format: form.api_format };
        if (form.provider.trim()) body.provider = form.provider.trim();
        if (form.base_url.trim()) body.base_url = form.base_url.trim();
        if (form.api_key.trim()) body.api_key = form.api_key.trim();
        onSaved(await addModel(body));
      } else {
        const body: UpdateModelBody = {
          model,
          new_alias: alias,
          provider: form.provider.trim(),
          base_url: form.base_url.trim(),
          api_format: form.api_format,
          clear_key: form.clear_key,
        };
        if (form.api_key.trim()) body.api_key = form.api_key.trim();
        onSaved(await updateModel(target.alias, body));
      }
    } catch (error) {
      // The reason belongs next to the fields that caused it: the dialog stays
      // open with everything typed so far still in place.
      setFormError(error instanceof Error ? error.message : "请求失败");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      open
      onClose={onClose}
      title={target.kind === "add" ? "添加模型" : "编辑模型"}
      variant="model-form-modal"
      actions={
        <>
          <button type="button" className="modal-cancel" onClick={onClose}>
            取消
          </button>
          <button type="button" className="primary" onClick={submitForm} disabled={busy}>
            {busy ? "…" : target.kind === "add" ? "添加" : "保存"}
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
          {target.kind === "add" && taken(form.alias.trim()) && (
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
          <span className="modal-select-wrap">
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
          </span>
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
                placeholder={target.kind === "edit" ? "留空则沿用已有密钥" : "输入该模型专用 API Key"}
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
          {target.kind === "edit" && (
            <small>
              {target.hasKey
                ? `已有密钥${target.keyTail ? `（尾号 ${target.keyTail}）` : ""}，留空保存不会改动它。`
                : "尚未配置密钥。"}
            </small>
          )}
          {form.api_key.trim() && target.kind === "edit" && <small>保存后替换为新输入的密钥。</small>}
        </div>
        {target.kind === "edit" && (
          <div className="modal-inline-actions">
            <button
              type="button"
              className="clear-key"
              disabled={busy || (!target.hasKey && !form.base_url)}
              onClick={() => setForm({ ...form, api_key: "", clear_key: true })}
            >
              清除连接凭据
            </button>
            {form.clear_key && <span>保存后删除该模型的 API Key 与 Base URL</span>}
          </div>
        )}
      </div>

      <div className="modal-section">
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
  );
}
