import { notifyManager } from "@tanstack/react-query";
import { act, StrictMode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { api } from "../api";
import { libraryPrefsSchema } from "../model";
import { queryClient } from "../query/client";
import { memoryStorage } from "../test/storage";
import { EMPTY_CATALOG_FILTERS, readCatalogFilters } from "../lib/catalogFilters";
import { CategoriesView } from "./CategoriesView";
import { UnifiedCatalogView } from "./UnifiedCatalogView";

vi.mock("../api", () => ({ appUrl: (path: string | null) => path, api: {
  categories: vi.fn(), classifications: vi.fn(), createClassification: vi.fn(), libraryPrefs: vi.fn(),
} }));
let root: Root;
let container: HTMLDivElement;
const categories = Array.from({ length: 60 }, (_, index) => ({
  slug: `tag-${index}`, label: `分类 ${index}`, aliases: [`alias-${index}`], image_url: null, work_count: index,
}));
const renderActors = () => root.render(<UnifiedCatalogView mode="actors" onOpenWork={vi.fn()} onOpenActor={vi.fn()} />);
const button = (label: string) => [...container.querySelectorAll<HTMLButtonElement>("button")]
  .find(item => item.textContent === label || item.getAttribute("aria-label") === label)!;
async function change(label: string, value: string) {
  const element = container.querySelector<HTMLInputElement | HTMLSelectElement>(`[aria-label="${label}"]`)!;
  await act(async () => {
    if (element instanceof HTMLInputElement) {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(element, value);
      element.dispatchEvent(new Event("input", { bubbles: true }));
    } else {
      element.value = value;
      element.dispatchEvent(new Event("change", { bubbles: true }));
    }
  });
}
beforeEach(() => {
  vi.clearAllMocks(); queryClient.clear(); notifyManager.setScheduler(queueMicrotask);
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  vi.stubGlobal("sessionStorage", memoryStorage()); vi.stubGlobal("localStorage", memoryStorage());
  sessionStorage.setItem("mdc:category-browsing", JSON.stringify({ dimension: "tags", query: "", onlyWithWorks: false, sort: "featured", page: 1, pageSize: 24 }));
  vi.mocked(api.classifications).mockResolvedValue({ dimensions: [] });
  vi.mocked(api.categories).mockResolvedValue({ categories });
  vi.mocked(api.libraryPrefs).mockResolvedValue(libraryPrefsSchema.parse({}));
  vi.spyOn(window, "scrollTo").mockImplementation(() => undefined);
  window.history.replaceState({}, "", "/");
  container = document.createElement("div"); document.body.append(container); root = createRoot(container);
});
afterEach(async () => {
  await act(async () => root.unmount()); queryClient.clear(); container.remove();
  vi.restoreAllMocks(); vi.unstubAllGlobals();
});

it("bounds category cards, remembers the page, and resets it when changing page size", async () => {
  await act(async () => root.render(<CategoriesView onOpenTag={vi.fn()} />));
  expect(container.querySelectorAll(".category-card")).toHaveLength(24);
  await act(async () => button("下一页分类").click());
  expect(container.querySelector(".category-card strong")?.textContent).toBe("分类 24");
  await act(async () => root.render(null));
  await act(async () => root.render(<CategoriesView onOpenTag={vi.fn()} />));
  expect(container.querySelector(".category-card strong")?.textContent).toBe("分类 24");
  await change("分类每页数量", "96");
  expect(container.querySelectorAll(".category-card")).toHaveLength(60);
  expect(container.querySelector('[aria-current="page"]')?.textContent).toBe("1");
  expect(api.categories).toHaveBeenCalledTimes(1);
});

it("searches category aliases, sorts counts, and clears an empty filter locally", async () => {
  await act(async () => root.render(<CategoriesView onOpenTag={vi.fn()} />));
  await change("分类排序", "count");
  expect(container.querySelector(".category-card strong")?.textContent).toBe("分类 59");
  await change("搜索分类", "alias-0");
  expect(container.querySelectorAll(".category-card")).toHaveLength(1);
  await act(async () => container.querySelector<HTMLInputElement>('input[type="checkbox"]')!.click());
  expect(container.textContent).toContain("没有匹配的分类");
  expect(container.textContent).not.toContain("scripts/");
  await act(async () => button("清除筛选").click());
  expect(container.querySelectorAll(".category-card")).toHaveLength(24);
  expect(api.categories).toHaveBeenCalledTimes(1);
});

it("retries a failed category load without claiming the catalog is empty", async () => {
  vi.mocked(api.categories).mockRejectedValueOnce(new Error("temporarily unavailable"));
  await act(async () => root.render(<CategoriesView onOpenTag={vi.fn()} />));
  expect(container.querySelector('[role="alert"]')?.textContent).toContain("分类加载失败");
  expect(container.textContent).not.toContain("暂无分类");
  await act(async () => button("重试分类").click());
  expect(container.querySelectorAll(".category-card")).toHaveLength(24);
});

it("opens a category by its stable ID while displaying its name", async () => {
  const open = vi.fn();
  vi.mocked(api.categories).mockResolvedValue({ categories: [
    { ...categories[0]!, id: "tag:amateur", label: "爱好者", aliases: ["素人", "Amateur"] },
  ] });
  await act(async () => root.render(<CategoriesView onOpenTag={open} />));
  await act(async () => container.querySelector<HTMLButtonElement>(".category-card")!.click());
  expect(open).toHaveBeenCalledWith("tag:amateur", "爱好者");
});

it("keeps the category page as a tag gallery with covers and stable tag navigation", async () => {
  sessionStorage.setItem("mdc:category-browsing", JSON.stringify({ dimension: "contents", query: "", onlyWithWorks: false, sort: "featured", page: 1, pageSize: 24 }));
  const open = vi.fn();
  vi.mocked(api.categories).mockResolvedValue({ categories: [
    { ...categories[0]!, id: "tag:amateur", label: "素人", image_url: "/api/category-covers/amateur.jpg" },
  ] });
  await act(async () => root.render(<CategoriesView onOpenTag={open} />));
  expect(api.categories).toHaveBeenCalledOnce();
  expect(api.classifications).not.toHaveBeenCalled();
  expect(container.querySelector('[aria-label="分类维度"]')).toBeNull();
  expect(container.querySelector(".category-card strong")?.textContent).toBe("素人");
  expect(container.querySelector(".category-card img")?.getAttribute("src")).toBe("/api/category-covers/amateur.jpg");
  await act(async () => container.querySelector<HTMLButtonElement>(".category-card")!.click());
  expect(open).toHaveBeenCalledWith("tag:amateur", "素人");
});

it("migrates local saved classifications once under StrictMode", async () => {
  localStorage.setItem("mdc:saved-filters", JSON.stringify([{ name: "Old", filters: {
    ...EMPTY_CATALOG_FILTERS, q: "keep", forms: ["自定义旧题材"],
  } }]));
  vi.mocked(api.createClassification).mockResolvedValue({ id: "forms:custom", name: "自定义旧题材", aliases: [], count: 0, revision: 1 });
  vi.stubGlobal("fetch", vi.fn(async (url: string) => new Response(JSON.stringify(
    url.endsWith("/facets") ? {} : { items: [], next_cursor: null, total: 0 },
  ))));
  await act(async () => root.render(<StrictMode><UnifiedCatalogView mode="works" onOpenWork={vi.fn()} onOpenActor={vi.fn()} /></StrictMode>));
  expect(api.createClassification).toHaveBeenCalledOnce();
  expect(JSON.parse(localStorage.getItem("mdc:saved-filters")!)[0].filters).toMatchObject({ q: "keep", forms: ["forms:custom"] });
  expect(localStorage.getItem("mdc:saved-classification-version")).toBe("1");
});

it("keeps a legacy tag selection readable and removes it when toggling the new ID", async () => {
  window.history.replaceState({}, "", "/?tag=Amateur");
  const fetch = vi.fn(async (url: string, init?: RequestInit) => new Response(JSON.stringify(
    url.endsWith("/facets") ? { tags: [{ id: "tag:amateur", name: "爱好者", aliases: ["素人", "Amateur"], count: 1 }] }
    : { items: [], next_cursor: null, total: 0, filters: JSON.parse(String(init?.body)) },
  )));
  vi.stubGlobal("fetch", fetch);
  await act(async () => root.render(<UnifiedCatalogView mode="works" onOpenWork={vi.fn()} onOpenActor={vi.fn()} />));
  expect(container.querySelector(".active-filter-chips")?.textContent).toContain("爱好者 ×");
  const tag = container.querySelector<HTMLButtonElement>('[aria-pressed="true"]')!;
  await act(async () => tag.click());
  expect(new URLSearchParams(window.location.search).has("tag")).toBe(false);
  await act(async () => container.querySelector<HTMLButtonElement>('button[aria-pressed]')!.click());
  expect(new URLSearchParams(window.location.search).get("tag")).toBe("tag:amateur");
  expect(container.querySelector(".active-filter-chips")?.textContent).toContain("爱好者 ×");
});

it("filters runtime and keeps work types under more filters", async () => {
  const fetch = vi.fn(async (url: string) => new Response(JSON.stringify(
    url.endsWith("/facets") ? { durations: [{ id: "30to60", name: "30-60 分钟", count: 2 }] }
      : { items: [], next_cursor: null, total: 0 },
  )));
  vi.stubGlobal("fetch", fetch);
  await act(async () => root.render(<UnifiedCatalogView mode="works" onOpenWork={vi.fn()} onOpenActor={vi.fn()} />));
  await act(async () => button("分类与筛选").click());
  expect(container.querySelector('[aria-label="筛选字幕语言"]')).toBeNull();
  expect(container.querySelector('[aria-label="筛选音轨语言"]')).toBeNull();
  expect(container.querySelector('[aria-label="筛选题材"]')).toBeNull();
  expect(container.querySelector('[aria-label="筛选语言"]')).not.toBeNull();
  expect(container.querySelector('[aria-label="筛选作品类型"]')).toBeNull();
  await change("筛选时长", "30to60");
  expect(readCatalogFilters(window.location.search)).toMatchObject({ duration_min: 1800, duration_max: 3600 });
  await act(async () => button("更多筛选").click());
  expect(container.querySelector('[aria-label="筛选作品类型"]')).not.toBeNull();
  const requests = fetch.mock.calls.filter(([url]) => url.endsWith("/works/query"));
  expect(requests.length).toBeGreaterThan(0);
});

it("combines production, brand, channel and series without treating series as an organization", async () => {
  vi.stubGlobal("fetch", vi.fn(async (url: string) => new Response(JSON.stringify(
    url.endsWith("/facets") ? {
      organizations: [{ id: "maker", name: "Company", role: "organization", count: 3 }],
      labels: [{ id: "brand", name: "Brand", count: 2 }],
      platforms: [{ id: "channel", name: "Channel", count: 1 }],
      series: [{ id: "cycle", name: "Cycle", count: 2 }],
    } : { items: [], next_cursor: null, total: 0 },
  ))));
  await act(async () => root.render(<UnifiedCatalogView mode="works" onOpenWork={vi.fn()} onOpenActor={vi.fn()} />));
  await act(async () => button("分类与筛选").click());
  expect(container.querySelector('[aria-label="合集"]')).toBeNull();
  expect(container.querySelector('[aria-label="机构和系列"]')).toBeNull();
  await change("筛选制作机构", "maker");
  await change("筛选厂牌", "brand");
  await change("筛选发布平台", "channel");
  await change("筛选系列", "cycle");
  expect(readCatalogFilters(window.location.search)).toMatchObject({
    organizations: ["maker"], labels: ["brand"], platforms: ["channel"], series: ["cycle"],
  });
  expect(container.querySelector(".active-filter-chips")?.textContent).toContain("系列：Cycle");
});

it("jumps actor pages, resets offsets on sort and search, and requests only the chosen page size", async () => {
  const fetch = vi.fn(async (url: string) => {
    const params = new URL(url, "https://example.com").searchParams;
    return new Response(JSON.stringify({ items: [{ id: "actor", name: "Actor", aliases: [], work_count: 2 }],
      total: 1000, next_offset: Number(params.get("offset")) + Number(params.get("limit")), labels: {} }));
  });
  vi.stubGlobal("fetch", fetch);
  await act(async () => renderActors());
  await change("演员跳转页码（顶部）", "8");
  await act(async () => button("跳转").click());
  expect(fetch.mock.calls.at(-1)?.[0]).toContain("offset=336");
  await change("演员排序", "name");
  expect(fetch.mock.calls.at(-1)?.[0]).toContain("offset=0");
  await change("演员每页数量", "24");
  expect(fetch.mock.calls.at(-1)?.[0]).toContain("limit=24");
  await change("搜索演员与别名", "Alias");
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 210)); });
  expect(fetch.mock.calls.at(-1)?.[0]).toContain("q=Alias");
  expect(fetch.mock.calls.at(-1)?.[0]).toContain("offset=0");
});

