import { useQuery } from "@tanstack/react-query";
import { FormEvent, useEffect, useMemo, useState } from "react";
import { z } from "zod";
import { useSessionState } from "../hooks/useSessionState";
import { api } from "../api";
import type { LibraryPrefs, SubscriptionWatchStatus } from "../model";
import { queryClient } from "../query/client";
import { resources } from "../query/resources";

type Props = {
  prefs: LibraryPrefs;
  busy: string | null;
  report: (message: string) => void;
  onChanged: (prefs: LibraryPrefs) => void;
  onOpenWork?: (workId: string) => void;
};

const queueLabels: Record<string, string> = { pending: "待处理", accepted: "已接受", dismissed: "已忽略" };
const queueFilterSchema = z.enum(["pending", "accepted", "dismissed", "all"]);
const tabSchema = z.enum(["subs", "queue"]);

export function SubscriptionsView({ prefs, busy, report, onChanged, onOpenWork }: Props) {
  useQuery(resources.prefs(), queryClient);
  const [tab, setTab] = useSessionState("mdc:subscription-tab", tabSchema, "subs");
  const [actorKey, setActorKey] = useState("");
  const [actorName, setActorName] = useState("");
  const [keyTouched, setKeyTouched] = useState(false);
  const [startDate, setStartDate] = useState(new Date().toISOString().slice(0, 10));
  const [maxCast, setMaxCast] = useState(3);
  const [localBusy, setLocalBusy] = useState<string | null>(null);
  const [watchStatus, setWatchStatus] = useState<SubscriptionWatchStatus | null>(null);
  const [queueFilter, setQueueFilter] = useSessionState("mdc:queue-filter", queueFilterSchema, "pending");
  const blocked = Boolean(busy || localBusy);
  useEffect(() => {
    let active = true;
    void api.subscriptionWatchStatus().then(value => { if (active) setWatchStatus(value); })
      .catch(() => { if (active) setWatchStatus(null); });
    return () => { active = false; };
  }, [prefs]);

  async function runLocal(key: string, action: () => Promise<void>) {
    setLocalBusy(key);
    try {
      await action();
    } catch (error) {
      report(error instanceof Error ? error.message : String(error));
    } finally {
      setLocalBusy(null);
    }
  }

  const enabledCount = useMemo(
    () => prefs.subscriptions.filter((item) => item.enabled).length,
    [prefs.subscriptions]
  );
  const pendingCount = prefs.queue.filter(item => item.status === "pending").length;
  const visibleQueue = prefs.queue.filter(item => queueFilter === "all" || item.status === queueFilter);
  function dismissQueueItem(itemId: string, status: "accepted" | "dismissed") {
    return runLocal(itemId, async () => onChanged(await api.patchQueueItem(itemId, status)));
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    const resolvedName = actorName.trim();
    const resolvedKey = (actorKey.trim() || resolvedName);
    if (!resolvedKey || !resolvedName) return;
    await runLocal("add-sub", async () => {
      const next = await api.upsertSubscription({
        actor_key: resolvedKey,
        actor_name: resolvedName,
        start_date: startDate,
        max_cast: maxCast,
        enabled: true
      });
      onChanged(next);
      report(`已订阅 ${resolvedName}（起始 ${startDate}，人数 ≤${maxCast}）`);
      setActorKey("");
      setActorName("");
      setKeyTouched(false);
    });
  }

  return (
    <section className="panel-section">
      <div className="section-hero">
        <div>
          <p className="eyebrow">ACTOR TRACKING</p>
          <h1>订阅</h1>
          <p className="muted">{enabledCount} 位演员正在被追踪 · 队列待审 {pendingCount}</p>
          {watchStatus && (
            <p className="muted">
              自动盯磁链：{watchStatus.enabled ? "开" : "关"}
              {watchStatus.draining ? ` · 正在检查到期目标 ${watchStatus.batch_cursor}/${watchStatus.batch_total}` : ""}
              {watchStatus.last_check_at ? ` · 上次检查 ${watchStatus.last_check_at}（到期 ${watchStatus.last_due}）` : ""}
              {` · 按偏好搜磁链中 ${watchStatus.last_hunting} · 已加入 115 离线 ${watchStatus.last_queued}（本次新提交 ${watchStatus.last_submitted}）`}
              {watchStatus.last_skipped_pan ? ` · 网盘未就绪 ${watchStatus.last_skipped_pan}` : ""}
            </p>
          )}
        </div>
        <div className="hero-actions">
          <button
            type="button"
            disabled={blocked}
            onClick={() => {
              void runLocal("scan-subs", async () => {
                const result = await api.scanSubscriptions();
                const next = await queryClient.fetchQuery({ ...resources.prefs(), staleTime: 0 });
                onChanged(next);
                report(`扫描完成：新增队列 ${result.queued}，跳过暂停 ${result.skipped}`);
              });
            }}
          >
            {localBusy === "scan-subs" ? "扫描中…" : "扫描新作"}
          </button>
          <button
            type="button"
            className="secondary"
            disabled={blocked}
            onClick={() => {
              void runLocal("watch-once", async () => {
                const status = await api.runSubscriptionWatch();
                setWatchStatus(status);
                report(
                  `盯磁链检查完成：到期 ${status.last_due}，按偏好搜磁链中 ${status.last_hunting}，已加入 115 离线 ${status.last_queued}（新提交 ${status.last_submitted}），刷新磁链 ${status.last_refreshed}，网盘未就绪 ${status.last_skipped_pan}`
                );
              });
            }}
          >
            {localBusy === "watch-once" ? "检查中…" : "立即盯磁链"}
          </button>
        </div>
      </div>

      <div className="sub-tabs">
        <button type="button" aria-pressed={tab === "subs"} className={tab === "subs" ? "active warm" : ""} onClick={() => setTab("subs")}>
          演员订阅
        </button>
        <button type="button" aria-pressed={tab === "queue"} className={tab === "queue" ? "active warm" : ""} onClick={() => setTab("queue")}>
          影片队列 ({pendingCount})
        </button>
      </div>

      {tab === "subs" && (
        <>
          <form className="subscription-form" onSubmit={(event) => void submit(event)}>
            <input
              value={actorName}
              onChange={(e) => {
                const nextName = e.target.value;
                setActorName(nextName);
                if (!keyTouched) setActorKey(nextName);
              }}
              placeholder="演员姓名"
              required
            />
            <input
              value={actorKey}
              onChange={(e) => {
                const nextKey = e.target.value;
                setActorKey(nextKey);
                setKeyTouched(Boolean(nextKey.trim()) && nextKey !== actorName);
              }}
              placeholder="稳定键（留空默认同姓名）"
            />
            <input type="date" aria-label="订阅起始日期" value={startDate} onChange={(e) => setStartDate(e.target.value)} required />
            <label className="inline-field">
              <span>人数 ≤</span>
              <input type="number" min={1} max={50} value={maxCast} onChange={(e) => setMaxCast(Number(e.target.value) || 3)} />
            </label>
            <button type="submit" disabled={blocked}>添加订阅</button>
          </form>

          {prefs.subscriptions.length === 0 ? (
            <div className="empty"><div>✦</div><h2>还没有演员订阅</h2><p>订阅后可扫描片库中符合起始日期与人数上限的新作，进入队列复核。</p></div>
          ) : (
            <div className="subscription-list">
              {prefs.subscriptions.map((sub) => (
                <article key={sub.actor_key} className="subscription-card">
                  <div className="subscription-avatar" aria-hidden>{sub.actor_name.slice(0, 1)}</div>
                  <div>
                    <h2>{sub.actor_name}</h2>
                    <div className="meta-pills">
                      <span>起始: {sub.start_date}</span>
                      <span>人数: ≤{sub.max_cast}人</span>
                      <span className={sub.enabled ? "on" : "off"}>{sub.enabled ? "已启用" : "已暂停"}</span>
                    </div>
                  </div>
                  <div className="subscription-actions">
                    <button
                      type="button"
                      className="ghost" disabled={blocked}
                      onClick={() => {
                        void runLocal(`toggle-${sub.actor_key}`, async () => {
                          const next = await api.upsertSubscription({ ...sub, enabled: !sub.enabled });
                          onChanged(next);
                          report(sub.enabled ? `已暂停 ${sub.actor_name}` : `已启用 ${sub.actor_name}`);
                        });
                      }}
                    >
                      {sub.enabled ? "暂停" : "启用"}
                    </button>
                    <button
                      type="button"
                      className="danger ghost" disabled={blocked}
                      onClick={() => {
                        void runLocal(`remove-${sub.actor_key}`, async () => {
                          const next = await api.removeSubscription(sub.actor_key);
                          onChanged(next);
                          report(`已取消订阅 ${sub.actor_name}`);
                        });
                      }}
                    >
                      取消订阅
                    </button>
                  </div>
                </article>
              ))}
            </div>
          )}
        </>
      )}

      {tab === "queue" && (
        prefs.queue.length === 0 ? (
          <div className="empty"><div>▤</div><h2>影片队列为空</h2><p>扫描订阅后，符合条件的作品会出现在这里供复核。手动下载类任务不会写入订阅。</p></div>
        ) : (
          <div className="queue-list">
            <label>队列状态 <select value={queueFilter} onChange={event => setQueueFilter(queueFilterSchema.parse(event.target.value))}>
              <option value="pending">待处理 ({pendingCount})</option><option value="accepted">已接受</option>
              <option value="dismissed">已忽略</option><option value="all">全部记录</option>
            </select></label>
            {visibleQueue.length === 0 && <p className="muted">当前状态没有影片。</p>}
            {visibleQueue.map((item) => (
              <article
                key={item.id}
                className="queue-card" aria-busy={localBusy === item.id}
              >
                {(() => {
                  const queuedWorkId = item.work_id;
                  return (
                    <>
                      <div>
                        <span className="pill">{queueLabels[item.status] ?? item.status}</span>
                        {queuedWorkId && onOpenWork ? (
                          <button
                            type="button"
                            className="queue-card-title-btn"
                            onClick={() => onOpenWork(queuedWorkId)}
                            title="打开作品详情"
                          >
                            <h2>{item.title}</h2>
                          </button>
                        ) : (
                          <h2>{item.title}</h2>
                        )}
                        <p>{[item.code, item.actor_name, item.release_date, `人数 ${item.cast_count}`].filter(Boolean).join(" · ")}</p>
                      </div>
                      <div className="subscription-actions">
                        {queuedWorkId && onOpenWork && (
                          <button type="button" className="ghost" onClick={() => onOpenWork(queuedWorkId)}>详情</button>
                        )}
                        {item.status === "pending" && (
                          <>
                            <button type="button" className="secondary" disabled={blocked} onClick={() => void dismissQueueItem(item.id, "accepted")}>接受</button>
                            <button type="button" className="ghost" disabled={blocked} onClick={() => void dismissQueueItem(item.id, "dismissed")}>忽略</button>
                          </>
                        )}
                      </div>
                    </>
                  );
                })()}
              </article>
            ))}
          </div>
        )
      )}
    </section>
  );
}
