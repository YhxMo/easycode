import { useEffect, useRef, useState } from "react";
import type { AddModelBody, ModelsInfo } from "./api";
import { addModel, deleteModel, fetchModels, switchModel } from "./api";

const PROVIDERS = ["openai", "anthropic", "deepseek", "openrouter", "custom"];

export function ModelPicker({
  models,
  current,
  onChange,
}: {
  models: ModelsInfo;
  current: string;
  onChange: (m: ModelsInfo) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [open, setOpen] = useState(false);
  const [showAdd, setShowAdd] = useState(false);
  const [providerOpen, setProviderOpen] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);
  const providerRef = useRef<HTMLDivElement>(null);

  const [form, setForm] = useState({
    alias: "",
    model: "",
    provider: "openai",
    base_url: "",
    api_key: "",
  });

  useEffect(() => {
    fetchModels().then(onChange).catch(() => {});
  }, []);

  useEffect(() => {
    const onDoc = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
      if (providerRef.current && !providerRef.current.contains(e.target as Node)) {
        setProviderOpen(false);
      }
    };
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, []);

  const handleSwitch = async (alias: string) => {
    setOpen(false);
    if (alias === current) return;
    setBusy(true);
    try {
      onChange(await switchModel(alias));
    } catch {
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
      onChange(await addModel(body));
      setShowAdd(false);
      setForm({ alias: "", model: "", provider: "openai", base_url: "", api_key: "" });
    } catch {
      // keep dialog open on failure
    } finally {
      setBusy(false);
    }
  };

  const removeCurrent = async () => {
    if (!current || !models.models[current]) return;
    setBusy(true);
    try {
      onChange(await deleteModel(current));
    } catch {
      fetchModels().then(onChange).catch(() => {});
    } finally {
      setBusy(false);
    }
  };

  const activeModel = models.models[current] !== undefined ? current : Object.keys(models.models)[0] || "";

  return (
    <div className="model-picker-row">
      <div className="custom-dropdown model-dropdown-wrap" ref={menuRef}>
        <button
          type="button"
          className="custom-dropdown-trigger model-trigger"
          disabled={busy}
          onClick={() => setOpen(!open)}
        >
          <span className="dropdown-value" title={activeModel}>
            {activeModel || "选择模型"}
          </span>
          <span className="dropdown-caret" aria-hidden="true">
            ⌄
          </span>
        </button>

        {open && (
          <div className="custom-dropdown-menu model-menu">
            <div className="dropdown-menu-header">切换模型</div>
            {Object.keys(models.models).map((alias) => (
              <button
                type="button"
                key={alias}
                className={`custom-dropdown-item ${alias === current ? "active" : ""}`}
                onClick={() => handleSwitch(alias)}
              >
                <span className="dropdown-item-check" aria-hidden="true">
                  {alias === current ? "✓" : ""}
                </span>
                <span className="dropdown-item-text">{alias}</span>
              </button>
            ))}
          </div>
        )}
      </div>

      <button className="model-btn" title="删除当前模型" onClick={removeCurrent} disabled={busy}>
        ✕
      </button>
      <button className="model-btn" title="添加模型" onClick={() => setShowAdd(true)} disabled={busy}>
        ＋
      </button>

      {showAdd && (
        <div className="modal-overlay" onClick={() => setShowAdd(false)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <h3>添加模型</h3>
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
              <span className="modal-field-label">Provider</span>
              <div className="custom-dropdown" ref={providerRef}>
                <button
                  type="button"
                  className="custom-dropdown-trigger form-trigger"
                  onClick={() => setProviderOpen(!providerOpen)}
                >
                  <span className="dropdown-value">{form.provider}</span>
                  <span className="dropdown-caret" aria-hidden="true">
                    ⌄
                  </span>
                </button>
                {providerOpen && (
                  <div className="custom-dropdown-menu provider-menu">
                    {PROVIDERS.map((p) => (
                      <button
                        type="button"
                        key={p}
                        className={`custom-dropdown-item ${p === form.provider ? "active" : ""}`}
                        onClick={() => {
                          setForm({ ...form, provider: p });
                          setProviderOpen(false);
                        }}
                      >
                        <span className="dropdown-item-check" aria-hidden="true">
                          {p === form.provider ? "✓" : ""}
                        </span>
                        <span className="dropdown-item-text">{p}</span>
                      </button>
                    ))}
                  </div>
                )}
              </div>
            </div>
            <label>
              Base URL（可选）
              <input
                value={form.base_url}
                placeholder="https://api.example.com/v1"
                onChange={(e) => setForm({ ...form, base_url: e.target.value })}
              />
            </label>
            <label>
              API Key（可选，留空则用环境变量）
              <input
                type="password"
                value={form.api_key}
                placeholder="sk-..."
                onChange={(e) => setForm({ ...form, api_key: e.target.value })}
              />
            </label>
            <div className="modal-actions">
              <button type="button" onClick={() => setShowAdd(false)}>
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
            </div>
          </div>
        </div>
      )}
    </div>
  );
}