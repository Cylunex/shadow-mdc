import { notifyManager } from "@tanstack/react-query";
import { queryClient, queryKeys } from "../query/client";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../api";
import { libraryPrefsSchema, workDetailSchema } from "../model";
import { useWorkPage } from "./useWorkPage";

vi.mock("../api", () => ({ api: {
  workDetail: vi.fn(), workRelated: vi.fn(), workOfflineTasks: vi.fn(), libraryPrefs: vi.fn(),
} }));

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}
function detail(id: string) {
  return workDetailSchema.parse({ id, title: id, family: "jav", category: "Japan", created_at: "", updated_at: "" });
}

describe("progressive detail loading", () => {
  let root: Root;
  let container: HTMLDivElement;
  let page: ReturnType<typeof useWorkPage>;
  function Harness({ id = "first" }) {
    page = useWorkPage(id);
    return <output>{page.loading ? "loading" : page.detail?.title ?? page.loadError}</output>;
  }
  beforeEach(() => {
    queryClient.clear();
    notifyManager.setScheduler(queueMicrotask);
    vi.clearAllMocks();
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
    vi.mocked(api.workDetail).mockImplementation(async (id) => detail(id));
    vi.mocked(api.workRelated).mockResolvedValue({ by_actor: [], by_tag: [] });
    vi.mocked(api.workOfflineTasks).mockResolvedValue([]);
    vi.mocked(api.libraryPrefs).mockResolvedValue(libraryPrefsSchema.parse({}));
    container = document.createElement("div");
    root = createRoot(container);
  });
  afterEach(async () => {
    await act(async () => root.unmount());
    queryClient.clear();
    vi.unstubAllGlobals();
  });

  it("shows primary content while recommendation and offline requests are pending", async () => {
    const related = deferred<Awaited<ReturnType<typeof api.workRelated>>>();
    const offline = deferred<Awaited<ReturnType<typeof api.workOfflineTasks>>>();
    vi.mocked(api.workRelated).mockReturnValue(related.promise);
    vi.mocked(api.workOfflineTasks).mockReturnValue(offline.promise);
    await act(async () => root.render(<Harness />));
    expect(container.textContent).toBe("first");
    expect(page.loading).toBe(false);
    await act(async () => related.resolve({ by_actor: [detail("related")], by_tag: [] }));
    expect(page.related.by_actor[0]?.id).toBe("related");
    expect(container.textContent).toBe("first");
  });

  it("aborts previous requests and ignores late detail results after navigation", async () => {
    const old = deferred<Awaited<ReturnType<typeof api.workDetail>>>();
    vi.mocked(api.workDetail).mockReturnValueOnce(old.promise);
    await act(async () => root.render(<Harness />));
    expect(api.workRelated).not.toHaveBeenCalled();
    expect(api.workOfflineTasks).not.toHaveBeenCalled();
    const signal = vi.mocked(api.workDetail).mock.calls[0]?.[1];
    await act(async () => root.render(<Harness id="second" />));
    expect(signal?.aborted).toBe(true);
    expect(container.textContent).toBe("second");
    await act(async () => old.resolve(detail("first")));
    expect(container.textContent).toBe("second");
  });

  it("reuses a complete detail when navigating away and back", async () => {
    await act(async () => root.render(<Harness />));
    await act(async () => root.render(<Harness id="second" />));
    await act(async () => root.render(<Harness />));
    expect(container.textContent).toBe("first");
    expect(api.workDetail).toHaveBeenCalledTimes(2);
  });

  it("updates the want badge from shared preferences without another detail download", async () => {
    await act(async () => root.render(<Harness />));
    await act(async () => { queryClient.setQueryData(queryKeys.prefs, libraryPrefsSchema.parse({ want_list: ["first"] })); });
    expect(page.detail?.want_list).toBe(true);
    expect(api.workDetail).toHaveBeenCalledTimes(1);
  });

  it("keeps primary content usable when optional requests fail", async () => {
    vi.mocked(api.workRelated).mockRejectedValue(new Error("optional error"));
    vi.mocked(api.workOfflineTasks).mockRejectedValue(new Error("optional error"));
    await act(async () => root.render(<Harness />));
    expect(container.textContent).toBe("first");
    expect(page.loadError).toBeNull();
  });

  it("shows primary failures and supports a fresh reload", async () => {
    vi.mocked(api.workDetail).mockRejectedValueOnce(new Error("detail error"));
    await act(async () => root.render(<Harness />));
    expect(container.textContent).toBe("detail error");
    await act(async () => page.reload("first"));
    expect(container.textContent).toBe("first");
  });
});
