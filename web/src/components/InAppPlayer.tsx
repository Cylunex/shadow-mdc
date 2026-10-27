import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { api, appUrl } from "../api";
import { trackPlaybackProgress } from "./playbackProgress";

export type PlayerSource = {
  kind: string;
  label: string;
  url: string;
  playable_in_app?: boolean;
  app_scheme?: string | null;
  provider?: string | null;
  note?: string | null;
  probe_ok?: boolean | null;
  probe_reason?: string | null;
  /** When set, skip auto-proxy (already same-origin). */
  skip_proxy?: boolean;
  referer?: string | null;
};

type Props = {
  title?: string;
  /** Stable work identity; signed media URLs are never used as storage keys. */
  playbackKey?: string;
  sources: PlayerSource[];
  initialIndex?: number;
  autoPlay?: boolean;
  onClose?: () => void;
  onReport?: (message: string) => void;
  className?: string;
  /** Compact mode for swipe cards (no chrome chrome bar). */
  embedded?: boolean;
};

type HlsCtor = new (config?: Record<string, unknown>) => {
  loadSource: (url: string) => void;
  attachMedia: (media: HTMLMediaElement) => void;
  destroy: () => void;
  on: (event: string, cb: (...args: unknown[]) => void) => void;
  startLoad: (startPosition?: number) => void;
};

declare global {
  interface Window {
    Hls?: HlsCtor & { isSupported: () => boolean; Events: Record<string, string> };
  }
}

let hlsLoader: Promise<void> | null = null;

function ensureHls(): Promise<typeof window.Hls | undefined> {
  if (window.Hls) return Promise.resolve(window.Hls);
  if (!hlsLoader) {
    hlsLoader = new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = appUrl("/vendor/hls.min.js") ?? "/vendor/hls.min.js";
      script.async = true;
      script.onload = () => resolve();
      script.onerror = () => {
        hlsLoader = null;
        script.remove();
        reject(new Error("hls.js 加载失败"));
      };
      document.head.appendChild(script);
    });
  }
  return hlsLoader.then(() => window.Hls);
}

function looksLikeHls(url: string): boolean {
  return /\.m3u8($|\?)/i.test(url) || /\/hls(\?|$)/i.test(url);
}


const DEEP_LINK_KINDS = new Set(["emby", "jellyfin", "external_site", "external", "app"]);

/** Emby / Jellyfin / external playback-site deep links open in a new tab. */
export function isDeepLinkSource(source: PlayerSource): boolean {
  if (DEEP_LINK_KINDS.has(source.kind)) return true;
  if (source.playable_in_app === false && !looksLikeHls(source.url) && !/\.(mp4|mkv|webm)($|\?)/i.test(source.url)) {
    return true;
  }
  return false;
}

/** HLS / mp4 / VOD rows that the in-app video element can play. */
export function isInAppMediaSource(source: PlayerSource): boolean {
  if (!source.url || isDeepLinkSource(source)) return false;
  return (
    source.playable_in_app === true ||
    source.kind === "vod" ||
    source.kind === "m3u8" ||
    source.kind === "vod_m3u8" ||
    source.kind === "tube" ||
    looksLikeHls(source.url) ||
    /\.(mp4|mkv|webm)($|\?)/i.test(source.url)
  );
}

function isSameOrigin(url: string): boolean {
  try {
    const u = new URL(url, window.location.href);
    return u.origin === window.location.origin;
  } catch {
    return true;
  }
}

/** Route external playable URLs through swipe media proxy (Referer/CORS/ORB). */
export function proxiedPlayUrl(source: PlayerSource): string {
  const raw = source.url;
  if (!raw || source.skip_proxy) return raw;
  // Resolve / HLS rewrite emit absolute `/api/swipe/...`. Under the /mdc SPA that
  // hits host nginx (OIDC 401). Prefix via appUrl → `./api/swipe/...` → `/mdc/api/swipe/...`.
  try {
    const u = new URL(raw, window.location.href);
    if (
      u.origin === window.location.origin &&
      (u.pathname === "/api/swipe" || u.pathname.startsWith("/api/swipe/"))
    ) {
      return appUrl(`${u.pathname}${u.search}${u.hash}`) ?? raw;
    }
  } catch {
    /* ignore */
  }
  if (isSameOrigin(raw)) return raw;
  // Emby / deep-links that are not direct media stay as-is (browser navigation).
  if (isDeepLinkSource(source)) {
    // Only proxy when the URL looks like raw media.
    if (!looksLikeHls(raw) && !/\.(mp4|mkv|webm|ts)($|\?)/i.test(raw)) {
      return raw;
    }
  }
  const type = looksLikeHls(raw) ? "hls" : "stream";
  return api.swipeMediaProxyUrl(raw, {
    type,
    referer: source.referer ?? undefined,
  });
}

