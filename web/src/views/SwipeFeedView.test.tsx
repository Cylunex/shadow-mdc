import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../api";
import type { PlayerSource } from "../components/InAppPlayer";
import { workSchema } from "../model";
import { SwipeFeedView } from "./SwipeFeedView";

vi.mock("../api", () => ({
  appUrl: (path: string) => path,
  api: { swipeFeed: vi.fn(), swipeResolve: vi.fn(), workPlayResolve: vi.fn() },
}));
vi.mock("../components/CoverImage", () => ({ CoverImage: () => null }));
vi.mock("../components/InAppPlayer", () => ({
  InAppPlayer: ({ sources }: { sources: PlayerSource[] }) => (
    <div data-player-source={sources[0]?.url} />
  ),
}));

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

const items = ["a", "b", "c", "d"].map((id) => ({ site: "fixture", id, title: id }));
const works = ["first", "second"].map((id) => workSchema.parse({
  id, title: id, family: "fixture", category: "Other", has_local_media: true,
  created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z",
}));

class CardObserver {
  static instances: CardObserver[] = [];
  targets = new Set<Element>();
  constructor(private callback: IntersectionObserverCallback) {
    CardObserver.instances.push(this);
  }
  observe(target: Element) { this.targets.add(target); }
  disconnect() { this.targets.clear(); }
  show(target: Element) {
    if (!this.targets.has(target)) return;
    this.callback([
      { target, isIntersecting: true, intersectionRatio: 1 } as IntersectionObserverEntry,
    ], this as unknown as IntersectionObserver);
  }
}

