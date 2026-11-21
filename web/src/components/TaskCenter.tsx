import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { queryClient } from "../query/client";
import { resources } from "../query/resources";
import type { TaskRun } from "../model";
import { api } from "../api";
import { useTaskUpdates } from "../hooks/useTaskUpdates";
import { offlineDirectoryLabel, offlineTaskLabel } from "./OfflineDownloadDialog";

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
  const [offlineFilter, setOfflineFilter] = useState("all");
  const [offlineAction, setOfflineAction] = useState<string | null>(null);
  const offline = useQuery({ ...resources.offlineList(), refetchInterval: autoRefresh ? 5_000 : false }, queryClient);
  const offlineTasks = offline.data ?? [];
  const visibleOffline = offlineTasks.filter((task) => offlineFilter === "all" ||
    task.status === offlineFilter || (offlineFilter === "done" && task.status === "completed"));

  const kinds = useMemo(() => ["all", ...Array.from(new Set(tasks.map((task) => task.kind))).sort()], [tasks]);
  const visible = useMemo(() => kindFilter === "all" ? tasks : tasks.filter((task) => task.kind === kindFilter), [tasks, kindFilter]);

  async function controlOffline(id: string, action: "cancel" | "next") {
    setOfflineAction(id);
    try {
      const task = await (action === "cancel" ? api.panOfflineCancel(id) : api.panOfflineNext(id));
      report(`${offlineTaskLabel(task)} · ${offlineDirectoryLabel(task)}`);
    } catch (error) {
      report(error instanceof Error ? error.message : String(error));
    } finally {
      setOfflineAction(null);
    }
  }

  const offlinePanel = offlineTasks.length > 0 || offline.isError ? (
    <section className="offline-task-panel" aria-label="离线任务">
      <div className="work-related-head">
        <h2>离线下载</h2>
        <label>
          <select aria-label="筛选离线任务状态" value={offlineFilter} onChange={(event) => setOfflineFilter(event.target.value)}>
            <option value="all">最近全部</option>
            <option value="running">进行中</option>
            <option value="done">已完成</option>
            <option value="failed">失败</option>
            <option value="cancelled">已取消</option>
          </select>
        </label>
      </div>
      {offline.isError && <div className="offline-directory-actions">
        <small role="alert" className="danger-text">{offline.error.message}</small>
        <button type="button" className="ghost" onClick={() => void offline.refetch()}>刷新</button>
      </div>}
      {!visibleOffline.length && !offline.isError && <p className="muted">最近任务中没有该状态的记录。</p>}
      <div className="magnet-list">
        {visibleOffline.map((task) => (
          <article className="offline-task-row" key={task.id}>
            <strong>{task.remote_name || task.info_hash.slice(0, 16)}</strong>
            <small className="muted">
              {offlineTaskLabel(task)}
              {task.attempt_count && task.attempt_count > 1 ? ` · 第${task.attempt_count}次` : ""}
            </small>
            <small className="muted">{offlineDirectoryLabel(task)}</small>
            {task.error ? <small className="danger-text">{task.error}</small> : null}
            {(task.can_cancel || task.can_switch) && (
              <div className="offline-directory-actions">
                {task.can_switch && <button type="button" className="ghost" disabled={offlineAction !== null}
                  onClick={() => void controlOffline(task.id, "next")}>换磁力</button>}
                {task.can_cancel && !["cancelling", "switching", "submitting"].includes(task.download_state ?? "") && (
                  <button type="button" className="ghost" disabled={offlineAction !== null}
                    onClick={() => void controlOffline(task.id, "cancel")}>取消下载</button>
                )}
              </div>
            )}
          </article>
        ))}
      </div>
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
