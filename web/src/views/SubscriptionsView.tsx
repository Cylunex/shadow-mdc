import { useQuery } from "@tanstack/react-query";
import { FormEvent, useEffect, useMemo, useState } from "react";
import { api } from "../api";
import type { LibraryPrefs, SubscriptionWatchStatus } from "../model";
import { queryClient } from "../query/client";
import { resources } from "../query/resources";

type Props = {
  prefs: LibraryPrefs;
  busy: string | null;
  report: (message: string) => void;
  onChanged: (prefs: LibraryPrefs) => void;
};

export function SubscriptionsView({ prefs, busy, report, onChanged }: Props) {
  useQuery(resources.prefs(), queryClient);
  const [tab, setTab] = useState<"subs" | "queue">("subs");
  const [actorKey, setActorKey] = useState("");
  const [actorName, setActorName] = useState("");
  const [startDate, setStartDate] = useState(new Date().toISOString().slice(0, 10));
  const [maxCast, setMaxCast] = useState(3);
  const [localBusy, setLocalBusy] = useState<string | null>(null);
  const [watchStatus, setWatchStatus] = useState<SubscriptionWatchStatus | null>(null);
  const [fadingIds, setFadingIds] = useState<Record<string, true>>({});
  const [localQueue, setLocalQueue] = useState(prefs.queue);
  const blocked = Boolean(busy || localBusy);

  useEffect(() => {
    void api.subscriptionWatchStatus().then(setWatchStatus).catch(() => setWatchStatus(null));
  }, [prefs]);

  useEffect(() => {
    // Keep scroll position: merge server queue under any in-flight fade removals.
    setLocalQueue((current) => {
      const fading = new Set(Object.keys(fadingIds));
      if (fading.size === 0) return prefs.queue;
      const serverIds = new Set(prefs.queue.map((item) => item.id));
      const kept = current.filter((item) => fading.has(item.id) || serverIds.has(item.id));
      const extras = prefs.queue.filter((item) => !kept.some((row) => row.id === item.id));
      return [...kept.filter((item) => serverIds.has(item.id) || fading.has(item.id)), ...extras];
    });
  }, [prefs.queue, fadingIds]);

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
  const pendingQueue = useMemo(
    () => localQueue.filter((item) => item.status === "pending"),
    [localQueue]
  );

  async function dismissQueueItem(itemId: string, status: "accepted" | "dismissed") {
    const scrollY = window.scrollY;
    setFadingIds((current) => ({ ...current, [itemId]: true }));
    // Optimistic remove after a short fade; refill keeps remaining rows in place.
    window.setTimeout(() => {
      setLocalQueue((current) => current.filter((item) => item.id !== itemId));
      setFadingIds((current) => {
        const next = { ...current };
        delete next[itemId];
        return next;
      });
      window.requestAnimationFrame(() => window.scrollTo({ top: scrollY }));
    }, 180);
    try {
      const next = await api.patchQueueItem(itemId, status);
      onChanged(next);
      window.requestAnimationFrame(() => window.scrollTo({ top: scrollY }));
    } catch (error) {
      setFadingIds({});
      setLocalQueue(prefs.queue);
      report(error instanceof Error ? error.message : String(error));
    }
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!actorKey.trim() || !actorName.trim()) return;
    await runLocal("add-sub", async () => {
      const next = await api.upsertSubscription({
        actor_key: actorKey.trim(),
        actor_name: actorName.trim(),
        start_date: startDate,
        max_cast: maxCast,
        enabled: true
      });
      onChanged(next);
      report(`已订阅 ${actorName.trim()}（起始 ${startDate}，人数 ≤${maxCast}）`);
      setActorKey("");
      setActorName("");
    });
  }

  return (
    <section className="panel-section">
      <div className="section-hero">
        <div>
          <p className="eyebrow">ACTOR TRACKING</p>
          <h1>订阅</h1>
          <p className="muted">{enabledCount} 位演员正在被追踪 · 队列待审 {pendingQueue.length}</p>
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
        <button type="button" className={tab === "subs" ? "active warm" : ""} onClick={() => setTab("subs")}>
          演员订阅
        </button>
        <button type="button" className={tab === "queue" ? "active warm" : ""} onClick={() => setTab("queue")}>
          影片队列 ({pendingQueue.length})
        </button>
      </div>

      {tab === "subs" && (
        <>
          <form className="subscription-form" onSubmit={(event) => void submit(event)}>
            <input value={actorName} onChange={(e) => { setActorName(e.target.value); if (!actorKey) setActorKey(e.target.value); }} placeholder="演员姓名" required />
            <input value={actorKey} onChange={(e) => setActorKey(e.target.value)} placeholder="稳定键（可同姓名）" required />
            <input type="date" value={startDate} onChange={(e) => setStartDate(e.target.value)} required />
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
                      className="ghost"
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
                      className="danger ghost"
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
        localQueue.length === 0 ? (
          <div className="empty"><div>▤</div><h2>影片队列为空</h2><p>扫描订阅后，符合条件的作品会出现在这里供复核。手动下载类任务不会写入订阅。</p></div>
        ) : (
          <div className="queue-list">
            {localQueue.map((item) => (
              <article
                key={item.id}
                className={fadingIds[item.id] ? "queue-card queue-card--fading" : "queue-card"}
              >
                <div>
                  <span className="pill">{item.status}</span>
                  <h2>{item.title}</h2>
                  <p>{[item.code, item.actor_name, item.release_date, `人数 ${item.cast_count}`].filter(Boolean).join(" · ")}</p>
                </div>
                <div className="subscription-actions">
                  {item.status === "pending" && (
                    <>
                      <button type="button" className="secondary" disabled={blocked || Boolean(fadingIds[item.id])} onClick={() => void dismissQueueItem(item.id, "accepted")}>接受</button>
                      <button type="button" className="ghost" disabled={blocked || Boolean(fadingIds[item.id])} onClick={() => void dismissQueueItem(item.id, "dismissed")}>忽略</button>
                    </>
                  )}
                </div>
              </article>
            ))}
          </div>
        )
      )}
    </section>
  );
}
