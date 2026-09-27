import { useEffect, useLayoutEffect, useRef, useState } from "react";

export interface ProjectMenuProps {
  pinned: boolean;
  /** True when this project has a conversation with a turn running. */
  running: boolean;
  onAction: (action: ProjectAction) => void;
}

export type ProjectAction =
  | "pin"
  | "unpin"
  | "edit"
  | "mcp"
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
const MCP_D = "M12 3v6m0 0-3 3v9m3-12 3 3v9M4 6h4M16 6h4M9 18h6";

const ITEMS: {
  action: ProjectAction;
  label: string;
  d: string;
  dividerBefore?: boolean;
  /** Needs every conversation of the project to be idle (the backend holds
   *  each session's lock for the whole of a turn). */
  needsIdle?: boolean;
}[] = [
  { action: "pin", label: "置顶", d: PIN_D },
  { action: "unpin", label: "取消置顶", d: UNPIN_D },
  { action: "edit", label: "编辑", d: EDIT_D },
  { action: "mcp", label: "MCP 服务", d: MCP_D },
  { action: "reveal", label: "在 Finder 中显示", d: REVEAL_D, dividerBefore: true },
  { action: "worktree", label: "创建永久工作树", d: WORKTREE_D },
  { action: "archive", label: "归档聊天", d: ARCHIVE_D, dividerBefore: true, needsIdle: true },
  { action: "remove", label: "移除项目", d: REMOVE_D, dividerBefore: true },
];

export function ProjectMenu({ pinned, running, onAction }: ProjectMenuProps) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const [pos, setPos] = useState<{ left: number; top: number } | null>(null);

  useLayoutEffect(() => {
    // 只在打开时测量定位；关闭后的陈旧 pos 不可见（菜单受 open 门控），
    // 重开时本 effect 会同步重测，故无需在关闭路径 setState（避免级联渲染）。
    if (!open) return;
    const btn = ref.current?.querySelector(".group-more-btn");
    const menu = menuRef.current;
    if (!btn || !menu) return;
    const b = btn.getBoundingClientRect();
    const m = menu.getBoundingClientRect();
    const left = Math.min(
      Math.max(8, b.right - m.width),
      Math.max(8, window.innerWidth - m.width - 8)
    );
    const top = Math.min(
      Math.max(8, b.bottom + 4),
      Math.max(8, window.innerHeight - m.height - 8)
    );
    setPos({ left, top });
  }, [open]);

  useEffect(() => {
    const onDoc = (e: PointerEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    const onEsc = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("pointerdown", onDoc);
    document.addEventListener("keydown", onEsc);
    return () => {
      document.removeEventListener("pointerdown", onDoc);
      document.removeEventListener("keydown", onEsc);
    };
  }, []);

  // While the menu is open, a scroll (capture: also fires for inner
  // overflow containers) or a window resize would leave the position:fixed menu
  // drifting away from its trigger. Closing on either is more robust than
  // recomputing the coordinates, so the popup never floats misaligned.
  useEffect(() => {
    if (!open) return;
    const close = () => setOpen(false);
    window.addEventListener("scroll", close, true);
    window.addEventListener("resize", close);
    return () => {
      window.removeEventListener("scroll", close, true);
      window.removeEventListener("resize", close);
    };
  }, [open]);

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
        onPointerDown={(e) => e.stopPropagation()}
        onClick={(e) => {
          e.stopPropagation();
          setOpen((prev) => !prev);
        }}
      >
        <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
          <circle cx="5.5" cy="12" r="1.7" />
          <circle cx="12" cy="12" r="1.7" />
          <circle cx="18.5" cy="12" r="1.7" />
        </svg>
      </button>
      {open && (
        <div
          ref={menuRef}
          className="project-menu project-action-menu"
          role="menu"
          aria-label="项目操作"
          style={{
            position: "fixed",
            left: pos?.left ?? 0,
            top: pos?.top ?? 0,
            zIndex: 400,
            visibility: pos ? "visible" : "hidden",
          }}
        >
          {items.map((it) => {
            // Only the actions a running turn really blocks are gated: the
            // rest stay usable while another conversation is working.
            const blocked = it.needsIdle && running;
            return (
              <button
                type="button"
                key={it.action}
                role="menuitem"
                className={`project-menu-item ${it.dividerBefore ? "divider" : ""}`}
                disabled={blocked}
                title={blocked ? "该项目有会话正在运行，先停止后再归档" : undefined}
                onClick={() => pick(it.action)}
              >
                <Icon d={it.d} />
                <span>{it.label}</span>
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}
