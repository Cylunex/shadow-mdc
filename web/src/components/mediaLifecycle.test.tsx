import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { CoverImage } from "./CoverImage";
import { InAppPlayer, type PlayerSource } from "./InAppPlayer";
import { Lightbox } from "./Lightbox";

vi.mock("../api", () => ({
  appUrl: (path: string) => path,
  api: { swipeMediaProxyUrl: (path: string) => path },
}));

class FakeHls {
  static Events = { MANIFEST_PARSED: "manifest", ERROR: "error" };
  static isSupported = () => true;
  static instances: FakeHls[] = [];
  callbacks = new Map<string, (...args: unknown[]) => void>();
  loadSource = vi.fn();
  attachMedia = vi.fn();
  destroy = vi.fn();
  startLoad = vi.fn();
  constructor() { FakeHls.instances.push(this); }
  on(event: string, callback: (...args: unknown[]) => void) { this.callbacks.set(event, callback); }
}

const source = (url: string): PlayerSource => ({ kind: "vod", label: url, url, playable_in_app: true });

describe("media component lifecycle", () => {
  let container: HTMLDivElement;
  let root: Root;
  beforeEach(() => {
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
    vi.stubGlobal("Hls", FakeHls);
    FakeHls.instances = [];
    vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue(undefined);
    vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => undefined);
    vi.spyOn(HTMLMediaElement.prototype, "load").mockImplementation(() => undefined);
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
  });
  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    document.querySelectorAll('script[src="/vendor/hls.min.js"]').forEach((script) => script.remove());
  });

  it("retries a new cover after the previous image failed", async () => {
    await act(async () => root.render(<CoverImage src="/missing.jpg" />));
    await act(async () => container.querySelector("img")!.dispatchEvent(new Event("error")));
    expect(container.querySelector("img")).toBeNull();
    await act(async () => root.render(<CoverImage src="/replacement.jpg" />));
    expect(container.querySelector("img")?.getAttribute("src")).toBe("/replacement.jpg");
  });

  it("does not attach HLS after a delayed loader finishes on an unmounted player", async () => {
    vi.stubGlobal("Hls", undefined);
    await act(async () => root.render(<InAppPlayer sources={[source("/a.m3u8")]} />));
    const script = document.querySelector<HTMLScriptElement>('script[src="/vendor/hls.min.js"]')!;
    expect(script).not.toBeNull();
    await act(async () => root.render(null));
    vi.stubGlobal("Hls", FakeHls);
    await act(async () => script.dispatchEvent(new Event("load")));
    expect(FakeHls.instances).toHaveLength(0);
  });

  it("destroys the old HLS session and ignores its late error callbacks", async () => {
    const report = vi.fn();
    await act(async () => root.render(<InAppPlayer sources={[source("/a.m3u8")]} onReport={report} />));
    const old = FakeHls.instances[0]!;
    await act(async () => root.render(<InAppPlayer sources={[source("/b.m3u8")]} onReport={report} />));
    expect(old.destroy).toHaveBeenCalledOnce();
    expect(FakeHls.instances[1]?.loadSource).toHaveBeenCalledWith("/b.m3u8");
    await act(async () => old.callbacks.get("error")?.("error", { fatal: true, details: "late" }));
    expect(report).not.toHaveBeenCalled();
    expect(FakeHls.instances[1]?.destroy).not.toHaveBeenCalled();
  });

  it("resets source selection when the movie changes", async () => {
    await act(async () => root.render(<InAppPlayer sources={[source("/a.mp4"), source("/b.mp4")]} />));
    const video = container.querySelector("video")!;
    await act(async () => video.dispatchEvent(new Event("error")));
    expect(container.querySelector("video")?.getAttribute("src")).toBe("/b.mp4");
    await act(async () => root.render(<InAppPlayer sources={[source("/c.mp4")]} />));
    expect(container.querySelector("video")?.getAttribute("src")).toBe("/c.mp4");
    expect(HTMLMediaElement.prototype.pause).toHaveBeenCalled();
  });

  it("does not revisit failed sources when the parent recreates the same list", async () => {
    const render = () => <InAppPlayer sources={[source("/a.mp4"), source("/b.mp4")]} />;
    await act(async () => root.render(render()));
    await act(async () => container.querySelector("video")!.dispatchEvent(new Event("error")));
    expect(container.querySelector("video")?.getAttribute("src")).toBe("/b.mp4");
    await act(async () => root.render(render()));
    await act(async () => container.querySelector("video")!.dispatchEvent(new Event("error")));
    expect(container.querySelector("video")?.getAttribute("src")).toBe("/b.mp4");
    expect(container.textContent).toContain("媒体错误");
  });

  it("focuses the lightbox, keeps keys inside it and restores focus on close", async () => {
    const opener = document.createElement("button");
    document.body.append(opener);
    opener.focus();
    const close = vi.fn();
    const index = vi.fn();
    const pageKey = vi.fn();
    window.addEventListener("keydown", pageKey);
    await act(async () => root.render(
      <Lightbox urls={["/a.jpg", "/b.jpg"]} index={5} onClose={close} onIndex={index} />,
    ));
    const dialog = container.querySelector<HTMLElement>('[role="dialog"]')!;
    expect(document.activeElement).toBe(dialog);
    expect(document.body.style.overflow).toBe("hidden");
    expect(container.querySelector("img")?.getAttribute("src")).toBe("/b.jpg");
    await act(async () => dialog.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true })));
    expect(index).toHaveBeenCalledWith(0);
    expect(pageKey).not.toHaveBeenCalled();
    await act(async () => dialog.dispatchEvent(new KeyboardEvent("keydown", { key: "Tab", bubbles: true })));
    expect(document.activeElement?.getAttribute("aria-label")).toBe("关闭预览");
    await act(async () => (document.activeElement as HTMLElement).click());
    expect(close).toHaveBeenCalledOnce();
    await act(async () => root.render(null));
    expect(document.activeElement).toBe(opener);
    expect(document.body.style.overflow).toBe("");
    window.removeEventListener("keydown", pageKey);
    opener.remove();
  });
});