function pickInitialIndex(sources: PlayerSource[], preferred?: number): number {
  if (typeof preferred === "number" && preferred >= 0 && preferred < sources.length) return preferred;
  const healthy = sources.findIndex((s) => s.probe_ok !== false && s.playable_in_app !== false);
  return healthy >= 0 ? healthy : 0;
}

export function InAppPlayer(props: Props) {
  const sessionKey = JSON.stringify([props.playbackKey, props.sources.map((source) => [
    source.url, source.kind, source.referer, source.skip_proxy, source.playable_in_app,
  ])]);
  return <PlayerSession key={sessionKey} {...props} />;
}

function PlayerSession({
  title,
  playbackKey,
  sources,
  initialIndex,
  autoPlay = true,
  onClose,
  onReport,
  className,
  embedded = false,
}: Props) {
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const hlsRef = useRef<{ destroy: () => void } | null>(null);
  const allSources = useMemo(() => sources.filter((s) => Boolean(s.url)), [sources]);
  const playable = useMemo(() => allSources.filter(isInAppMediaSource), [allSources]);
  const [index, setIndex] = useState(() => {
    const preferred = initialIndex === undefined ? undefined : sources[initialIndex];
    if (preferred && isInAppMediaSource(preferred)) {
      const idx = playable.indexOf(preferred);
      if (idx >= 0) return idx;
    }
    return pickInitialIndex(playable, undefined);
  });
  const [status, setStatus] = useState<"idle" | "loading" | "playing" | "error">("idle");
  const [errorText, setErrorText] = useState<string | null>(null);
  const [muted, setMuted] = useState(true);
  const triedRef = useRef<Set<number>>(new Set(playable.length ? [index] : []));
  const resumeAtRef = useRef(0);

  const current = playable[index] ?? playable[0] ?? null;

  const destroyHls = useCallback(() => {
    if (hlsRef.current) {
      try {
        hlsRef.current.destroy();
      } catch {
        /* ignore */
      }
      hlsRef.current = null;
    }
  }, []);

  const tryFailover = useCallback(
    (reason: string) => {
      if (!playable.length) return;
      triedRef.current.add(index);
      const next = playable.findIndex((_, i) => !triedRef.current.has(i));
      if (next < 0) {
        setStatus("error");
        setErrorText(reason || "全部片源播放失败");
        onReport?.(reason || "全部片源播放失败");
        return;
      }
      const label = playable[next]?.label || `源 ${next + 1}`;
      onReport?.(`播放失败，自动切换：${label}`);
      resumeAtRef.current = videoRef.current?.currentTime || 0;
      setIndex(next);
    },
    [index, onReport, playable],
  );

  const attachSource = useCallback(
    async (source: PlayerSource, signal: AbortSignal) => {
      const video = videoRef.current;
      if (!video || !source?.url) return;
      destroyHls();
      setStatus("loading");
      setErrorText(null);
      const mediaUrl = proxiedPlayUrl(source);

      try {
        if (looksLikeHls(source.url) || looksLikeHls(mediaUrl)) {
          const Hls = await ensureHls();
          if (signal.aborted) return;
          if (Hls?.isSupported()) {
            const hls = new Hls({
              enableWorker: true,
              lowLatencyMode: false,
              maxBufferLength: 30,
            });
            hlsRef.current = hls;
            hls.loadSource(mediaUrl);
            hls.attachMedia(video);
            hls.on(Hls.Events.MANIFEST_PARSED || "hlsManifestParsed", () => {
              if (signal.aborted) return;
              if (autoPlay) void video.play().catch(() => {
                if (!signal.aborted) setStatus("idle");
              });
              else setStatus("idle");
            });
            hls.on(Hls.Events.ERROR || "hlsError", (_evt, data) => {
              const d = data as { fatal?: boolean; type?: string; details?: string };
              if (signal.aborted || !d?.fatal) return;
              destroyHls();
              tryFailover(d.details || d.type || "HLS 错误");
            });
            return;
          }
          // Native HLS (Safari)
          video.src = mediaUrl;
        } else {
          video.src = mediaUrl;
        }
        if (autoPlay) await video.play().catch(() => {
          if (!signal.aborted) setStatus("idle");
        });
        else setStatus("idle");
      } catch (err) {
        if (!signal.aborted) tryFailover(err instanceof Error ? err.message : String(err));
      }
    },
    [autoPlay, destroyHls, tryFailover],
  );

  useEffect(() => {
    if (!current) {
      // Deep-link-only resolve: chips still work; do not flash a fatal empty error.
      if (!playable.length && allSources.length) {
        setStatus("idle");
        setErrorText(null);
        return;
      }
      setStatus("error");
      setErrorText("无可用片源");
      return;
    }
    const startAt = resumeAtRef.current;
    resumeAtRef.current = 0;
    const controller = new AbortController();
    const video = videoRef.current;
    const stopTracking = video ? trackPlaybackProgress(video, playbackKey, startAt) : undefined;
    void attachSource(current, controller.signal);
    return () => {
      stopTracking?.();
      controller.abort();
      destroyHls();
      if (video) {
        video.pause();
        video.removeAttribute("src");
        video.load();
      }
    };
  }, [current?.url, current?.label, index]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    const video = videoRef.current;
    if (!video) return;
    video.muted = muted;
  }, [muted]);

  useEffect(() => {
    const video = videoRef.current;
    if (!video) return;
    const onError = () => tryFailover(video.error?.message || "媒体错误");
    const onPlaying = () => setStatus("playing");
    video.addEventListener("error", onError);
    video.addEventListener("playing", onPlaying);
    return () => {
      video.removeEventListener("error", onError);
      video.removeEventListener("playing", onPlaying);
    };
  }, [tryFailover]);

  const switchSource = (next: number) => {
    if (next === index || next < 0 || next >= playable.length) return;
    resumeAtRef.current = videoRef.current?.currentTime || 0;
    triedRef.current.add(next);
    setIndex(next);
    onReport?.(`已切换：${playable[next]?.label || `源 ${next + 1}`}`);
  };

  const openDeepLink = (source: PlayerSource) => {
    window.open(source.url, "_blank", "noopener,noreferrer");
    onReport?.(`已打开：${source.label}`);
  };

  if (!allSources.length) {
    return (
      <div className={`in-app-player in-app-player--empty ${className || ""}`}>
        <p>暂无可用片源</p>
        {onClose ? (
          <button type="button" className="ghost" onClick={onClose}>
            关闭
          </button>
        ) : null}
      </div>
    );
  }

  return (
    <div className={`in-app-player ${embedded ? "in-app-player--embedded" : "in-app-player--overlay"} ${className || ""}`}>
      {!embedded ? (
        <div className="in-app-player-chrome">
          <div className="in-app-player-title">{title || current?.label || "在线播放"}</div>
          <div className="in-app-player-actions">
            <button type="button" className="ghost" onClick={() => setMuted((m) => !m)}>
              {muted ? "取消静音" : "静音"}
            </button>
            {onClose ? (
              <button type="button" className="ghost" onClick={onClose}>
                关闭
              </button>
            ) : null}
          </div>
        </div>
      ) : null}
      <div className="in-app-player-stage">
        {playable.length ? (
          <video
            ref={videoRef}
            className="in-app-player-video"
            playsInline
            controls={!embedded}
            muted={muted}
            preload="metadata"
          />
        ) : (
          <div className="in-app-player-status">无应用内片源 — 下方选择外站 / Emby 打开</div>
        )}
        {playable.length && status === "loading" ? <div className="in-app-player-status">解析 / 缓冲中…</div> : null}
        {playable.length && status === "error" ? (
          <div className="in-app-player-status in-app-player-status--error">
            {errorText || "播放失败"}
            {onClose ? (
              <button type="button" className="secondary" onClick={onClose}>
                关闭
              </button>
            ) : null}
          </div>
        ) : null}
        {embedded && playable.length ? (
          <button
            type="button"
            className="in-app-player-mute"
            onClick={() => setMuted((m) => !m)}
            aria-label={muted ? "取消静音" : "静音"}
          >
            {muted ? "🔇" : "🔊"}
          </button>
        ) : null}
      </div>
      {allSources.length ? (
        <div className="in-app-player-sources" role="group" aria-label="片源">
          {allSources.map((s, i) => {
            const deep = isDeepLinkSource(s);
            const playableIdx = deep ? -1 : playable.indexOf(s);
            const active = !deep && playableIdx === index;
            return (
              <button
                key={`${s.kind}-${s.label}-${i}`}
                type="button"
                className={active ? "warm" : "ghost"}
                onClick={() => {
                  if (deep) openDeepLink(s);
                  else if (playableIdx >= 0) switchSource(playableIdx);
                }}
                title={deep ? `外开 · ${s.kind}` : (s.probe_reason || s.note || undefined)}
              >
                {s.label}{deep ? " ↗" : ""}{s.probe_ok === false ? " !" : ""}
              </button>
            );
          })}
        </div>
      ) : null}
    </div>
  );
}
