import { FormEvent, useMemo, useState } from "react";
import { api } from "../api";
import type { LibraryPrefs } from "../model";

type Props = {
  prefs: LibraryPrefs;
  busy: string | null;
  report: (message: string) => void;
  onChanged: (prefs: LibraryPrefs) => void;
};

export function SubscriptionsView({ prefs, busy, report, onChanged }: Props) {
  const [tab, setTab] = useState<"subs" | "queue">("subs");
  const [actorKey, setActorKey] = useState("");
  const [actorName, setActorName] = useState("");
  const [startDate, setStartDate] = useState(new Date().toISOString().slice(0, 10));
  const [maxCast, setMaxCast] = useState(3);

  const enabledCount = useMemo(
    () => prefs.subscriptions.filter((item) => item.enabled).length,
    [prefs.subscriptions]
  );
  const pendingQueue = useMemo(
    () => prefs.queue.filter((item) => item.status === "pending"),
    [prefs.queue]
  );

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!actorKey.trim() || !actorName.trim()) return;
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
  }

  return (
    <section className="panel-section">
      <div className="section-hero">
        <div>
          <p className="eyebrow">ACTOR TRACKING</p>
          <h1>订阅</h1>
          <p className="muted">{enabledCount} 位演员正在被追踪 · 队列待审 {pendingQueue.length}</p>
        </div>
        <div className="hero-actions">
          <button
            type="button"
            disabled={busy === "scan-subs"}
            onClick={() => {
              void (async () => {
                const result = await api.scanSubscriptions();
                const next = await api.libraryPrefs();
                onChanged(next);
                report(`扫描完成：新增队列 ${result.queued}，跳过暂停 ${result.skipped}`);
              })();
            }}
          >
            扫描新作
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
            <button type="submit">添加订阅</button>
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
                        void api.upsertSubscription({ ...sub, enabled: !sub.enabled }).then((next) => {
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
                        void api.removeSubscription(sub.actor_key).then((next) => {
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
            {prefs.queue.map((item) => (
              <article key={item.id} className="queue-card">
                <div>
                  <span className="pill">{item.status}</span>
                  <h2>{item.title}</h2>
                  <p>{[item.code, item.actor_name, item.release_date, `人数 ${item.cast_count}`].filter(Boolean).join(" · ")}</p>
                </div>
                <div className="subscription-actions">
                  {item.status === "pending" && (
                    <>
                      <button type="button" className="secondary" onClick={() => void api.patchQueueItem(item.id, "accepted").then(onChanged)}>接受</button>
                      <button type="button" className="ghost" onClick={() => void api.patchQueueItem(item.id, "dismissed").then(onChanged)}>忽略</button>
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
