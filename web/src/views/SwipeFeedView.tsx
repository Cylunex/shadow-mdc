import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { api } from "../api";
import { CoverImage } from "../components/CoverImage";
import { InAppPlayer, type PlayerSource } from "../components/InAppPlayer";
import { mediaUrl } from "../lib/mediaUrl";
import type { Work } from "../model";

type Props = {
  works: Work[];
  loading: boolean;
  onOpenDetail: (workId: string) => void;
  report: (message: string) => void;
};

type Mode = "tube" | "library";
type LibraryFilter = "all" | "local" | "want";
const MAX_RESOLVE_CACHE = 48;
const RESOLVE_TTL_MS = 5 * 60 * 1000;

type TubeItem = {
  site: string;
  id: string;
  title?: string;
  cover?: string;
  pageUrl?: string;
  duration?: string | number;
  views?: string | number;
  [key: string]: unknown;
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
  const withPoster = works.filter((w) => Boolean(w.image_url));
  const without = works.filter((w) => !w.image_url);
  const sortBucket = (bucket: Work[]) => {
    const local = bucket.filter((w) => w.has_local_media);
    const remote = bucket.filter((w) => !w.has_local_media);
    return [...shuffle(local), ...shuffle(remote)];
  };
  return [...sortBucket(withPoster), ...sortBucket(without)];
}

function tubeKey(item: TubeItem): string {
  return `${item.site}::${item.id}`;
}

function sourcesFromResolve(payload: Record<string, unknown>, pageUrl?: string): PlayerSource[] {
  const out: PlayerSource[] = [];
  const qualities = Array.isArray(payload.qualities) ? payload.qualities : [];
  for (const q of qualities) {
    if (!q || typeof q !== "object") continue;
    const row = q as Record<string, unknown>;
    const url = typeof row.url === "string" ? row.url : typeof row.src === "string" ? row.src : "";
    if (!url) continue;
    out.push({
      kind: "tube",
      label: String(row.label || row.quality || row.name || `源 ${out.length + 1}`),
      url,
      playable_in_app: true,
      referer: pageUrl || (typeof row.referer === "string" ? row.referer : null),
      provider: typeof row.provider === "string" ? row.provider : null,
    });
  }
  const direct =
    (typeof payload.directUrl === "string" && payload.directUrl) ||
    (typeof payload.url === "string" && payload.url) ||
    "";
  if (direct && !out.some((s) => s.url === direct)) {
    out.unshift({
      kind: "tube",
      label: String(payload.type || "默认"),
      url: direct,
      playable_in_app: true,
      referer: pageUrl || null,
    });
  }
  return out;
}

async function enqueueOffline(workId: string, report: (message: string) => void): Promise<void> {
  try {
    await api.submitWorkOffline(workId, {});
    report("已提交离线入库任务");
  } catch (err) {
    report(err instanceof Error ? err.message : String(err));
  }
}

