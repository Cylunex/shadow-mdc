import { act } from "react";
import { createRoot } from "react-dom/client";
import { expect, it, vi } from "vitest";
import { api } from "../api";
import { memoryStorage } from "../test/storage";
import { DiscoverPanel } from "./DiscoverPanel";

vi.mock("../api", () => ({ api: { javrankingSections: vi.fn(), javrankingList: vi.fn() } }));

it("opens the latest populated year and restores the chosen year without loading another list", async () => {
  const sections = [
    { slug: "awards", kind: "curated-videos", year: null, item_count: 1 },
    { slug: "new", kind: "top250-year", year: 2026, item_count: 0 },
    { slug: "last", kind: "top250-year", year: 2025, item_count: 1 },
    { slug: "old", kind: "top250-year", year: 2024, item_count: 1 },
  ].map(row => ({ ...row, title: row.slug, id: row.slug }));
  vi.mocked(api.javrankingSections).mockResolvedValue({ sections });
  vi.mocked(api.javrankingList).mockImplementation(async slug => ({
    section: sections.find(row => row.slug === slug)!, items: [{ title: slug, position: 1 }],
  }));
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  vi.stubGlobal("sessionStorage", memoryStorage());
  const el = document.createElement("div");
  const root = createRoot(el);
  const render = () => root.render(<DiscoverPanel busy={null} report={vi.fn()} onSeeded={vi.fn()} />);
  try {
    await act(async () => render());
    await act(async () => [...el.querySelectorAll("button")].find(node => node.textContent === "年榜 TOP250")!.click());
    expect(el.querySelector(".dense-rank-card h2")?.textContent).toBe("last");
    await act(async () => el.querySelector<HTMLButtonElement>('[title="old"]')!.click());
    await act(async () => root.render(null));
    vi.mocked(api.javrankingList).mockClear();
    await act(async () => render());
    expect(el.querySelector(".dense-rank-card h2")?.textContent).toBe("old");
    expect(vi.mocked(api.javrankingList).mock.calls.map(([slug]) => slug)).toEqual(["old"]);
  } finally {
    await act(async () => root.unmount());
    vi.unstubAllGlobals();
  }
});
