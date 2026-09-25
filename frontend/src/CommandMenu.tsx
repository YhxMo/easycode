import { filterCommands, clampCommandIndex } from "./lib/commands";
import { useMemo, useRef } from "react";
import type { CommandInfo } from "./api";
import { useRevealActive } from "./lib/useRevealActive";

/** Id of the listbox the composer field points at with aria-controls. */
export const COMMAND_LIST_ID = "composer-command-list";

function SkillIcon() {
  return (
    <svg className="command-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M12 2L2 7l10 5 10-5-10-5z" />
      <path d="M2 17l10 5 10-5" />
      <path d="M2 12l10 5 10-5" />
    </svg>
  );
}

function CommandIcon() {
  return (
    <svg className="command-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <polyline points="4 17 10 11 4 5" />
      <line x1="12" y1="19" x2="20" y2="19" />
    </svg>
  );
}

export function CommandMenu({
  commands,
  open,
  query,
  index,
  onPick,
  onClose,
}: {
  commands: CommandInfo[];
  open: boolean;
  query: string;
  index: number;
  onPick: (c: CommandInfo) => void;
  onClose: () => void;
}) {
  const filtered = useMemo(() => filterCommands(commands, query), [commands, query]);
  const boxRef = useRef<HTMLDivElement>(null);
  const idx = clampCommandIndex(filtered, index);
  useRevealActive(boxRef, ".command-item.active", `${idx}:${filtered.length}`);

  if (!open || filtered.length === 0) return null;

  // filterCommands returns the list grouped by kind (skill → template →
  // builtin), which is exactly the section order below; the cursor index
  // therefore always names the same item that is highlighted.
  const skills = filtered.filter((c) => c.kind === "skill");
  const templates = filtered.filter((c) => c.kind === "template");

  let globalCounter = 0;

  const renderSection = (title: string, items: CommandInfo[]) => {
    if (items.length === 0) return null;
    return (
      <div className="command-section" key={title}>
        <div className="command-section-title">{title}</div>
        <div className="command-section-items">
          {items.map((c) => {
            const itemIdx = globalCounter++;
            const isActive = itemIdx === idx;
            const sourceLabel = c.source === "user" ? "个人" : c.source === "project" ? "项目" : "";
            return (
              <button
                type="button"
                role="option"
                aria-selected={isActive}
                key={c.name}
                className={`command-item ${isActive ? "active" : ""}`}
                onMouseDown={(e) => {
                  e.preventDefault();
                  onPick(c);
                }}
              >
                <div className="command-item-left">
                  {c.kind === "skill" ? <SkillIcon /> : <CommandIcon />}
                  <span className="command-name">/{c.name}</span>
                  {c.argument_hint && <span className="command-hint">{c.argument_hint}</span>}
                  <span className="command-desc" title={c.description}>
                    {c.description}
                  </span>
                </div>
                {sourceLabel && <div className="command-source">{sourceLabel}</div>}
              </button>
            );
          })}
        </div>
      </div>
    );
  };

  return (
    <div className="command-menu">
      <div className="command-menu-scroll" ref={boxRef} id={COMMAND_LIST_ID} role="listbox" aria-label="可用命令">
        {renderSection("技能", skills)}
        {renderSection("命令", templates)}
      </div>
      <div className="command-scrim" onClick={onClose} />
    </div>
  );
}
