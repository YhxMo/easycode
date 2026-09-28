import { useCallback, useId, useRef, useState } from "react";
import { Modal } from "../../components/Modal";
import { McpSettingsPanel } from "./McpSettings";
import { SkillsSettings } from "./SkillsSettings";

/** What the dialog was opened for: frozen while it is open. */
export interface ExtensionTarget {
  /** The project whose skills and servers the panel edits. */
  root: string | null;
  /**
   * The open conversation, but only when it really runs in ``root``: connection
   * status belongs to the session it was read from, and one project's status
   * must never be shown under another project's servers.
   */
  sessionId: string | null;
  initialTab: "skills" | "mcp";
}

export interface ExtensionsSettingsProps extends ExtensionTarget {
  /** The target project's display name, for the header. */
  projectName: string;
  /** Its resolved directory; null when the project has no directory of its own. */
  projectPath: string | null;
  onClose: () => void;
  /** Something was installed or saved: the `/` menu must re-read its sources. */
  onChanged: () => void;
  onToast: (kind: "ok" | "err", text: string) => void;
}

const TABS = [
  { id: "skills", label: "Skills" },
  { id: "mcp", label: "MCP 服务" },
] as const;

/**
 * One dialog for everything a project can be extended with.
 *
 * Skills and MCP servers are edited in separate panels but belong to the same
 * question — "what can this project's conversations use?" — so they share one
 * dialog, one target project and one footer row.
 *
 * Both panels stay mounted and the inactive one is only hidden: a half-typed
 * server form must survive a glance at the skills list. What must not survive
 * is focus inside the hidden branch, which is why the dialog's Tab cycle skips
 * hidden subtrees.
 */
export function ExtensionsSettings({
  root,
  sessionId,
  initialTab,
  projectName,
  projectPath,
  onClose,
  onChanged,
  onToast,
}: ExtensionsSettingsProps) {
  const baseId = useId();
  const [tab, setTab] = useState<"skills" | "mcp">(initialTab);
  // Published by Modal: the panels portal their own buttons into this row, so
  // neither parent has to know a panel's fields or its busy state.
  const [actionsHost, setActionsHost] = useState<HTMLDivElement | null>(null);
  const tabRefs = useRef<Record<string, HTMLButtonElement | null>>({});

  const select = useCallback((next: "skills" | "mcp", focus: boolean) => {
    setTab(next);
    if (focus) tabRefs.current[next]?.focus();
  }, []);

  const onTabKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLDivElement>) => {
      const index = TABS.findIndex((t) => t.id === tab);
      if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
        e.preventDefault();
        const delta = e.key === "ArrowRight" ? 1 : -1;
        select(TABS[(index + delta + TABS.length) % TABS.length].id, true);
      } else if (e.key === "Home" || e.key === "End") {
        e.preventDefault();
        select(TABS[e.key === "Home" ? 0 : TABS.length - 1].id, true);
      }
    },
    [tab, select],
  );

  const panelId = (id: string) => `${baseId}-panel-${id}`;
  const tabId = (id: string) => `${baseId}-tab-${id}`;

  return (
    <Modal open onClose={onClose} title="扩展" variant="extensions-modal" footerRef={setActionsHost}>
      <p className="ext-target">
        <span className="ext-target-name">{projectName}</span>
        <span className="ext-target-path">{projectPath ?? "默认工作区（无固定目录）"}</span>
      </p>
      <div
        className="ext-tabs"
        role="tablist"
        aria-label="扩展类型"
        onKeyDown={onTabKeyDown}
      >
        {TABS.map((t) => (
          <button
            key={t.id}
            type="button"
            role="tab"
            id={tabId(t.id)}
            ref={(el) => {
              tabRefs.current[t.id] = el;
            }}
            className={`ext-tab${tab === t.id ? " on" : ""}`}
            aria-selected={tab === t.id}
            aria-controls={panelId(t.id)}
            // Roving tabindex: Tab reaches the selected tab once, and the arrow
            // keys move between them the way the tablist pattern expects.
            tabIndex={tab === t.id ? 0 : -1}
            onClick={() => select(t.id, false)}
          >
            {t.label}
          </button>
        ))}
      </div>
      <div
        className="ext-panel"
        role="tabpanel"
        id={panelId("skills")}
        aria-labelledby={tabId("skills")}
        hidden={tab !== "skills"}
      >
        <SkillsSettings
          root={root}
          active={tab === "skills"}
          actionsHost={actionsHost}
          onClose={onClose}
          onChanged={onChanged}
          onToast={onToast}
        />
      </div>
      <div
        className="ext-panel"
        role="tabpanel"
        id={panelId("mcp")}
        aria-labelledby={tabId("mcp")}
        hidden={tab !== "mcp"}
      >
        <McpSettingsPanel
          root={root}
          sessionId={sessionId}
          active={tab === "mcp"}
          actionsHost={actionsHost}
          onClose={onClose}
          onChanged={onChanged}
          onToast={onToast}
        />
      </div>
    </Modal>
  );
}
