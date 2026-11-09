import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { queryClient } from "../query/client";
import { memoryStorage } from "../test/storage";
import { CatalogPeopleEditor, CatalogOrganizations, CatalogPlaylists, type ManagedPerson } from "./CatalogManagement";

vi.mock("../api", () => ({ appUrl: (path: string) => path }));
let container: HTMLDivElement;
let root: Root;
beforeEach(() => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  vi.stubGlobal("localStorage", memoryStorage());
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
});
afterEach(async () => {
  await act(async () => root.unmount());
  queryClient.clear(); container.remove(); vi.restoreAllMocks(); vi.unstubAllGlobals();
});
const person = (id: string): ManagedPerson => ({
  id, ref: "person:" + id, refs: ["person:" + id], name: id,
  aliases: [], biography: null, image_url: null, notes: "",
  groups: [], match_names: [], work_ids: [], revision: 1,
  favorite: false, blacklist: false
});

it("loads organization tools only when opened and cancels a closed read", async () => {
  let signal: AbortSignal | undefined;
  const fetch = vi.fn((_url: string, init: RequestInit) => {
    signal = init.signal ?? undefined;
    return new Promise<Response>(() => undefined);
  });
  vi.stubGlobal("fetch", fetch);
  await act(async () => root.render(<CatalogOrganizations works={[]} onFilter={vi.fn()} />));
  expect(fetch).not.toHaveBeenCalled();
  const details = container.querySelector("details")!;
  await act(async () => { details.open = true; details.dispatchEvent(new Event("toggle")); });
  expect(fetch).toHaveBeenCalledOnce();
  await act(async () => { details.open = false; details.dispatchEvent(new Event("toggle")); });
  expect(signal?.aborted).toBe(true);
});

it("merges only after inline confirmation and sends both revisions", async () => {
  const calls: { url: string; body: unknown }[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init: RequestInit) => {
    if (url.includes("/people-cards?")) return new Response(JSON.stringify({
      items: [{ id: "target", name: "target", work_count: 0, revision: 1 }], next_offset: null,
    }));
    if (init.method === "POST") calls.push({ url, body: JSON.parse(String(init.body)) });
    return new Response(JSON.stringify(init.method === "POST" ? { merge_id: "merge" } : []), { status: 200 });
  }));
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
  const saved = vi.fn();
  await act(async () => root.render(<CatalogPeopleEditor person={person("source")} onSaved={saved} />));
  await act(async () => {
    const details = [...container.querySelectorAll("details")].find(item => item.querySelector("summary")?.textContent === "合并到")!;
    details.open = true;
    details.dispatchEvent(new Event("toggle"));
  });
  await vi.waitFor(() => expect(container.querySelector('option[value="target"]')).not.toBeNull());
  const select = [...container.querySelectorAll<HTMLSelectElement>("select")]
    .find(item => [...item.options].some(option => option.value === "target"))!;
  await act(async () => { select.value = "target"; select.dispatchEvent(new Event("change", { bubbles: true })); });
  const button = [...container.querySelectorAll("button")].find(item => item.textContent === "合并档案")!;
  await act(async () => button.click());
  expect(calls).toHaveLength(0);
  expect(saved).not.toHaveBeenCalled();
  confirm.mockReturnValue(true);
  await act(async () => button.click());
  expect(calls[0]?.body).toEqual({ target_id: "target", source_id: "source", target_revision: 1, source_revision: 1 });
  expect(saved).toHaveBeenCalledOnce();
});

it("saves the user chosen playlist order within a stable preference namespace", async () => {
  const list = { id: "list", name: "Selected", work_ids: ["b", "a"], revision: 1 };
  const puts: { body: unknown; owner: string | undefined }[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init: RequestInit) => {
    if (init.method === "PUT") {
      puts.push({ body: JSON.parse(String(init.body)), owner: (init.headers as Record<string, string>)["X-Catalog-Owner"] });
      return new Response(String(init.body), { status: 200 });
    }
    return new Response(JSON.stringify(url.endsWith("/works")
      ? [{ id: "b", title: "B" }, { id: "a", title: "A" }] : [list]), { status: 200 });
  }));
  await act(async () => root.render(<CatalogPlaylists works={[{ id: "a", title: "A" }, { id: "b", title: "B" }]} onOpenWork={() => undefined} />));
  await act(async () => {
    const details = container.querySelector("details")!;
    details.open = true;
    details.dispatchEvent(new Event("toggle"));
  });
  const select = container.querySelector<HTMLSelectElement>("select")!;
  await act(async () => { select.value = "list"; select.dispatchEvent(new Event("change", { bubbles: true })); });
  const up = container.querySelectorAll<HTMLButtonElement>('button[aria-label="上移"]')[1]!;
  await act(async () => up.click());
  const save = [...container.querySelectorAll("button")].find(button => button.textContent === "保存片单及顺序")!;
  await act(async () => save.click());
  expect(puts[0]?.body).toMatchObject({ work_ids: ["a", "b"], revision: 1 });
  expect(puts[0]?.owner).toBeTruthy();
});
