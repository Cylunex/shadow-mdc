import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { notifyManager, useQuery } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../api";
import { workDetailSchema } from "../model";
import { invalidateLibrary, queryClient, queryKeys } from "./client";
import { resources } from "./resources";

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}
const work = (title = "original") => workDetailSchema.parse({
  id: "fixture", title, family: "jav", category: "Japan", created_at: "", updated_at: "",
});
const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status });

describe("shared resource queries through the real API transport", () => {
  let root: Root;
  let container: HTMLDivElement;
  let fetchMock: ReturnType<typeof vi.fn<typeof fetch>>;
  function Reader({ name }: { name: string }) {
    const query = useQuery(resources.detail("fixture"), queryClient);
    return <output data-reader={name}>{query.data?.title ?? query.error?.message ?? "pending"}</output>;
  }
  beforeEach(() => {
    queryClient.clear();
    notifyManager.setScheduler(queueMicrotask);
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
    fetchMock = vi.fn<typeof fetch>();
    vi.stubGlobal("fetch", fetchMock);
    container = document.createElement("div");
    root = createRoot(container);
  });
  afterEach(async () => {
    await act(async () => root.unmount());
    queryClient.clear();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("coalesces two components and an imperative reader into one uncached request", async () => {
    const pending = deferred<Response>();
    fetchMock.mockReturnValue(pending.promise);
    await act(async () => root.render(<><Reader name="a" /><Reader name="b" /></>));
    const imperative = queryClient.fetchQuery(resources.detail("fixture"));
    expect(fetchMock).toHaveBeenCalledTimes(1);
    await act(async () => pending.resolve(json(work())));
    expect((await imperative).title).toBe("original");
    expect([...container.querySelectorAll("output")].map((item) => item.textContent)).toEqual(["original", "original"]);
  });

  it("keeps a shared request alive until the last component leaves", async () => {
    const pending = deferred<Response>();
    fetchMock.mockReturnValue(pending.promise);
    await act(async () => root.render(<><Reader key="a" name="a" /><Reader key="b" name="b" /></>));
    const signal = fetchMock.mock.calls[0]![1]!.signal!;
    await act(async () => root.render(<Reader key="b" name="b" />));
    expect(signal.aborted).toBe(false);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    await act(async () => root.render(null));
    expect(signal.aborted).toBe(true);
    await act(async () => pending.resolve(json(work("late"))));
    expect(queryClient.getQueryData(queryKeys.detail("fixture"))).toBeUndefined();
  });

  it("discards a pre-mutation response and updates every reader after invalidation", async () => {
    const old = deferred<Response>();
    fetchMock.mockReturnValueOnce(old.promise).mockImplementation(async (_url, init) =>
      json(work(init?.method === "PATCH" ? "saved" : "fresh")));
    await act(async () => root.render(<><Reader name="a" /><Reader name="b" /></>));
    await act(async () => { await api.updateWork("fixture", { title: "saved" }); });
    expect(container.textContent).toBe("freshfresh");
    await act(async () => old.resolve(json(work("stale"))));
    expect(container.textContent).toBe("freshfresh");
    expect(fetchMock).toHaveBeenCalledTimes(3); // one old GET, PATCH, one shared new GET
  });

  it("applies preference snapshots without downloading works or accepting a stale pending read", async () => {
    const old = deferred<Response>();
    fetchMock.mockReturnValueOnce(old.promise).mockResolvedValue(json({ want_list: ["fixture"] }));
    const initial = queryClient.fetchQuery(resources.prefs()).catch(() => undefined);
    await api.setWantList("fixture", true);
    old.resolve(json({ want_list: [] }));
    await initial;
    expect(queryClient.getQueryData(queryKeys.prefs)).toMatchObject({ want_list: ["fixture"] });
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(fetchMock.mock.calls.every(([url]) => String(url).includes("library-prefs"))).toBe(true);
  });

  it("does not cache failures or retry a cold request behind the user's back", async () => {
    fetchMock.mockResolvedValueOnce(json({ detail: "offline" }, 503)).mockImplementation(async () => json(work()));
    await expect(queryClient.fetchQuery(resources.detail("fixture"))).rejects.toThrow("offline");
    expect(fetchMock).toHaveBeenCalledTimes(1);
    await expect(queryClient.fetchQuery(resources.detail("fixture"))).resolves.toMatchObject({ title: "original" });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("keeps stale content visible while one background refresh supplies all readers", async () => {
    let now = Date.now();
    vi.spyOn(Date, "now").mockImplementation(() => now);
    fetchMock.mockImplementation(async () => json(work()));
    await act(async () => root.render(<Reader name="a" />));
    await act(async () => root.render(null));
    now += 30_001;
    const fresh = deferred<Response>();
    fetchMock.mockReturnValue(fresh.promise);
    await act(async () => root.render(<><Reader name="a" /><Reader name="b" /></>));
    expect(container.textContent).toBe("originaloriginal");
    expect(fetchMock).toHaveBeenCalledTimes(2);
    await act(async () => fresh.resolve(json(work("refreshed"))));
    expect(container.textContent).toBe("refreshedrefreshed");
  });

  it("rejects invalid payloads and preserves a valid cache after failed writes", async () => {
    fetchMock.mockResolvedValueOnce(json({ id: "bad" })).mockImplementation(async () => json(work()));
    await expect(queryClient.fetchQuery(resources.detail("fixture"))).rejects.toThrow();
    expect(queryClient.getQueryData(queryKeys.detail("fixture"))).toBeUndefined();
    await queryClient.fetchQuery(resources.detail("fixture"));
    fetchMock.mockResolvedValue(json({ detail: "rejected" }, 409));
    await expect(api.updateWork("fixture", { title: "change" })).rejects.toThrow("rejected");
    expect(queryClient.getQueryState(queryKeys.detail("fixture"))?.isInvalidated).toBe(false);
    expect(queryClient.getQueryData(queryKeys.detail("fixture"))).toMatchObject({ title: "original" });
  });

  it("separates resource variants and refetches only after their freshness expires", async () => {
    let now = Date.now();
    vi.spyOn(Date, "now").mockImplementation(() => now);
    fetchMock.mockImplementation(async () => json({ categories: [] }));
    await Promise.all([
      queryClient.fetchQuery(resources.categories(false)), queryClient.fetchQuery(resources.categories(true)),
    ]);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    await queryClient.fetchQuery(resources.categories(false));
    expect(fetchMock).toHaveBeenCalledTimes(2);
    now += 30_001;
    await queryClient.fetchQuery(resources.categories(false));
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it("marks inactive library resources stale without eagerly downloading them", async () => {
    queryClient.setQueryData(queryKeys.works, []);
    queryClient.setQueryData(queryKeys.actors, []);
    await invalidateLibrary();
    expect(fetchMock).not.toHaveBeenCalled();
    expect(queryClient.getQueryState(queryKeys.works)?.isInvalidated).toBe(true);
    expect(queryClient.getQueryState(queryKeys.actors)?.isInvalidated).toBe(true);
  });

  it("bounds inactive detail retention during long browsing sessions", () => {
    for (let i = 0; i < 160; i++) queryClient.setQueryData(queryKeys.detail(String(i)), work());
    expect(queryClient.getQueryCache().getAll().length).toBeLessThanOrEqual(96);
    expect(queryClient.getQueryData(queryKeys.detail("159"))).toBeDefined();
  });

  it("executes distinct writes independently", async () => {
    fetchMock.mockImplementation(async () => json({ want_list: ["fixture"] }));
    await Promise.all([api.setWantList("fixture", true), api.setWantList("other", false)]);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(fetchMock.mock.calls.map(([, init]) => init?.method)).toEqual(["PUT", "PUT"]);
  });

  it("does not invalidate the library for read-only POST playback resolution", async () => {
    queryClient.setQueryData(queryKeys.works, []);
    fetchMock.mockResolvedValue(json({ url: "https://example.com/fixture.mp4" }));
    await api.swipeResolve({ site: "fixture", id: "fixture" });
    expect(queryClient.getQueryState(queryKeys.works)?.isInvalidated).toBe(false);
  });
});
