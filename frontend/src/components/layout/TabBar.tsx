import type { StreamActivityMap } from "../../useChatStream";

export interface OpenTab {
  id: string;
  title: string;
  /** The unsaved new-session tab: it has no id to close and no running turn. */
  draft?: boolean;
}

interface Props {
  tabs: OpenTab[];
  currentId: string | null;
  activity: StreamActivityMap;
  onSelect: (id: string) => void;
  onClose: (id: string) => void;
  onNew: () => void;
}

export function TabBar({ tabs, currentId, activity, onSelect, onClose, onNew }: Props) {
  return (
    <div className="tab-bar">
      <div className="tab-strip" role="tablist" aria-label="打开的会话">
        {tabs.map((tab) => {
          const act = activity[tab.id];
          // The draft tab is the foreground exactly while no session is open.
          const active = tab.draft ? currentId === null : tab.id === currentId;
          return (
            <div key={tab.id} className={`tab${active ? " active" : ""}`}>
              <button
                type="button"
                role="tab"
                aria-selected={active}
                className="tab-label"
                title={tab.title}
                onClick={() => onSelect(tab.id)}
              >
                <span className={`tab-run${act?.busy ? " on" : ""}`} aria-hidden="true" />
                <span className="tab-title">{tab.title}</span>
                {act?.approvals ? (
                  <span className="tab-badge" title={`${act.approvals} 个待批准`}>
                    {act.approvals}
                  </span>
                ) : null}
              </button>
              {!tab.draft && (
                <button
                  type="button"
                  className="tab-close"
                  aria-label={`关闭 ${tab.title}`}
                  title="关闭标签"
                  onClick={() => onClose(tab.id)}
                >
                  <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
                    <path d="M6 6l12 12M18 6L6 18" />
                  </svg>
                </button>
              )}
            </div>
          );
        })}
      </div>
      <button
        type="button"
        className="tab-new"
        aria-label="新建会话"
        title="在默认工作区新建会话"
        onClick={onNew}
      >
        <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
          <path d="M12 5v14M5 12h14" />
        </svg>
      </button>
    </div>
  );
}