it("recovers an actor page that disappeared after the list shrank", async () => {
  sessionStorage.setItem("mdc:actor-browsing", JSON.stringify({ search: "", sort: "works", followed: false, page: 9, pageSize: 48 }));
  const fetch = vi.fn(async (url: string) => new Response(JSON.stringify({
    items: url.includes("offset=0") ? [{ id: "actor", name: "Remaining", aliases: [], work_count: 0 }] : [],
    total: 1, next_offset: null, labels: {},
  })));
  vi.stubGlobal("fetch", fetch);
  await act(async () => renderActors());
  expect(fetch).toHaveBeenCalledTimes(2);
  expect(fetch.mock.calls[0]?.[0]).toContain("offset=384");
  expect(fetch.mock.calls[1]?.[0]).toContain("offset=0");
  expect(container.querySelector(".actor-grid")?.textContent).toContain("Remaining");
});

it("retains works on a failed next page, retries the same cursor, and removes duplicate cards", async () => {
  let attempt = 0;
  const fetch = vi.fn(async (url: string, init?: RequestInit) => {
    if (url.endsWith("/facets")) return new Response("{}");
    const body = JSON.parse(String(init?.body));
    if (body.cursor && ++attempt === 1) return new Response(null, { status: 502 });
    const card = (id: string) => ({ id, title: id, actors: [], tags: [], poster: "" });
    return new Response(JSON.stringify(body.cursor
      ? { items: [card("First"), card("Second")], next_cursor: null, total: 2 }
      : { items: [card("First")], next_cursor: "next", total: 2 }));
  });
  vi.stubGlobal("fetch", fetch);
  await act(async () => root.render(<UnifiedCatalogView mode="works" onOpenWork={vi.fn()} onOpenActor={vi.fn()} />));
  await act(async () => button("加载下一页").click());
  expect(container.querySelectorAll(".work-grid .work")).toHaveLength(1);
  await act(async () => button("重试作品").click());
  expect(attempt).toBe(2);
  expect(container.querySelectorAll(".work-grid .work")).toHaveLength(2);
  expect(container.textContent).toContain("已显示全部作品");
});
