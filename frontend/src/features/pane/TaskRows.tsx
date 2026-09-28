import type { TodoItem } from "../../types";

const LABELS: Record<TodoItem["status"], string> = {
  pending: "待开始",
  in_progress: "进行中",
  completed: "已完成",
};

function StatusMark({ status, index }: { status: TodoItem["status"]; index: number }) {
  if (status === "completed") {
    return (
      <svg className="task-mark done" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
        <path d="m5 12.5 4.5 4.5L19 7" />
      </svg>
    );
  }
  if (status === "in_progress") {
    return (
      <span className="task-mark running" aria-hidden="true">
        {index}
      </span>
    );
  }
  return (
    <span className="task-mark pending" aria-hidden="true">
      {index}
    </span>
  );
}

function counts(todos: TodoItem[]) {
  return { done: todos.filter((t) => t.status === "completed").length, total: todos.length };
}

/** The task list the model maintains, for the pane's 任务 section. */
export function TaskRows({ todos, stale = false }: { todos: TodoItem[]; stale?: boolean }) {
  if (!todos.length) {
    return <p className="pane-empty">这一轮还没有任务清单。多步任务会在这里列出来。</p>;
  }
  const { done, total } = counts(todos);
  return (
    <div className="task-list">
      <div className="pane-group-title">
        任务清单
        {/* The list outlives the turn that wrote it; without this the pane reads
            as if it were the progress of whatever is running now. */}
        {stale && <span className="task-stale">上次清单</span>}
        <span className="pane-count tabular">
          {done}/{total} 完成
        </span>
      </div>
      <ol className="task-rows">
        {todos.map((todo, index) => (
          <li key={`${index}-${todo.text}`} className={`task-row ${todo.status}`}>
            <StatusMark status={todo.status} index={index + 1} />
            <span className="task-text">{todo.text}</span>
            <span className={`task-pill ${todo.status}`}>{LABELS[todo.status]}</span>
          </li>
        ))}
      </ol>
    </div>
  );
}

/** The compact line the message stream carries, opening the pane's list. */
export function TaskSummary({ todos, onOpen }: { todos: TodoItem[]; onOpen: () => void }) {
  const { done, total } = counts(todos);
  if (!total) return null;
  return (
    <button type="button" className="task-summary" onClick={onOpen} title="在右侧面板查看任务清单">
      <svg className="task-summary-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
        <path d="M5 6.5h14M5 12h14M5 17.5h9" />
      </svg>
      <span className="task-summary-label">任务清单</span>
      <span className="task-summary-bar" aria-hidden="true">
        <span style={{ width: `${total ? (done / total) * 100 : 0}%` }} />
      </span>
      <span className="task-summary-count tabular">
        {done}/{total}
      </span>
    </button>
  );
}
