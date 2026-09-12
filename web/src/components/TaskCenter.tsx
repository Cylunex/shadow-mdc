import { useEffect, useMemo, useState } from "react";
import type { TaskRun } from "../model";
import { api, appUrl } from "../api";
import { taskRunsSchema } from "../model";

type Props = {
  tasks: TaskRun[];
  busy: string | null;
  onChanged: () => Promise<void>;
  report: (message: string) => void;
  onTasksSnapshot?: (tasks: TaskRun[]) => void;
};

export function TaskCenter({ tasks, busy, onChanged, report, onTasksSnapshot }: Props) {
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [live, setLive] = useState(false);
  const [kindFilter, setKindFilter] = useState("all");
  const kinds = useMemo(() => ["all", ...Array.from(new Set(tasks.map((task) => task.kind))).sort()], [tasks]);
  const visible = useMemo(() => kindFilter === "all" ? tasks : tasks.filter((task) => task.kind === kindFilter), [tasks, kindFilter]);

  useEffect(() => {
    if (!autoRefresh) return;
    const url = appUrl("/api/tasks/events");
    if (!url || typeof EventSource === "undefined") {
      const hasRunning = tasks.some((task) => !task.finished_at && task.status !== "succeeded");
      if (!hasRunning) return;
      const timer = window.setInterval(() => {
        void onChanged();
      }, 2500);
      return () => window.clearInterval(timer);
    }
    const source = new EventSource(url);
    source.addEventListener("tasks", (event) => {
      try {
        const payload = taskRunsSchema.parse(JSON.parse((event as MessageEvent).data));
        setLive(true);
        onTasksSnapshot?.(payload);
      } catch {
        void onChanged();
      }
    });
    source.onerror = () => {
      setLive(false);
    };
    return () => source.close();
  }, [autoRefresh, onChanged, onTasksSnapshot, tasks]);

  if (tasks.length === 0) {
    return <p className="empty-detail">还没有运行记录。扫描、识别、翻译和整理会显示在这里，可取消或登记重试。</p>;
  }

  return (
    <div className="task-center">
      <div className="task-center-toolbar">
        <label>
          类型
          <select value={kindFilter} onChange={(e) => setKindFilter(e.target.value)} aria-label="按任务类型筛选">
            {kinds.map((kind) => <option key={kind} value={kind}>{kind === "all" ? "全部类型" : kind}</option>)}
          </select>
        </label>
        <small className="muted">手动下载类任务只进进度，不写入订阅。</small>
        <label>
          <input type="checkbox" checked={autoRefresh} onChange={(e) => setAutoRefresh(e.target.checked)} />
          实时同步进行中的任务{live ? "（SSE）" : "（轮询回退）"}
        </label>
      </div>
      <div className="task-list">
        {visible.map((task) => {
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
