import { useEffect, useRef, useState } from "react";

export interface ProjectMenuProps {
  pinned: boolean;
  disabled: boolean;
  onAction: (action: ProjectAction) => void;
}

export type ProjectAction =
  | "pin"
  | "unpin"
  | "edit"
  | "reveal"
  | "worktree"
  | "archive"
  | "remove";

function Icon({ d, fill = false }: { d: string; fill?: boolean }) {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false" fill={fill ? "currentColor" : "none"} stroke="currentColor" strokeWidth={fill ? 0 : 1.7} strokeLinecap="round" strokeLinejoin="round">
      <path d={d} />
    </svg>
  );
}

const PIN_D = "M12 3v9m0 0-4-4m4 4 4-4M6 21h12"; // pin
const UNPIN_D = "M12 3v9m0 0-4-4m4 4 4-4M5 5l14 14M6 16h12";
const EDIT_D = "M4 20h16M13.5 6.5l4 4L8 20l-5 1 1-5 9.5-9.5Z";
const REVEAL_D = "M3 7h7l2 2h9v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7Z";
const WORKTREE_D = "M12 21V9M12 9l-3.5 3.5M12 9l3.5 3.5M4 4h16M4 4v3M20 4v3M6 21h12";
const ARCHIVE_D = "M4 5h16v4H4V5ZM4 9h16v10a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V9ZM10 13h4";
const REMOVE_D = "M4 7h16M9 7V5h6v2m-8 0 1 13h8l1-13";

const ITEMS: {
  action: ProjectAction;
  label: string;
  d: string;
  dividerBefore?: boolean;
}[] = [
  { action: "pin", label: "置顶", d: PIN_D },
  { action: "unpin", label: "取消置顶", d: UNPIN_D },
  { action: "edit", label: "编辑", d: EDIT_D },
  { action: "reveal", label: "在 Finder 中显示", d: REVEAL_D, dividerBefore: true },
  { action: "worktree", label: "创建永久工作树", d: WORKTREE_D },
  { action: "archive", label: "归档聊天", d: ARCHIVE_D, dividerBefore: true },
  { action: "remove", label: "移除项目", d: REMOVE_D, dividerBefore: true },
];

export function ProjectMenu({ pinned, disabled, onAction }: ProjectMenuProps) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const onDoc = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("pointerdown", onDoc);
    return () => document.removeEventListener("pointerdown", onDoc);
  }, []);

  const pick = (action: ProjectAction) => {
    setOpen(false);
    onAction(action);
  };

  const items = ITEMS.filter((it) => (pinned ? it.action !== "pin" : it.action !== "unpin"));

  return (
    <div className="project-menu-wrap" ref={ref}>
      <button
        type="button"
        className={`group-more-btn ${open ? "open" : ""}`}
        title="更多操作"
        aria-label="更多操作"
        aria-haspopup="menu"
        aria-expanded={open}
        disabled={disabled}
        onPointerDown={(e) => e.stopPropagation()}
        onClick={(e) => {
          e.stopPropagation();
          setOpen(!open);
        }}
      >
        <span aria-hidden="true">•••</span>
      </button>
      {open && (
        <div className="project-menu" role="menu" aria-label="项目操作">
          {items.map((it) => (
            <button
              type="button"
              key={it.action}
              role="menuitem"
              className={`project-menu-item ${it.dividerBefore ? "divider" : ""}`}
              disabled={disabled}
              onClick={() => pick(it.action)}
            >
              <Icon d={it.d} />
              <span>{it.label}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
