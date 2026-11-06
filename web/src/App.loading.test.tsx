import { notifyManager } from "@tanstack/react-query";
import { queryClient } from "./query/client";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "./App";
import { api } from "./api";
import { libraryPrefsSchema, workSchema, type Work } from "./model";
import { memoryStorage } from "./test/storage";

vi.mock("./api", () => ({ appUrl: (path: string) => path, api: {
  libraries: vi.fn(), libraryPrefs: vi.fn(), tasks: vi.fn(), works: vi.fn(),
  actors: vi.fn(), nonJavActors: vi.fn(), assets: vi.fn(), setWantList: vi.fn(),
} }));
vi.mock("./panels", () => ({
  Works: ({ works, onToggleWant }: { works: Work[]; onToggleWant: (work: Work) => void }) => (
    <div data-works>{works.map((work) => (
      <button key={work.id} onClick={() => onToggleWant(work)}>{work.id}:{String(work.want_list)}</button>
    ))}</div>
  ),
  Actors: () => <div>actors-ready</div>, Inbox: () => null,
}));
vi.mock("./views/WorkDetailView", () => ({ WorkDetailView: () => <div>detail-ready</div> }));
vi.mock("./components/DiscoverPanel", () => ({ DiscoverPanel: ({ onToggleWant }: { onToggleWant: (id: string, wanted: boolean) => void }) => (
  <button onClick={() => onToggleWant("fixture", true)}>want-from-ranking</button>
) }));
vi.mock("./views/SwipeFeedView", () => ({ SwipeFeedView: ({ onLibraryMode }: { onLibraryMode: (active: boolean) => void }) => (
  <button onClick={() => onLibraryMode(true)}>load-library-feed</button>
) }));

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}
const work = workSchema.parse({ id: "fixture", title: "Fixture", family: "jav", category: "Japan", created_at: "", updated_at: "" });

