import { notifyManager } from "@tanstack/react-query";
import { queryClient } from "./query/client";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "./App";
import { api } from "./api";
import { libraryPrefsSchema, workSchema, type Work } from "./model";

vi.mock("./api", () => ({ api: {
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

  it("does not refetch the library when preferences arrive during its first request", async () => {
    const prefs = deferred<Awaited<ReturnType<typeof api.libraryPrefs>>>();
    const rows = deferred<Work[]>();
    vi.mocked(api.libraryPrefs).mockReturnValue(prefs.promise);
    vi.mocked(api.works).mockReturnValue(rows.promise);
    // Unrelated slow task history must not delay the want-list overlay.
    vi.mocked(api.tasks).mockReturnValue(new Promise(() => undefined));
    await act(async () => root.render(<App />));
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

  it("starts only works and preferences on a completely cold film page", async () => {
    await act(async () => root.render(<App />));
    expect(api.works).toHaveBeenCalledTimes(1);
    expect(api.libraryPrefs).toHaveBeenCalledTimes(1);
    expect(api.tasks).not.toHaveBeenCalled();
    expect(api.libraries).not.toHaveBeenCalled();
    expect(api.assets).not.toHaveBeenCalled();
    expect(api.actors).not.toHaveBeenCalled();
    expect(api.nonJavActors).not.toHaveBeenCalled();
  });

  it("shows actor content while the other directory is still pending", async () => {
    window.history.replaceState({}, "", "/?view=actors");
    vi.mocked(api.nonJavActors).mockReturnValue(new Promise(() => undefined));
    await act(async () => root.render(<App />));
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
    expect(api.works).toHaveBeenCalledTimes(1);
    expect(container.querySelector("[data-works]")?.textContent).toBe("fixture:true");
  });

  it("shares fulfilled resources across app remounts", async () => {
    await act(async () => root.render(<App />));
    await act(async () => root.render(null));
    await act(async () => root.render(<App />));
    expect(container.querySelector("[data-works]")?.textContent).toBe("fixture:false");
    expect(api.works).toHaveBeenCalledTimes(1);
    expect(api.libraryPrefs).toHaveBeenCalledTimes(1);
  });

  it("force-refreshes visible queries together without blanking existing content", async () => {
    await act(async () => root.render(<App />));
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
    await nav("演员");
    await nav("影片");
    expect(api.works).toHaveBeenCalledTimes(1);
    await act(async () => rows.resolve([work]));
    expect(container.querySelector("[data-works]")?.textContent).toBe("fixture:false");
  });

  it("updates want-list state without downloading the library again", async () => {
    vi.mocked(api.setWantList).mockResolvedValue(libraryPrefsSchema.parse({ want_list: [work.id] }));
    await act(async () => root.render(<App />));
    await act(async () => container.querySelector<HTMLButtonElement>("[data-works] button")!.click());
    expect(container.querySelector("[data-works]")?.textContent).toBe("fixture:true");
    expect(api.works).toHaveBeenCalledTimes(1);
  });
});
