import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { queryClient } from "../query/client";
import { resources } from "../query/resources";
import type { TaskRun } from "../model";
import { api } from "../api";
import { useTaskUpdates } from "../hooks/useTaskUpdates";

type Props = {
  tasks: TaskRun[];
  busy: string | null;
  onChanged: () => Promise<void>;
  report: (message: string) => void;
  onTasksSnapshot?: (tasks: TaskRun[]) => void;
};

export function TaskCenter({ tasks, busy, onChanged, report, onTasksSnapshot }: Props) {
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [replay, setReplay] = useState<Awaited<ReturnType<typeof api.matchingReplay>>>();
  const [replayLoading, setReplayLoading] = useState(false);
  const live = useTaskUpdates({ enabled: autoRefresh, onChanged, onTasksSnapshot });
  const [kindFilter, setKindFilter] = useState("all");
  const offline = useQuery({ ...resources.offlineList(), refetchInterval: autoRefresh ? 5_000 : false }, queryClient);
  const offlineTasks = offline.data ?? [];

  const kinds = useMemo(() => ["all", ...Array.from(new Set(tasks.map((task) => task.kind))).sort()], [tasks]);
  const visible = useMemo(() => kindFilter === "all" ? tasks : tasks.filter((task) => task.kind === kindFilter), [tasks, kindFilter]);

  const offlinePanel = offlineTasks.length > 0 ? (
    <section className="offline-task-panel" aria-label="115 离线任务">
      <div className="work-related-head">
        <h2>115 离线 / STRM</h2>
        <span className="muted">{offlineTasks.length}</span>
      </div>
      <div className="magnet-list">
        {offlineTasks.slice(0, 10).map((task) => (
          <div className="magnet-row" key={task.id}>
            <span>{task.remote_name || task.info_hash.slice(0, 16)}</span>
            <small className="muted">
              {task.status}
              {task.download_state ? `/${task.download_state}` : ""}
              {" · "}
              {Math.round(task.progress || 0)}%
              {task.attempt_count && task.attempt_count > 1 ? ` · 第${task.attempt_count}次` : ""}
              {task.switch_reason ? ` · ${task.switch_reason}` : ""}
            </small>
            {task.strm_path ? <small className="muted">STRM 已写</small> : null}
            {task.error ? <small className="danger-text">{task.error}</small> : null}
          </div>
        ))}
      </div>
      <p className="muted">完整配置与刷新在「设置 → 115 / OpenList」。</p>
    </section>
  ) : null;

  const replayPanel = <>
    <button type="button" disabled={replayLoading} onClick={() => {
        setReplayLoading(true);
        api.matchingReplay().then(setReplay).catch(error => report(String(error)))
          .finally(() => setReplayLoading(false));
      }}>回放人工确认样本</button>
      {replay && <div aria-label="识别回放结果">
        {([["有番号", replay.code], ["无番号", replay.without_code]] as const).map(([label, counts]) =>
          <p key={label}>{label} {counts.samples} 个样本，需人工确认 {counts.manual_review} 个，
            自动匹配 {counts.automatic} 个，其中与人工选择不一致 {counts.disagreed} 个。</p>)}
      </div>}
  </>;

  if (tasks.length === 0) {
    return (
      <div className="task-center">
        {replayPanel}
      {offlinePanel}
        <p className="empty-detail">还没有运行记录。扫描、识别、翻译和整理会显示在这里，可取消。失败的扫描和批量识别可以重试。</p>
      </div>
    );
  }

  return (
    <div className="task-center">
      {offlinePanel}
      {replayPanel}
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
                {task.finished_at && ["scan", "identify:library"].includes(task.kind) &&
                  ["failed", "partial", "cancelled", "interrupted"].includes(task.status) && (
                  <button
                    type="button"
                    disabled={busy === `retry-${task.id}`}
                    onClick={() => {
                      void (async () => {
                        try {
                          await api.retryTask(task.id);
                          report(`已提交重试 ${task.kind}`);
                          await onChanged();
                        } catch (error) {
                          report(error instanceof Error ? error.message : String(error));
                        }
                      })();
                    }}
                  >
                    重试
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
