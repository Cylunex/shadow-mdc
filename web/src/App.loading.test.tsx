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
