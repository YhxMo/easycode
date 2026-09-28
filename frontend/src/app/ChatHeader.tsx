import type { RefObject } from "react";
import { TabBar, type OpenTab } from "../features/sidebar/TabBar";
import type { StreamActivityMap } from "../features/chat/useChatStream";

/**
 * The row above the conversation: the open tabs, and the buttons that belong to
 * the window rather than to any one conversation.
 *
 * Presentational — what "busy" means, which tab is open and whether the pane is
 * showing are all decided above; this only lays them out.
 */
export function ChatHeader({
  tabs,
  currentId,
  activity,
  onSelectTab,
  onCloseTab,
  onNewTab,
  sidebarOpen,
  onOpenSidebar,
  busy,
  pendingApproval,
  onRevealApproval,
  paneOpen,
  onTogglePane,
  paneToggleRef,
}: {
  tabs: OpenTab[];
  currentId: string | null;
  activity: StreamActivityMap;
  onSelectTab: (id: string | null) => void;
  onCloseTab: (id: string) => void;
  onNewTab: () => void;
  sidebarOpen: boolean;
  onOpenSidebar: () => void;
  busy: boolean;
  pendingApproval: boolean;
  onRevealApproval: () => void;
  paneOpen: boolean;
  onTogglePane: () => void;
  paneToggleRef: RefObject<HTMLButtonElement | null>;
}) {
  return (
    <div className="chat-top">
      <TabBar
        tabs={tabs}
        currentId={currentId}
        activity={activity}
        onSelect={onSelectTab}
        onClose={onCloseTab}
        onNew={onNewTab}
      />
      <div className="chat-actions">
        <button
          type="button"
          className="sidebar-toggle"
          aria-label="打开侧栏"
          aria-controls="sidebar"
          aria-expanded={sidebarOpen}
          title="打开侧栏"
          onClick={onOpenSidebar}
        >
          <span aria-hidden="true">☰</span>
        </button>
        <span
          className={`connection-state ${busy ? "working" : ""} ${pendingApproval ? "clickable" : ""}`}
          role={pendingApproval ? "button" : undefined}
          title={pendingApproval ? "查看待批准的操作" : undefined}
          onClick={pendingApproval ? onRevealApproval : undefined}
        >
          <span aria-hidden="true" />
          {busy ? (pendingApproval ? "等待批准" : "正在工作") : "就绪"}
        </span>
        <button
          type="button"
          className={`icon-btn${paneOpen ? " on" : ""}`}
          aria-label={paneOpen ? "收起面板" : "展开面板"}
          aria-pressed={paneOpen}
          title={paneOpen ? "收起面板" : "展开面板"}
          ref={paneToggleRef}
          onClick={onTogglePane}
        >
          <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
            <rect x="3.5" y="4.5" width="17" height="15" rx="2.5" />
            <path d="M14.5 4.5v15" />
          </svg>
        </button>
      </div>
    </div>
  );
}