describe("page load request budget", () => {
  let root: Root;
  let container: HTMLDivElement;
  beforeEach(() => {
    queryClient.clear();
    notifyManager.setScheduler(queueMicrotask);
    vi.clearAllMocks();
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
    vi.stubGlobal("fetch", vi.fn(async (url: string) =>
      new Response(JSON.stringify(url.endsWith("/works/query")
        ? { items: [], next_cursor: null, total: 0 }
        : url.includes("/people-cards") ? { items: [], next_offset: null, labels: {} }
        : url.endsWith("/facets") ? {} : []))));
    vi.spyOn(window, "scrollTo").mockImplementation(() => undefined);
    window.history.replaceState({}, "", "/");
    vi.mocked(api.libraries).mockResolvedValue([]);
    vi.mocked(api.libraryPrefs).mockResolvedValue(libraryPrefsSchema.parse({}));
    vi.mocked(api.tasks).mockResolvedValue([]);
    vi.mocked(api.works).mockResolvedValue([work]);
    vi.mocked(api.actors).mockResolvedValue([]);
    vi.mocked(api.nonJavActors).mockResolvedValue([]);
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
  });
  afterEach(async () => {
    await act(async () => root.unmount());
    queryClient.clear();
    container.remove();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  async function nav(label: string) {
    const button = [...container.querySelectorAll("nav button")].find((item) => item.textContent?.includes(label));
    if (!button) throw new Error(`Missing nav: ${label}`);
    await act(async () => (button as HTMLElement).click());
  }

  it("renders the first visit over LAN HTTP without the secure-context UUID API", async () => {
    vi.stubGlobal("localStorage", memoryStorage());
    vi.stubGlobal("crypto", { getRandomValues: crypto.getRandomValues.bind(crypto) });
    await act(async () => root.render(<App />));
    expect(container.textContent).toContain("作品库");
    expect(container.querySelector('input[aria-label="片单标识"]')).not.toBeNull();
    expect(localStorage.getItem("mdc:catalog-owner")).toBeTruthy();
  });

  async function openLegacy(label = "原有筛选与批量工具") {
    const summary = [...container.querySelectorAll("summary")].find(item => item.textContent === label)!;
    await act(async () => {
      const details = summary.parentElement as HTMLDetailsElement;
      details.open = true;
      details.dispatchEvent(new Event("toggle"));
    });
  }

  it("does not refetch the library when preferences arrive during its first request", async () => {
    const prefs = deferred<Awaited<ReturnType<typeof api.libraryPrefs>>>();
    const rows = deferred<Work[]>();
    vi.mocked(api.libraryPrefs).mockReturnValue(prefs.promise);
    vi.mocked(api.works).mockReturnValue(rows.promise);
    // Unrelated slow task history must not delay the want-list overlay.
    vi.mocked(api.tasks).mockReturnValue(new Promise(() => undefined));
    await act(async () => root.render(<App />));
    await openLegacy();
    await act(async () => prefs.resolve(libraryPrefsSchema.parse({ want_list: [work.id] })));
    expect(api.works).toHaveBeenCalledTimes(1);
    await act(async () => rows.resolve([work]));
    expect(container.querySelector("[data-works]")?.textContent).toBe("fixture:true");
    expect(api.works).toHaveBeenCalledTimes(1);
  });

  it("does not load the whole library for a work detail deep link", async () => {
    window.history.replaceState({}, "", "/?work=fixture");
    await act(async () => root.render(<App />));
    expect(container.textContent).toContain("detail-ready");
    expect(api.works).not.toHaveBeenCalled();
  });

  it("shows paginated works without waiting for facets or downloading the legacy library", async () => {
    const facets = deferred<Response>();
    const fetch = vi.fn(async (url: string) => {
      if (url.endsWith("/facets")) return facets.promise;
      if (url.endsWith("/works/query")) return new Response(JSON.stringify({
        items: [{ id: "page", title: "Paged work", actors: [], tags: [], poster: "" }],
        next_cursor: "more", total: 2,
      }));
      return new Response("[]");
    });
    vi.stubGlobal("fetch", fetch);
    await act(async () => root.render(<App />));
    expect(container.querySelector(".work-grid")?.textContent).toContain("Paged work");
    expect(api.works).not.toHaveBeenCalled();
    expect(api.actors).not.toHaveBeenCalled();
    expect(api.nonJavActors).not.toHaveBeenCalled();
    expect(fetch.mock.calls.some(([url]) => url.includes("/people"))).toBe(false);
    expect(api.libraryPrefs).toHaveBeenCalledTimes(1);
    expect(api.tasks).not.toHaveBeenCalled();
    await act(async () => facets.resolve(new Response(null, { status: 502 })));
    expect(container.querySelector(".work-grid")?.textContent).toContain("Paged work");
    expect(container.textContent).toContain("分类统计读取失败");
  });

  it("loads actor cards one page at a time and fetches the selected profile on demand", async () => {
    window.history.replaceState({}, "", "/?view=actors");
    const fetch = vi.fn(async (url: string) => {
      if (url.includes("/people-cards?") && !url.includes("?id=")) {
        const second = url.includes("offset=48");
        const name = second ? "Second" : "First";
        return new Response(JSON.stringify({
          items: [{ id: name, name, aliases: [], work_count: 1 }],
          next_offset: second ? null : 48,
        }));
      }
      if (url.endsWith("/people/Second")) return new Response(JSON.stringify({
        id: "Second", name: "Second", ref: "person:Second", biography: "Full biography",
      }));
      if (url.endsWith("/works/query")) return new Response('{"items":[],"next_cursor":null,"total":0}');
      return new Response(url.includes("/people-cards?") ? '{"labels":{"Second":"Second"}}' : "[]");
    });
    vi.stubGlobal("fetch", fetch);
    await act(async () => root.render(<App />));
    expect(container.querySelector(".actor-grid")?.textContent).toContain("First");
    expect(api.actors).not.toHaveBeenCalled();
    expect(api.nonJavActors).not.toHaveBeenCalled();
    expect(fetch.mock.calls.some(([url]) => url.endsWith("/people/First"))).toBe(false);
    await act(async () => [...container.querySelectorAll("button")]
      .find(button => button.textContent === "下一页演员")!.click());
    expect(container.querySelector(".actor-grid")?.textContent).toContain("Second");
    await act(async () => container.querySelector<HTMLButtonElement>(".actor-card")!.click());
    expect(container.textContent).toContain("Full biography");
  });

  it("shows actor content while the other directory is still pending", async () => {
    window.history.replaceState({}, "", "/?view=actors");
    vi.mocked(api.nonJavActors).mockReturnValue(new Promise(() => undefined));
    await act(async () => root.render(<App />));
    await openLegacy("维护演员资料");
    expect(container.textContent).toContain("actors-ready");
    expect(container.textContent).toContain("演员资料仍在更新");
    expect(api.works).not.toHaveBeenCalled();
    expect(api.tasks).not.toHaveBeenCalled();
  });

  it("does not download the library until the local swipe feed is selected", async () => {
    window.history.replaceState({}, "", "/?view=swipe");
    await act(async () => root.render(<App />));
    expect(api.works).not.toHaveBeenCalled();
    await act(async () => [...container.querySelectorAll("button")]
      .find((button) => button.textContent === "load-library-feed")!.click());
    expect(api.works).toHaveBeenCalledTimes(1);
  });

  it("does not turn an unloaded library into a cached empty list when a ranking item is marked wanted", async () => {
    window.history.replaceState({}, "", "/?view=rankings");
    vi.mocked(api.setWantList).mockResolvedValue(libraryPrefsSchema.parse({ want_list: ["fixture"] }));
    await act(async () => root.render(<App />));
    await act(async () => [...container.querySelectorAll("button")].find((button) => button.textContent === "want-from-ranking")!.click());
    expect(api.works).not.toHaveBeenCalled();
    await nav("影片");
    await openLegacy();
    expect(api.works).toHaveBeenCalledTimes(1);
    expect(container.querySelector("[data-works]")?.textContent).toBe("fixture:true");
  });

  it("shares fulfilled resources across app remounts", async () => {
    await act(async () => root.render(<App />));
    await openLegacy();
    await act(async () => root.render(null));
    await act(async () => root.render(<App />));
    await openLegacy();
    expect(container.querySelector("[data-works]")?.textContent).toBe("fixture:false");
    expect(api.works).toHaveBeenCalledTimes(1);
    expect(api.libraryPrefs).toHaveBeenCalledTimes(1);
  });

  it("force-refreshes visible queries together without blanking existing content", async () => {
    await act(async () => root.render(<App />));
    await openLegacy();
    const prefs = deferred<Awaited<ReturnType<typeof api.libraryPrefs>>>();
    const rows = deferred<Work[]>();
    vi.mocked(api.libraryPrefs).mockReturnValue(prefs.promise);
    vi.mocked(api.works).mockReturnValue(rows.promise);
    await act(async () => [...container.querySelectorAll("button")].find((button) => button.textContent === "刷新")!.click());
    expect(api.libraryPrefs).toHaveBeenCalledTimes(2);
    expect(api.works).toHaveBeenCalledTimes(2);
    expect(container.querySelector("[data-works]")).not.toBeNull();
    expect(api.tasks).not.toHaveBeenCalled();
    await act(async () => { prefs.resolve(libraryPrefsSchema.parse({})); rows.resolve([work]); });
  });

  it("deduplicates a pending list request when switching pages", async () => {
    const rows = deferred<Work[]>();
    vi.mocked(api.works).mockReturnValue(rows.promise);
    await act(async () => root.render(<App />));
    await openLegacy();
    await nav("演员");
    await nav("影片");
    expect(api.works).toHaveBeenCalledTimes(1);
    await act(async () => rows.resolve([work]));
    expect(container.querySelector("[data-works]")?.textContent).toBe("fixture:false");
  });

  it("updates want-list state without downloading the library again", async () => {
    vi.mocked(api.setWantList).mockResolvedValue(libraryPrefsSchema.parse({ want_list: [work.id] }));
    await act(async () => root.render(<App />));
    await openLegacy();
    await act(async () => container.querySelector<HTMLButtonElement>("[data-works] button")!.click());
    expect(container.querySelector("[data-works]")?.textContent).toBe("fixture:true");
    expect(api.works).toHaveBeenCalledTimes(1);
  });
});
