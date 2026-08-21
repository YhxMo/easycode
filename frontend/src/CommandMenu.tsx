import { useMemo } from "react";
import type { CommandInfo } from "./api";

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

function BuiltinIcon() {
  return (
    <svg className="command-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="12" cy="12" r="10" />
      <line x1="12" y1="8" x2="12" y2="12" />
      <line x1="12" y1="16" x2="12.01" y2="16" />
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
  const q = (query.startsWith("/") ? query.slice(1) : query).toLowerCase();

  const filtered = useMemo(() => {
    return commands.filter(
      (c) => c.name.startsWith(q) || (q.length > 0 && c.name.includes(q)) || c.description.toLowerCase().includes(q)
    );
  }, [commands, q]);

  if (!open || filtered.length === 0) return null;
  const idx = Math.min(index, filtered.length - 1);

  // Group by category: 技能 (skill), 自定义命令 (template), 内置命令 (builtin)
  const skills = filtered.filter((c) => c.kind === "skill");
  const templates = filtered.filter((c) => c.kind === "template");
  const builtins = filtered.filter((c) => c.kind === "builtin");

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
              <div
                key={c.name}
                className={`command-item ${isActive ? "active" : ""}`}
                onMouseDown={(e) => {
                  e.preventDefault();
                  onPick(c);
                }}
              >
                <div className="command-item-left">
                  {c.kind === "skill" ? (
                    <SkillIcon />
                  ) : c.kind === "template" ? (
                    <CommandIcon />
                  ) : (
                    <BuiltinIcon />
                  )}
                  <span className="command-name">/{c.name}</span>
                  {c.argument_hint && <span className="command-hint">{c.argument_hint}</span>}
                  <span className="command-desc" title={c.description}>
                    {c.description}
                  </span>
                </div>
                {sourceLabel && <div className="command-source">{sourceLabel}</div>}
              </div>
            );
          })}
        </div>
      </div>
    );
  };

  return (
    <div className="command-menu">
      <div className="command-menu-scroll">
        {renderSection("技能", skills)}
        {renderSection("命令", templates)}
        {renderSection("内置命令", builtins)}
      </div>
      <div className="command-scrim" onClick={onClose} />
    </div>
  );
}