export function SwipeFeedView({ works, loading, onOpenDetail, report }: Props) {
  const [mode, setMode] = useState<Mode>("tube");
  const [orderKey, setOrderKey] = useState(0);
  const [filter, setFilter] = useState<LibraryFilter>("all");
  const [activeLibId, setActiveLibId] = useState<string | null>(null);
  const [libSources, setLibSources] = useState<PlayerSource[]>([]);
  const [libResolving, setLibResolving] = useState(false);
  const [offlineId, setOfflineId] = useState<string | null>(null);

  const [tubeItems, setTubeItems] = useState<TubeItem[]>([]);
  const [tubeCursor, setTubeCursor] = useState<string | null>(null);
  const [tubeHasMore, setTubeHasMore] = useState(true);
  const [tubeLoading, setTubeLoading] = useState(false);
  const [tubeReady, setTubeReady] = useState(false);
  const [tubeError, setTubeError] = useState<string | null>(null);
  const [activeTubeKey, setActiveTubeKey] = useState<string | null>(null);
  const [tubePlayback, setTubePlayback] = useState<{ key: string; sources: PlayerSource[] } | null>(null);
  const [tubeResolving, setTubeResolving] = useState(false);
  const resolveCache = useRef<Map<string, { sources: PlayerSource[]; expiresAt: number }>>(new Map());
  const libraryRequest = useRef<{ workId: string; controller: AbortController } | null>(null);
  const scrollerRef = useRef<HTMLDivElement | null>(null);
  const loadingMoreRef = useRef(false);

  const filtered = useMemo(() => {
    if (filter === "local") return works.filter((w) => w.has_local_media);
    if (filter === "want") return works.filter((w) => Boolean(w.want_list));
    return works;
  }, [works, filter]);

  const libraryFeed = useMemo(() => rankWorks(filtered), [filtered, orderKey]);
  const activeTubeItem = useMemo(
    () => tubeItems.find((item) => tubeKey(item) === activeTubeKey),
    [tubeItems, activeTubeKey],
  );
  const tubeSources = tubePlayback?.key === activeTubeKey ? tubePlayback.sources : [];

  const stopLibraryPlay = useCallback(() => {
    libraryRequest.current?.controller.abort();
    libraryRequest.current = null;
    setActiveLibId(null);
    setLibSources([]);
    setLibResolving(false);
  }, []);

  useEffect(() => {
    stopLibraryPlay();
    return () => libraryRequest.current?.controller.abort();
  }, [mode, filter, orderKey, stopLibraryPlay]);

  const loadTube = useCallback(
    async (opts: { reset?: boolean } = {}) => {
      if (loadingMoreRef.current) return;
      loadingMoreRef.current = true;
      setTubeLoading(true);
      setTubeError(null);
      try {
        const cursor = opts.reset ? null : tubeCursor;
        const res = await api.swipeFeed({ cursor, limit: 12, mode: "random" });
        const rawItems = Array.isArray(res.items) ? res.items : [];
        const mapped: TubeItem[] = rawItems
          .map((row) => {
            const r = row as Record<string, unknown>;
            const site = String(r.site || r.plugin || "");
            const id = String(r.id || r.videoId || r.vid || "");
            if (!site || !id) return null;
            return {
              site,
              id,
              title: typeof r.title === "string" ? r.title : undefined,
              cover: typeof r.cover === "string" ? r.cover : typeof r.thumb === "string" ? r.thumb : undefined,
              pageUrl: typeof r.pageUrl === "string" ? r.pageUrl : typeof r.url === "string" ? r.url : undefined,
              duration: (r.duration as string | number | undefined) ?? undefined,
              views: (r.views as string | number | undefined) ?? undefined,
            } as TubeItem;
          })
          .filter(Boolean) as TubeItem[];
        setTubeItems((prev) => {
          const next = opts.reset ? [] : prev;
          const seen = new Set(next.map(tubeKey));
          return [...next, ...mapped.filter((item) => {
            const key = tubeKey(item);
            if (seen.has(key)) return false;
            seen.add(key);
            return true;
          })];
        });
        setTubeCursor(res.nextCursor ?? null);
        setTubeHasMore(Boolean(res.hasMore ?? res.nextCursor));
        if (res.error) setTubeError(String(res.error));
        setTubeReady(true);
      } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        setTubeError(msg);
        setTubeReady(true);
        report(`刷流 feed：${msg}`);
      } finally {
        setTubeLoading(false);
        loadingMoreRef.current = false;
      }
    },
    [report, tubeCursor],
  );

  useEffect(() => {
    if (mode !== "tube") return;
    if (tubeReady || tubeLoading) return;
    void loadTube({ reset: true });
  }, [mode, tubeReady, tubeLoading, loadTube]);

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
  }, [mode]);

  // IntersectionObserver: active card + prefetch more tube
  useEffect(() => {
    const root = scrollerRef.current;
    if (!root) return;
    const cards = Array.from(root.querySelectorAll<HTMLElement>("[data-swipe-key]"));
    if (!cards.length) return;
    const io = new IntersectionObserver(
      (entries) => {
        const visible = entries
          .filter((e) => e.isIntersecting && e.intersectionRatio >= 0.55)
          .sort((a, b) => b.intersectionRatio - a.intersectionRatio)[0];
        if (!visible) return;
        const key = (visible.target as HTMLElement).dataset.swipeKey || null;
        if (mode === "tube") {
          setActiveTubeKey(key);
          const idx = cards.findIndex((c) => c.dataset.swipeKey === key);
          if (idx >= cards.length - 3 && tubeHasMore && !tubeLoading && !tubeError) {
            void loadTube();
          }
        } else {
          if (libraryRequest.current && libraryRequest.current.workId !== key) stopLibraryPlay();
        }
      },
      { root, threshold: [0.55, 0.75] },
    );
    cards.forEach((c) => io.observe(c));
    return () => io.disconnect();
  }, [mode, tubeItems, libraryFeed, tubeHasMore, tubeLoading, tubeError, loadTube, orderKey, stopLibraryPlay]);

  // Resolve active tube item
  useEffect(() => {
    if (mode !== "tube" || !activeTubeKey || !activeTubeItem) return;
    const item = activeTubeItem;
    const cached = resolveCache.current.get(activeTubeKey);
    if (cached && cached.expiresAt > Date.now()) {
      setTubePlayback({ key: activeTubeKey, sources: cached.sources });
      setTubeResolving(false);
      return;
    }
    resolveCache.current.delete(activeTubeKey);
    const controller = new AbortController();
    setTubeResolving(true);
    setTubePlayback(null);
    void (async () => {
      try {
        const res = await api.swipeResolve({
          site: item.site,
          id: item.id,
          pageUrl: item.pageUrl,
        }, controller.signal);
        if (controller.signal.aborted) return;
        if (res.error) {
          report(`${item.title || item.id}: ${res.error}`);
          return;
        }
        const sources = sourcesFromResolve(res as Record<string, unknown>, item.pageUrl);
        if (sources.length) {
          resolveCache.current.set(activeTubeKey, { sources, expiresAt: Date.now() + RESOLVE_TTL_MS });
          if (resolveCache.current.size > MAX_RESOLVE_CACHE) {
            const oldest = resolveCache.current.keys().next().value;
            if (oldest !== undefined) resolveCache.current.delete(oldest);
          }
        }
        setTubePlayback({ key: activeTubeKey, sources });
        if (!sources.length) report(`${item.title || item.id}: 无可用直链`);
      } catch (err) {
        if (!controller.signal.aborted) report(err instanceof Error ? err.message : String(err));
      } finally {
        if (!controller.signal.aborted) setTubeResolving(false);
      }
    })();
    return () => {
      controller.abort();
    };
  }, [activeTubeKey, activeTubeItem, mode, report]);

  const openLibraryPlay = async (work: Work) => {
    libraryRequest.current?.controller.abort();
    const controller = new AbortController();
    libraryRequest.current = { workId: work.id, controller };
    setLibResolving(true);
    setActiveLibId(work.id);
    setLibSources([]);
    try {
      const resolved = await api.workPlayResolve(work.id, controller.signal);
      if (controller.signal.aborted) return;
      const preferred = resolved.chosen || resolved.preferred;
      const list = (resolved.sources ?? []).map((s) => ({
        kind: s.kind,
        label: s.label,
        url: s.url,
        playable_in_app: s.playable_in_app,
        app_scheme: s.app_scheme,
        provider: s.provider,
        note: s.note,
        probe_ok: s.probe_ok,
        probe_reason: s.probe_reason,
      })) as PlayerSource[];

      const inApp = list.filter(
        (s) =>
          s.playable_in_app ||
          s.kind === "vod" ||
          s.kind === "m3u8" ||
          /\.m3u8/i.test(s.url) ||
          /\.mp4/i.test(s.url),
      );

      if (inApp.length) {
        // Prefer chosen/preferred first
        if (preferred?.url) {
          const idx = inApp.findIndex((s) => s.url === preferred.url);
          if (idx > 0) {
            const [hit] = inApp.splice(idx, 1);
            if (hit) inApp.unshift(hit);
          }
        }
        setLibSources(inApp);
        const fails = resolved.failures ?? [];
        if (fails.length) {
          report(`已选片源；${fails.length} 路探测失败`);
        }
        return;
      }

      if (preferred?.url && !preferred.playable_in_app) {
        window.open(preferred.url, "_blank", "noopener,noreferrer");
        setActiveLibId(null);
        return;
      }
      report("暂无可用片源（需 Emby 或本地 playback-sites）");
      setActiveLibId(null);
    } catch (err) {
      if (controller.signal.aborted) return;
      report(err instanceof Error ? err.message : String(err));
      setActiveLibId(null);
    } finally {
      if (!controller.signal.aborted) setLibResolving(false);
    }
  };

  const toolbar = (
    <div className="swipe-feed-toolbar">
      <div className="swipe-feed-filters" role="group" aria-label="刷流模式">
        <button type="button" className={mode === "tube" ? "warm" : "ghost"} onClick={() => setMode("tube")}>
          外站
        </button>
        <button type="button" className={mode === "library" ? "warm" : "ghost"} onClick={() => setMode("library")}>
          本库
        </button>
      </div>
      {mode === "library" ? (
        <>
          <span className="swipe-feed-hint">上下滑 · J/K · {libraryFeed.length} 部</span>
          <div className="swipe-feed-filters" role="group" aria-label="本库筛选">
            <button type="button" className={filter === "all" ? "warm" : "ghost"} onClick={() => setFilter("all")}>
              全部
            </button>
            <button type="button" className={filter === "local" ? "warm" : "ghost"} onClick={() => setFilter("local")}>
              本地
            </button>
            <button type="button" className={filter === "want" ? "warm" : "ghost"} onClick={() => setFilter("want")}>
              想看
            </button>
          </div>
          <button type="button" className="ghost" onClick={reshuffle}>
            打乱
          </button>
        </>
      ) : (
        <span className="swipe-feed-hint">
          外站 tube · 滑到卡片自动解析播放
          {tubeLoading ? " · 加载中…" : ""}
          {tubeItems.length ? ` · ${tubeItems.length}` : ""}
        </span>
      )}
      {mode === "tube" && tubeItems.length > 0 && tubeError && !tubeLoading ? (
        <button type="button" className="secondary" onClick={() => void loadTube()}>
          重试加载
        </button>
      ) : null}
    </div>
  );

  if (mode === "library") {
    if (loading) {
      return (
        <section className="swipe-feed swipe-feed--loading" aria-busy="true" aria-label="刷流加载中">
          {toolbar}
          <p className="muted">正在加载片库…</p>
        </section>
      );
    }
    if (libraryFeed.length === 0) {
      return (
        <section className="swipe-feed swipe-feed--empty">
          {toolbar}
          <p>片库为空。先在「影片」里扫库或导入作品。</p>
        </section>
      );
    }
  }

  if (mode === "tube" && tubeReady && !tubeItems.length && !tubeLoading) {
    return (
      <section className="swipe-feed swipe-feed--empty" aria-label="外站刷流">
        {toolbar}
        <p>{tubeError || "外站 feed 为空。检查 sidecar / 站点配置（site-settings，本地-only）。"}</p>
        <button type="button" className="secondary" onClick={() => { setTubeReady(false); void loadTube({ reset: true }); }}>
          重试
        </button>
        <button type="button" className="ghost" onClick={() => setMode("library")}>
          改用本库
        </button>
      </section>
    );
  }

  return (
    <section className="swipe-feed" aria-label={mode === "tube" ? "外站刷流" : "本库刷流"}>
      {toolbar}
      <div className="swipe-feed-scroller" ref={scrollerRef}>
        {mode === "tube"
          ? tubeItems.map((item, index) => {
              const key = tubeKey(item);
              const active = activeTubeKey === key;
              const thumb = item.cover || "";
              return (
                <article key={key} className="swipe-card" data-swipe-key={key} data-index={index}>
                  <div className="swipe-card-stage">
                    {active && tubeSources.length ? (
                      <InAppPlayer
                        embedded
                        title={item.title}
                        sources={tubeSources}
                        onReport={report}
                      />
                    ) : thumb ? (
                      <CoverImage src={thumb} className="swipe-card-cover" loading={index < 3 ? "eager" : "lazy"} />
                    ) : (
                      <div className="poster-fallback swipe-card-fallback" aria-hidden />
                    )}
                    <div className="swipe-card-gradient" aria-hidden />
                    <div className="swipe-card-hud">
                      <div className="swipe-card-code">{item.site}</div>
                      <h2 className="swipe-card-title">{item.title || item.id}</h2>
                      <p className="swipe-card-meta">
                        {[item.duration, item.views].filter(Boolean).join(" · ")}
                        {active && tubeResolving ? " · 解析中…" : ""}
                      </p>
                    </div>
                  </div>
                </article>
              );
            })
          : libraryFeed.map((work, index) => {
              const thumb = mediaUrl(work.image_url);
              const code = work.primary_code || work.id.slice(0, 8);
              const title = work.title || code;
              const playing = activeLibId === work.id && libSources.length > 0;
              return (
                <article
                  key={`${work.id}-${orderKey}`}
                  className="swipe-card"
                  data-swipe-key={work.id}
                  data-index={index}
                  id={`swipe-card-${work.id}`}
                >
                  <div className="swipe-card-stage">
                    {playing ? (
                      <InAppPlayer
                        embedded
                        title={title}
                        playbackKey={`work:${work.id}`}
                        sources={libSources}
                        onReport={report}
                        onClose={stopLibraryPlay}
                      />
                    ) : thumb ? (
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
                      {!playing ? (
                        <div className="swipe-card-actions">
                          <button
                            type="button"
                            className="warm"
                            disabled={libResolving && activeLibId === work.id}
                            onClick={() => void openLibraryPlay(work)}
                          >
                            {libResolving && activeLibId === work.id ? "解析中…" : "在线播放"}
                          </button>
                          <button
                            type="button"
                            className="secondary"
                            disabled={offlineId === work.id}
                            onClick={() => {
                              setOfflineId(work.id);
                              void enqueueOffline(work.id, report).finally(() => setOfflineId(null));
                            }}
                          >
                            {offlineId === work.id ? "提交中…" : "入库"}
                          </button>
                          <button type="button" className="secondary" onClick={() => onOpenDetail(work.id)}>
                            详情
                          </button>
                        </div>
                      ) : null}
                    </div>
                  </div>
                </article>
              );
            })}
      </div>
    </section>
  );
}
