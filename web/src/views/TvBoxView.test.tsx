import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { expect, it, vi } from "vitest";
import { TvBoxView } from "./TvBoxView";
import { memoryStorage } from "../test/storage";

vi.mock("../api", () => ({ appUrl: (path: string) => path }));

it("shows a tested site in the directory after one import", async () => {
  const cache = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  let available = false;
  const site = { id: "demo", name: "示例站点", enabled: true, active_revision: "revision", failure: null };
  const fetch = vi.fn(async (path: string, init?: RequestInit) => {
    if (path === "/api/tvbox/imports" && init?.method === "POST") {
      return Response.json({ id: "test", result: null }, { status: 202 });
    }
    if (path === "/api/tvbox/imports/test") {
      available = true;
      return Response.json({ id: "test", result: { sites: [site], warnings: [], skipped: 0 } });
    }
    if (path === "/api/tvbox/groups") return Response.json({ items: [] });
    if (path === "/api/tvbox/sites") return Response.json({ items: available ? [site] : [] });
    if (path.endsWith("/categories")) return Response.json({ items: [] });
    if (path.endsWith("/items?page=1")) {
      return Response.json({ items: [{ site_id: "demo", id: "item", title: "示例影片" }], failures: [] });
    }
    throw new Error("Unexpected endpoint");
  });
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  vi.stubGlobal("fetch", fetch);
  vi.stubGlobal("localStorage", memoryStorage());
  try {
    await act(async () => root.render(<QueryClientProvider client={cache}><TvBoxView /></QueryClientProvider>));
    for (let tick = 0; tick < 20 && !container.querySelector('input[type="url"]'); tick++) {
      await act(async () => new Promise<void>((resolve) => setTimeout(resolve, 8)));
    }
    await act(async () => {
      const input = container.querySelector<HTMLInputElement>('input[type="url"]')!;
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, "https://example.com/cms");
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await act(async () => container.querySelector("form")!.dispatchEvent(new Event("submit", {
      bubbles: true, cancelable: true,
    })));
    for (let attempt = 0; attempt < 25 && !container.textContent?.includes("示例影片"); attempt++) {
      await act(async () => new Promise<void>((resolve) => setTimeout(resolve, 8)));
    }
    expect(container.textContent).toContain("示例影片");
    expect(container.textContent).toContain("1 个站点通过测试");
    expect(fetch.mock.calls.filter(([, options]) => options?.method === "POST")).toHaveLength(1);
  } finally {
    await act(async () => root.unmount());
    cache.clear();
    container.remove();
    vi.unstubAllGlobals();
  }
});
