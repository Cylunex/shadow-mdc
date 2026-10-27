import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { memoryStorage } from "../test/storage";
import { trackPlaybackProgress } from "./playbackProgress";

function media(duration = 300): HTMLVideoElement {
  const video = document.createElement("video");
  Object.defineProperty(video, "duration", { configurable: true, value: duration });
  return video;
}

describe("browser playback progress", () => {
  const disposers: (() => void)[] = [];
  function bind(video: HTMLVideoElement, key?: string, position?: number) {
    const dispose = trackPlaybackProgress(video, key, position);
    disposers.push(dispose);
    return () => { dispose(); disposers.splice(disposers.indexOf(dispose), 1); };
  }
  beforeEach(() => vi.stubGlobal("localStorage", memoryStorage()));
  afterEach(() => {
    disposers.splice(0).forEach((dispose) => dispose());
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  function remember(key = "work:a", seconds = 80) {
    const video = media();
    const dispose = bind(video, key);
    video.dispatchEvent(new Event("loadedmetadata"));
    video.currentTime = seconds;
    video.dispatchEvent(new Event("timeupdate"));
    dispose();
  }

  it("restores the same work after recreation and isolates other works", () => {
    remember();
    const video = media();
    bind(video, "work:a");
    expect(video.currentTime).toBe(0);
    video.dispatchEvent(new Event("loadedmetadata"));
    expect(video.currentTime).toBe(80);
    const other = media();
    bind(other, "work:b");
    other.dispatchEvent(new Event("loadedmetadata"));
    expect(other.currentTime).toBe(0);
  });

  it("does not erase progress if the next source never loads metadata", () => {
    remember();
    const video = media();
    const dispose = bind(video, "work:a");
    video.dispatchEvent(new Event("pause"));
    dispose();
    const retry = media();
    bind(retry, "work:a");
    retry.dispatchEvent(new Event("loadedmetadata"));
    expect(retry.currentTime).toBe(80);
  });

  it("clears completed progress and ignores late pause events", () => {
    remember();
    const video = media();
    const dispose = bind(video, "work:a");
    video.dispatchEvent(new Event("loadedmetadata"));
    video.currentTime = 300;
    video.dispatchEvent(new Event("ended"));
    video.dispatchEvent(new Event("pause"));
    dispose();
    const next = media();
    bind(next, "work:a");
    next.dispatchEvent(new Event("loadedmetadata"));
    expect(next.currentTime).toBe(0);
  });

  it("prefers the current source position and does not resume beyond a shorter replacement", () => {
    remember();
    const failover = media();
    bind(failover, "work:a", 2);
    failover.dispatchEvent(new Event("loadedmetadata"));
    expect(failover.currentTime).toBe(2);
    const shorter = media(50);
    bind(shorter, "work:a");
    shorter.dispatchEvent(new Event("loadedmetadata"));
    expect(shorter.currentTime).toBe(0);
  });

  it("throttles time updates but flushes pagehide and removes listeners before teardown", () => {
    const write = vi.spyOn(window.localStorage, "setItem");
    const video = media();
    const dispose = bind(video, "work:a");
    video.dispatchEvent(new Event("loadedmetadata"));
    for (let time = 10; time < 20; time++) {
      video.currentTime = time;
      video.dispatchEvent(new Event("timeupdate"));
    }
    expect(write).toHaveBeenCalledTimes(1);
    window.dispatchEvent(new Event("pagehide"));
    expect(JSON.parse(window.localStorage.getItem("shadow-mdc:playback:v1")!)[0].seconds).toBe(19);
    dispose();
    const count = write.mock.calls.length;
    video.currentTime = 0;
    video.dispatchEvent(new Event("pause"));
    window.dispatchEvent(new Event("pagehide"));
    expect(write).toHaveBeenCalledTimes(count);
  });

  it("bounds retained positions and discards expired or invalid entries", () => {
    const entries = Array.from({ length: 210 }, (_, i) => ({ key: `work:${i}`, seconds: 50, updatedAt: Date.now() }));
    entries.push({ key: "expired", seconds: 80, updatedAt: 1 });
    entries.push({ key: "invalid", seconds: -1, updatedAt: Date.now() });
    window.localStorage.setItem("shadow-mdc:playback:v1", JSON.stringify(entries));
    remember();
    const stored = JSON.parse(window.localStorage.getItem("shadow-mdc:playback:v1")!);
    expect(stored).toHaveLength(200);
    expect(stored.at(-1).key).toBe("work:a");
    expect(stored.some((entry: { key: string }) => ["expired", "invalid"].includes(entry.key))).toBe(false);
  });

  it("keeps playback usable when storage is corrupt or unavailable", () => {
    window.localStorage.setItem("shadow-mdc:playback:v1", "bad json");
    expect(() => remember()).not.toThrow();
    vi.spyOn(window.localStorage, "getItem").mockImplementation(() => { throw new Error("blocked"); });
    vi.spyOn(window.localStorage, "setItem").mockImplementation(() => { throw new Error("quota"); });
    expect(() => remember()).not.toThrow();
  });
});
