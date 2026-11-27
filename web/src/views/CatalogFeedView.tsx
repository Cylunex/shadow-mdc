import { useEffect, useRef, useState } from "react";
import { api, appUrl } from "../api";
import { InAppPlayer, type PlayerSource } from "../components/InAppPlayer";
import type { CatalogFeedStart } from "./UnifiedCatalogView";

type Props = {
  initial: CatalogFeedStart;
  onState: (state: CatalogFeedStart) => void;
  onClose: () => void;
  onOpenWork: (id: string) => void;
};
export function CatalogFeedView({ initial, onState, onClose, onOpenWork }: Props) {
  const [items, setItems] = useState(initial.items);
  const [cursor, setCursor] = useState(initial.nextCursor);
  const [index, setIndex] = useState(initial.index ?? 0);
  const currentIndex = useRef(index);
  const [sources, setSources] = useState<PlayerSource[]>([]);
  const [error, setError] = useState("");
  const [sourceError, setSourceError] = useState("");
  const [sourceRetry, setSourceRetry] = useState(0);
  const [loading, setLoading] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const active = useRef(true);
  const pageRequest = useRef<AbortController | null>(null);
  useEffect(() => {
    active.current = true;
    return () => { active.current = false; pageRequest.current?.abort(); };
  }, []);
  const touchStart = useRef<number | null>(null);
  const lastGesture = useRef(0);
  const current = items[index];
  useEffect(() => {
    if (!current) return;
    const controller = new AbortController();
    setLoading(true); setSources([]); setError(""); setSourceError("");
    api.workPlayResolve(current.id, controller.signal).then(result => {
      if (controller.signal.aborted) return;
      const preferred = result.chosen || result.preferred;
      const list = [...result.sources];
      if (preferred?.url) {
        const idx = list.findIndex(item => item.url === preferred.url);
        if (idx > 0) {
          const [hit] = list.splice(idx, 1);
          if (hit) list.unshift(hit);
        }
      }
      setSources(list);
    }).catch(e => { if (!controller.signal.aborted) setSourceError(String(e)); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [current?.id, sourceRetry]);
  function move(delta: number) {
    const next = index + delta;
    if (next < 0 || next >= items.length) return;
    setIndex(next);
    currentIndex.current = next;
    onState({ ...initial, items, nextCursor: cursor, index: next });
  }
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      if (target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.tagName === "SELECT" || target.isContentEditable)) {
        return;
      }
      if (event.key === "Escape") {
        event.preventDefault();
        onClose();
      } else if (event.key === "ArrowDown" || event.key === "ArrowRight" || event.key === "j") {
        event.preventDefault();
        move(1);
      } else if (event.key === "ArrowUp" || event.key === "ArrowLeft" || event.key === "k") {
        event.preventDefault();
        move(-1);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });
  async function more() {
    if (!cursor || loadingMore) return;
    setLoadingMore(true); setError("");
    const controller = new AbortController();
    pageRequest.current = controller;
    try {
      const response = await fetch(appUrl("/api/catalog/works/query")!, {
        signal: controller.signal, method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...initial.filters, cursor })
      });
      if (!response.ok) throw new Error("下一页读取失败");
      const page: { items: CatalogFeedStart["items"]; next_cursor: string | null } = await response.json();
      if (!active.current) return;
      const next = [...new Map([...items, ...page.items].map(item => [item.id, item])).values()];
      setItems(next); setCursor(page.next_cursor);
      onState({ ...initial, items: next, nextCursor: page.next_cursor, index: currentIndex.current });
    } catch (e) { if (active.current && !controller.signal.aborted) setError(String(e)); }
    finally { if (active.current) setLoadingMore(false); }
  }
  return <section className="panel-section catalog-feed"
    onTouchStart={e => { touchStart.current = e.touches[0]?.clientY ?? null; }}
    onTouchEnd={e => {
      const end = e.changedTouches[0]?.clientY;
      if (touchStart.current !== null && end !== undefined && Math.abs(end - touchStart.current) > 70) {
        const target = e.target as HTMLElement;
        if (!target.closest("button,input,select,textarea")) move(end < touchStart.current ? 1 : -1);
      }
      touchStart.current = null;
    }}
    onWheel={e => {
      const target = e.target as HTMLElement;
      if (target.closest("button,input,select,textarea") || Math.abs(e.deltaY) < 35 ||
        Date.now() - lastGesture.current < 700) return;
      lastGesture.current = Date.now(); move(e.deltaY > 0 ? 1 : -1);
    }}>
    <header><button type="button" className="ghost" onClick={onClose}>← 返回筛选结果</button><span className="muted">{index + 1} / {items.length}</span></header>
    {current && <>
      <div className="catalog-feed-title-row">
        <div>
          <h1>{current.title}</h1>
          <p className="muted">{[current.code, current.year ?? "年份待补", current.actors.join(" / ")].filter(Boolean).join(" · ")}</p>
        </div>
        <button type="button" className="secondary" onClick={() => onOpenWork(current.id)}>作品详情</button>
      </div>
      {loading ? <p role="status">正在读取播放来源…</p> : sources.length > 0 ? <InAppPlayer
        key={current.id} title={current.title} playbackKey={"work:" + current.id} sources={sources}
        onRefreshSource={async source => {
          const result = await api.workPlayResolve(current.id);
          return result.sources.find(item => item.source_id === source.source_id &&
            item.version_id === source.version_id) ?? null;
        }}
        onClose={onClose} onReport={setError}
      /> : sourceError ? <p role="alert">{sourceError} <button type="button" className="ghost"
        onClick={() => setSourceRetry(value => value + 1)}>重试播放来源</button></p> : <p className="muted">暂无已配置的播放来源，可查看作品详情。</p>}
    </>}
    {error && <p role="alert">{error}</p>}
    <footer><button type="button" className="ghost" disabled={index === 0} onClick={() => move(-1)}>上一部</button>
      <button type="button" className="ghost" disabled={index >= items.length - 1} onClick={() => move(1)}>下一部</button>
      {cursor && <button type="button" className="secondary" disabled={loadingMore} onClick={() => void more()}>{loadingMore ? "正在追加…" : "继续加载同一筛选"}</button>}
    </footer>
  </section>;
}