describe("SwipeFeedView request lifecycle", () => {
  let container: HTMLDivElement;
  let root: Root;
  const report = vi.fn();

  beforeEach(() => {
    vi.clearAllMocks();
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
    vi.stubGlobal("IntersectionObserver", CardObserver);
    CardObserver.instances = [];
    vi.mocked(api.swipeFeed).mockResolvedValue({ items, hasMore: false });
    vi.mocked(api.swipeResolve).mockResolvedValue({ url: "https://example.com/a.mp4" });
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  async function render() {
    await act(async () => root.render(
      <SwipeFeedView works={works} loading={false} onOpenDetail={vi.fn()} report={report} />,
    ));
  }

  async function show(key: string) {
    const card = container.querySelector(`[data-swipe-key="${key}"]`);
    if (!card) throw new Error(`Missing card: ${key}`);
    await act(async () => {
      [...CardObserver.instances].forEach((observer) => observer.show(card));
    });
  }

  async function click(label: string, parent: ParentNode = container) {
    const button = [...parent.querySelectorAll("button")].find((node) => node.textContent === label);
    if (!button) throw new Error(`Missing button: ${label}`);
    await act(async () => button.click());
  }

  function playerUrl() {
    return container.querySelector("[data-player-source]")?.getAttribute("data-player-source");
  }

  it("keeps the current resolve alive when the next page arrives", async () => {
    const page = deferred<Awaited<ReturnType<typeof api.swipeFeed>>>();
    const resolve = deferred<Awaited<ReturnType<typeof api.swipeResolve>>>();
    vi.mocked(api.swipeFeed)
      .mockResolvedValueOnce({ items, hasMore: true, nextCursor: "next" })
      .mockReturnValueOnce(page.promise);
    vi.mocked(api.swipeResolve).mockReturnValue(resolve.promise);
    await render();
    await show("fixture::b");
    const signal = vi.mocked(api.swipeResolve).mock.calls[0]?.[1];
    await act(async () => page.resolve({ items: [{ site: "fixture", id: "e" }], hasMore: false }));
    expect(api.swipeResolve).toHaveBeenCalledTimes(1);
    expect(signal?.aborted).toBe(false);
    await act(async () => resolve.resolve({ url: "https://example.com/b.mp4" }));
    expect(playerUrl()).toBe("https://example.com/b.mp4");
  });

  it("pauses failed pagination until the user retries", async () => {
    vi.mocked(api.swipeFeed)
      .mockResolvedValueOnce({ items, hasMore: true, nextCursor: "next" })
      .mockRejectedValueOnce(new Error("temporarily unavailable"))
      .mockResolvedValueOnce({ items: [{ site: "fixture", id: "e" }], hasMore: false });
    await render();
    await show("fixture::b");
    await show("fixture::c");
    expect(api.swipeFeed).toHaveBeenCalledTimes(2);
    await click("重试加载");
    expect(api.swipeFeed).toHaveBeenCalledTimes(3);
    expect(container.querySelector('[data-swipe-key="fixture::e"]')).not.toBeNull();
    expect(api.swipeResolve).toHaveBeenCalledTimes(2);
  });

  it("aborts old requests and clears the resolving indicator on a cache hit", async () => {
    const pending = deferred<Awaited<ReturnType<typeof api.swipeResolve>>>();
    vi.mocked(api.swipeResolve)
      .mockResolvedValueOnce({ url: "https://example.com/a.mp4" })
      .mockReturnValueOnce(pending.promise);
    await render();
    await show("fixture::a");
    await show("fixture::b");
    expect(playerUrl()).toBeUndefined();
    const signal = vi.mocked(api.swipeResolve).mock.calls[1]?.[1];
    await show("fixture::a");
    expect(signal?.aborted).toBe(true);
    expect(playerUrl()).toBe("https://example.com/a.mp4");
    expect(container.textContent).not.toContain("解析中");
    await act(async () => pending.resolve({ url: "https://example.com/b.mp4" }));
    expect(playerUrl()).toBe("https://example.com/a.mp4");
    expect(api.swipeResolve).toHaveBeenCalledTimes(2);
  });

  it("retries empty results when revisiting a card", async () => {
    vi.mocked(api.swipeResolve).mockResolvedValueOnce({ qualities: [] });
    await render();
    await show("fixture::a");
    await show("fixture::b");
    await show("fixture::a");
    expect(api.swipeResolve).toHaveBeenCalledTimes(3);
    expect(playerUrl()).toBe("https://example.com/a.mp4");
  });

  it("refreshes expired media links", async () => {
    let now = 1000;
    vi.spyOn(Date, "now").mockImplementation(() => now);
    await render();
    await show("fixture::a");
    await show("fixture::b");
    now += 5 * 60 * 1000 + 1;
    await show("fixture::a");
    expect(api.swipeResolve).toHaveBeenCalledTimes(3);
  });

  it("bounds cached media links during a long session", async () => {
    vi.mocked(api.swipeFeed).mockResolvedValue({
      items: Array.from({ length: 49 }, (_, id) => ({ site: "fixture", id: String(id) })),
      hasMore: false,
    });
    await render();
    for (let id = 0; id < 49; id += 1) await show(`fixture::${id}`);
    await show("fixture::0");
    expect(api.swipeResolve).toHaveBeenCalledTimes(50);
  });

  it("aborts a pending tube resolve on unmount", async () => {
    const pending = deferred<Awaited<ReturnType<typeof api.swipeResolve>>>();
    vi.mocked(api.swipeResolve).mockReturnValue(pending.promise);
    await render();
    await show("fixture::a");
    const signal = vi.mocked(api.swipeResolve).mock.calls[0]?.[1];
    await act(async () => root.render(null));
    expect(signal?.aborted).toBe(true);
    await act(async () => pending.resolve({ error: "late error" }));
    expect(report).not.toHaveBeenCalled();
  });

  it("normalizes identifiers and removes duplicate cards within a page", async () => {
    vi.mocked(api.swipeFeed).mockResolvedValue({
      items: [{ site: "fixture", id: 1 }, { site: "fixture", id: "1" }], hasMore: false,
    });
    await render();
    expect(container.querySelectorAll("[data-swipe-key]")).toHaveLength(1);
    await show("fixture::1");
    expect(api.swipeResolve).toHaveBeenCalledWith(
      { site: "fixture", id: "1", pageUrl: undefined }, expect.any(AbortSignal),
    );
  });

  it("keeps the latest library selection when responses arrive out of order", async () => {
    const first = deferred<Awaited<ReturnType<typeof api.workPlayResolve>>>();
    const second = deferred<Awaited<ReturnType<typeof api.workPlayResolve>>>();
    vi.mocked(api.workPlayResolve).mockImplementation((id) => id === "first" ? first.promise : second.promise);
    await render();
    await click("本库");
    await click("在线播放", container.querySelector('[data-swipe-key="first"]')!);
    await click("在线播放", container.querySelector('[data-swipe-key="second"]')!);
    const source = (id: string) => ({
      work_id: id, emby_in_library: false,
      sources: [{ kind: "vod", label: id, url: `https://example.com/${id}.mp4`, playable_in_app: true }],
    });
    await act(async () => second.resolve(source("second")));
    await act(async () => first.resolve(source("first")));
    expect(playerUrl()).toBe("https://example.com/second.mp4");
    expect(vi.mocked(api.workPlayResolve).mock.calls[0]?.[1]?.aborted).toBe(true);
    await show("first");
    expect(playerUrl()).toBeUndefined();
  });

  it("cancels pending library playback when switching modes", async () => {
    const pending = deferred<Awaited<ReturnType<typeof api.workPlayResolve>>>();
    vi.mocked(api.workPlayResolve).mockReturnValue(pending.promise);
    await render();
    await click("本库");
    await click("在线播放", container.querySelector('[data-swipe-key="first"]')!);
    await click("外站");
    expect(vi.mocked(api.workPlayResolve).mock.calls[0]?.[1]?.aborted).toBe(true);
    await act(async () => pending.resolve({ work_id: "first", sources: [], emby_in_library: false }));
    expect(report).not.toHaveBeenCalled();
    await click("本库");
    expect(container.textContent).not.toContain("解析中");
  });
});
