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
  if (isSameOrigin(raw)) return raw;
  // Emby / deep-links that are not direct media stay as-is (browser navigation).
  if (source.kind === "emby" || source.kind === "external" || source.kind === "app") {
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
  const playable = useMemo(
    () => sources.filter((s) => Boolean(s.url) && (s.playable_in_app !== false || looksLikeHls(s.url) || /\.mp4/i.test(s.url))),
    [sources],
  );
  const [index, setIndex] = useState(() => {
    const preferred = initialIndex === undefined ? undefined : sources[initialIndex];
    return pickInitialIndex(playable, preferred ? playable.indexOf(preferred) : undefined);
  });
  const [status, setStatus] = useState<"idle" | "loading" | "playing" | "error">("idle");
  const [errorText, setErrorText] = useState<string | null>(null);
  const [muted, setMuted] = useState(true);
  const triedRef = useRef<Set<number>>(new Set([index]));
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

  if (!playable.length) {
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
        <video
          ref={videoRef}
          className="in-app-player-video"
          playsInline
          controls={!embedded}
          muted={muted}
          preload="metadata"
        />
        {status === "loading" ? <div className="in-app-player-status">解析 / 缓冲中…</div> : null}
        {status === "error" ? (
          <div className="in-app-player-status in-app-player-status--error">
            {errorText || "播放失败"}
            {onClose ? (
              <button type="button" className="secondary" onClick={onClose}>
                关闭
              </button>
            ) : null}
          </div>
        ) : null}
        {embedded ? (
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
      {playable.length > 1 ? (
        <div className="in-app-player-sources" role="group" aria-label="片源">
          {playable.map((s, i) => (
            <button
              key={`${s.kind}-${s.label}-${i}`}
              type="button"
              className={i === index ? "warm" : "ghost"}
              onClick={() => switchSource(i)}
              title={s.probe_reason || s.note || undefined}
            >
              {s.label}
              {s.probe_ok === false ? " !" : ""}
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}
