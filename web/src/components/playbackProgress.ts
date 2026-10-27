/** Browser-only progress: stable work IDs, bounded retention, no media URLs. */
const STORAGE_KEY = "shadow-mdc:playback:v1";
const MAX_ENTRIES = 200;
const MAX_AGE = 90 * 24 * 60 * 60 * 1000;
const WRITE_INTERVAL = 5000;

type Position = { key: string; seconds: number; updatedAt: number };

function readPositions(): Position[] {
  try {
    const parsed: unknown = JSON.parse(window.localStorage.getItem(STORAGE_KEY) || "[]");
    if (!Array.isArray(parsed)) return [];
    const cutoff = Date.now() - MAX_AGE;
    return parsed.filter((item): item is Position =>
      item !== null && typeof item === "object"
      && typeof item.key === "string" && item.key.length > 0
      && typeof item.seconds === "number" && Number.isFinite(item.seconds) && item.seconds >= 0
      && typeof item.updatedAt === "number" && Number.isFinite(item.updatedAt)
      && item.updatedAt >= cutoff && item.updatedAt <= Date.now(),
    ).slice(-MAX_ENTRIES);
  } catch {
    return [];
  }
}

function storePosition(key: string, seconds: number): void {
  try {
    const positions = readPositions().filter((item) => item.key !== key);
    if (seconds >= 5) positions.push({ key, seconds, updatedAt: Date.now() });
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(positions.slice(-MAX_ENTRIES)));
  } catch {
    // Private browsing, disabled storage and quota errors must not stop playback.
  }
}

/** Bind before attaching a source; dispose before clearing it to avoid saving reset events. */
export function trackPlaybackProgress(video: HTMLVideoElement, key?: string, sourcePosition = 0): () => void {
  const remembered = key ? readPositions().find((item) => item.key === key)?.seconds || 0 : 0;
  const resumeAt = sourcePosition > 0 && Number.isFinite(sourcePosition) ? sourcePosition : remembered;
  let ready = false;
  let completed = false;
  let lastWrite = 0;

  const onMetadata = () => {
    if (ready) return;
    const duration = video.duration;
    const canResume = sourcePosition > 0 ? resumeAt > 0 && duration > resumeAt : resumeAt >= 5 && duration > resumeAt + 10;
    if (canResume && Number.isFinite(duration)) {
      try { video.currentTime = resumeAt; } catch { /* Source may reject seeking. */ }
    }
    ready = true;
  };
  const save = (force: boolean) => {
    if (!key || !ready || completed || video.seeking) return;
    const seconds = video.currentTime;
    const duration = video.duration;
    if (!Number.isFinite(seconds) || seconds < 0 || !Number.isFinite(duration) || duration <= 0) return;
    if (!force && Date.now() - lastWrite < WRITE_INTERVAL) return;
    storePosition(key, duration - seconds <= 10 ? 0 : seconds);
    lastWrite = Date.now();
  };
  const onTime = () => save(false);
  const onPause = () => save(true);
  const onSeek = () => { completed = false; save(true); };
  const onEnded = () => {
    completed = true;
    if (key && ready) storePosition(key, 0);
  };
  const onVisibility = () => { if (document.visibilityState === "hidden") save(true); };
  video.addEventListener("loadedmetadata", onMetadata);
  video.addEventListener("timeupdate", onTime);
  video.addEventListener("pause", onPause);
  video.addEventListener("seeked", onSeek);
  video.addEventListener("ended", onEnded);
  window.addEventListener("pagehide", onPause);
  document.addEventListener("visibilitychange", onVisibility);
  return () => {
    save(true);
    video.removeEventListener("loadedmetadata", onMetadata);
    video.removeEventListener("timeupdate", onTime);
    video.removeEventListener("pause", onPause);
    video.removeEventListener("seeked", onSeek);
    video.removeEventListener("ended", onEnded);
    window.removeEventListener("pagehide", onPause);
    document.removeEventListener("visibilitychange", onVisibility);
  };
}
