import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { api } from "../api";
import { CoverImage } from "../components/CoverImage";
import { mediaUrl } from "../lib/mediaUrl";
import type { Work } from "../model";

type Props = {
  works: Work[];
  loading: boolean;
  onOpenDetail: (workId: string) => void;
  report: (message: string) => void;
};

function shuffle<T>(items: readonly T[]): T[] {
  const next = [...items];
  for (let i = next.length - 1; i > 0; i -= 1) {
    const j = Math.floor(Math.random() * (i + 1));
    const a = next[i];
    const b = next[j];
    if (a === undefined || b === undefined) continue;
    next[i] = b;
    next[j] = a;
  }
  return next;
}

function rankWorks(works: readonly Work[]): Work[] {
  // Prefer titles with a poster, then local media, then randomize within buckets.
  const withPoster = works.filter((w) => Boolean(w.image_url));
  const without = works.filter((w) => !w.image_url);
  const sortBucket = (bucket: Work[]) => {
    const local = bucket.filter((w) => w.has_local_media);
    const remote = bucket.filter((w) => !w.has_local_media);
    return [...shuffle(local), ...shuffle(remote)];
  };
  return [...sortBucket(withPoster), ...sortBucket(without)];
}

async function openPlay(workId: string, report: (message: string) => void): Promise<void> {
  try {
    const resolved = await api.workPlayResolve(workId);
    const preferred = resolved.preferred;
    if (preferred?.playable_in_app) {
      window.open(api.workPlayerUrl(workId), "_blank", "noopener,noreferrer");
      return;
    }
    if (preferred?.url) {
      window.open(preferred.url, "_blank", "noopener,noreferrer");
      return;
    }
    if ((resolved.sources ?? []).length > 0) {
      window.open(api.workPlayerUrl(workId), "_blank", "noopener,noreferrer");
      return;
    }
    report("暂无可用片源（需 Emby 或本地 playback-sites）");
  } catch (err) {
    report(err instanceof Error ? err.message : String(err));
  }
}

export function SwipeFeedView({ works, loading, onOpenDetail, report }: Props) {
  const [orderKey, setOrderKey] = useState(0);
  const [playingId, setPlayingId] = useState<string | null>(null);
  const scrollerRef = useRef<HTMLDivElement | null>(null);

  const feed = useMemo(() => rankWorks(works), [works, orderKey]);

  const reshuffle = useCallback(() => {
    setOrderKey((value) => value + 1);
    requestAnimationFrame(() => {
      scrollerRef.current?.scrollTo({ top: 0 });
    });
  }, []);

  useEffect(() => {
    const el = scrollerRef.current;
    if (!el) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "ArrowDown" && event.key !== "ArrowUp" && event.key !== "j" && event.key !== "k") return;
      const target = event.target as HTMLElement | null;
      if (target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.isContentEditable)) return;
      event.preventDefault();
      const height = el.clientHeight || window.innerHeight;
      const dir = event.key === "ArrowDown" || event.key === "j" ? 1 : -1;
      el.scrollBy({ top: dir * height, behavior: "smooth" });
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  if (loading) {
    return (
      <section className="swipe-feed swipe-feed--loading" aria-busy="true" aria-label="刷流加载中">
        <p className="muted">正在加载片库…</p>
      </section>
    );
  }

  if (feed.length === 0) {
    return (
      <section className="swipe-feed swipe-feed--empty">
        <p>片库为空。先在「影片」里扫库或导入作品。</p>
      </section>
    );
  }

  return (
    <section className="swipe-feed" aria-label="片库刷流">
      <div className="swipe-feed-toolbar">
        <span className="swipe-feed-hint">上下滑切换 · J/K 或 ↑↓ · 共 {feed.length} 部</span>
        <button type="button" className="ghost" onClick={reshuffle}>打乱</button>
      </div>
      <div className="swipe-feed-scroller" ref={scrollerRef}>
        {feed.map((work, index) => {
          const thumb = mediaUrl(work.image_url);
          const code = work.primary_code || work.id.slice(0, 8);
          const title = work.title || code;
          return (
            <article
              key={`${work.id}-${orderKey}`}
              className="swipe-card"
              data-index={index}
              id={`swipe-card-${work.id}`}
            >
              <div className="swipe-card-stage">
                {thumb ? (
                  <CoverImage src={thumb} className="swipe-card-cover" loading={index < 3 ? "eager" : "lazy"} />
                ) : (
                  <div className="poster-fallback swipe-card-fallback" aria-hidden />
                )}
                <div className="swipe-card-gradient" aria-hidden />
                <div className="swipe-card-hud">
                  <div className="swipe-card-code">{code}</div>
                  <h2 className="swipe-card-title">{title}</h2>
                  <p className="swipe-card-meta">
                    {[work.studio, (work.actors ?? []).slice(0, 3).join(" / "), work.has_local_media ? "本地媒资" : null]
                      .filter(Boolean)
                      .join(" · ")}
                  </p>
                  <div className="swipe-card-actions">
                    <button
                      type="button"
                      className="warm"
                      disabled={playingId === work.id}
                      onClick={() => {
                        setPlayingId(work.id);
                        void openPlay(work.id, report).finally(() => setPlayingId(null));
                      }}
                    >
                      {playingId === work.id ? "解析中…" : "在线播放"}
                    </button>
                    <button type="button" className="secondary" onClick={() => onOpenDetail(work.id)}>
                      详情
                    </button>
                  </div>
                </div>
              </div>
            </article>
          );
        })}
      </div>
    </section>
  );
}
