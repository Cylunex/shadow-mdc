import { useEffect, useState } from "react";
import type { TaskRun } from "../model";
import { api } from "../api";

type Props = {
  tasks: TaskRun[];
  busy: string | null;
  onChanged: () => Promise<void>;
  report: (message: string) => void;
};

export function TaskCenter({ tasks, busy, onChanged, report }: Props) {
  const [autoRefresh, setAutoRefresh] = useState(true);
  useEffect(() => {
    if (!autoRefresh) return;
    const hasRunning = tasks.some((task) => !task.finished_at && task.status !== "succeeded");
    if (!hasRunning) return;
    const timer = window.setInterval(() => {
      void onChanged();
    }, 2500);
    return () => window.clearInterval(timer);
  }, [autoRefresh, tasks, onChanged]);

  if (tasks.length === 0) {
    return <p className="empty-detail">还没有运行记录。扫描、识别、翻译和整理会显示在这里，可取消或登记重试。</p>;
  }

  return (
    <div className="task-center">
      <div className="task-center-toolbar">
        <label>
          <input type="checkbox" checked={autoRefresh} onChange={(e) => setAutoRefresh(e.target.checked)} />
          自动刷新进行中的任务
        </label>
      </div>
      <div className="task-list">
        {tasks.map((task) => {
          const summary = task.summary || {};
          const current = Number(summary.progress_current ?? 0);
          const total = Number(summary.progress_total ?? 0);
          const pct = total > 0 ? Math.min(100, Math.round((current / total) * 100)) : null;
          return (
            <article key={task.id}>
              <div>
                <span className={`task-state ${task.status}`}>{task.status}</span>
                <h2>{task.kind}</h2>
                <p>{task.scope}</p>
                {pct !== null && (
                  <div className="task-progress" aria-label="progress">
                    <div style={{ width: `${pct}%` }} />
                    <span>
                      {current}/{total} ({pct}%)
                    </span>
                  </div>
                )}
              </div>
              <code>{JSON.stringify(summary)}</code>
              <div className="task-actions">
                {!task.finished_at && (
                  <button
                    type="button"
                    disabled={busy === `cancel-${task.id}`}
                    onClick={() => {
                      void (async () => {
                        try {
                          await api.cancelTask(task.id);
                          report(`已请求取消 ${task.kind}`);
                          await onChanged();
                        } catch (error) {
                          report(error instanceof Error ? error.message : String(error));
                        }
                      })();
                    }}
                  >
                    取消
                  </button>
                )}
                {task.finished_at && (
                  <button
                    type="button"
                    disabled={busy === `retry-${task.id}`}
                    onClick={() => {
                      void (async () => {
                        try {
                          await api.retryTask(task.id);
                          report(`已登记重试 ${task.kind}，请再次执行对应操作`);
                          await onChanged();
                        } catch (error) {
                          report(error instanceof Error ? error.message : String(error));
                        }
                      })();
                    }}
                  >
                    登记重试
                  </button>
                )}
                <time>{new Date(task.created_at).toLocaleString()}</time>
              </div>
            </article>
          );
        })}
      </div>
    </div>
  );
}
