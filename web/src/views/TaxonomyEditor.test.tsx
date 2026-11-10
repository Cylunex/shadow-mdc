import { notifyManager } from "@tanstack/react-query";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { api } from "../api";
import { taxonomySchema } from "../lib/classifications";
import { queryClient } from "../query/client";
import { ClassificationTermEditor } from "./ClassificationTermEditor";
import { TaxonomyEditor } from "./TaxonomyEditor";

vi.mock("../api", () => ({ api: { classifications: vi.fn(), workTaxonomy: vi.fn(), saveWorkTaxonomy: vi.fn(), editClassification: vi.fn(), createClassification: vi.fn() } }));
let root: Root;
let container: HTMLDivElement;
const doc = taxonomySchema.parse({ languages: ["languages:ja"], subtitle_languages: ["subtitle_languages:zh"], regions: ["regions:us"], release_year: 2020 });
beforeEach(() => {
  vi.clearAllMocks(); queryClient.clear(); notifyManager.setScheduler(queueMicrotask);
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  vi.mocked(api.workTaxonomy).mockResolvedValue(doc);
  vi.mocked(api.classifications).mockResolvedValue({ dimensions: [
    { id: "genres", name: "题材", description: "题材", values: [
      { id: "genres:drama", name: "剧情", aliases: [], count: 0, revision: 1 },
      { id: "genres:comedy", name: "喜剧", aliases: [], count: 0, revision: 1 },
    ] },
    ...(["languages", "subtitle_languages", "regions"] as const).map(id => ({
      id, name: id, description: id, values: doc[id].map(value => ({ id: value, name: value, aliases: [], count: 0, revision: 1 })),
    })),
  ] });
  vi.mocked(api.saveWorkTaxonomy).mockResolvedValue(doc);
  container = document.createElement("div"); document.body.append(container); root = createRoot(container);
});
afterEach(async () => { await act(async () => root.unmount()); queryClient.clear(); container.remove(); vi.unstubAllGlobals(); });
async function select(label: string, values: string[]) {
  const element = container.querySelector<HTMLSelectElement>(`select[aria-label="${label}"]`)!;
  await act(async () => {
    for (const option of element.options) option.selected = values.includes(option.value);
    element.dispatchEvent(new Event("change", { bubbles: true }));
  });
}
async function input(label: string, value: string) {
  const element = container.querySelector<HTMLInputElement>(`input[aria-label="${label}"]`)!;
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(element, value);
    element.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

it("edits multiple terms using IDs and keeps audio, subtitle and production region independent", async () => {
  const saved = vi.fn();
  await act(async () => root.render(<TaxonomyEditor work={{ id: "a", title: "A" }} onClose={vi.fn()} onSaved={saved} />));
  await select("编辑题材", ["genres:drama", "genres:comedy"]);
  await act(async () => container.querySelector("form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
  expect(api.saveWorkTaxonomy).toHaveBeenCalledWith("a", expect.objectContaining({
    genres: ["genres:drama", "genres:comedy"], languages: ["languages:ja"], subtitle_languages: ["subtitle_languages:zh"], regions: ["regions:us"],
  }));
  expect(vi.mocked(api.saveWorkTaxonomy).mock.calls[0]![1]).not.toHaveProperty("classification_refs");
  expect(saved).toHaveBeenCalledOnce();
});

it("keeps edited values and reports a failed save", async () => {
  vi.mocked(api.saveWorkTaxonomy).mockRejectedValueOnce(new Error("保存失败"));
  const saved = vi.fn();
  await act(async () => root.render(<TaxonomyEditor work={{ id: "a", title: "A" }} onClose={vi.fn()} onSaved={saved} />));
  await select("编辑题材", ["genres:comedy"]);
  await act(async () => container.querySelector("form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
  expect(container.querySelector('[role="alert"]')?.textContent).toContain("保存失败");
  expect(saved).not.toHaveBeenCalled();
  expect([...container.querySelector<HTMLSelectElement>('[aria-label="编辑题材"]')!.selectedOptions].map(option => option.value)).toEqual(["genres:comedy"]);
});

it("renames a classification without changing its ID or revision guard", async () => {
  const term = { id: "genres:drama", name: "剧情", aliases: [], count: 2, revision: 3 };
  vi.mocked(api.editClassification).mockResolvedValue({ ...term, name: "新名称", revision: 4 });
  await act(async () => root.render(<ClassificationTermEditor dimension="genres" terms={[term]} />));
  await select("管理分类项", [term.id]);
  await input("分类展示名称", "新名称");
  await act(async () => container.querySelector("form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
  expect(api.editClassification).toHaveBeenCalledWith("genres:drama", "新名称", [], 3);
  expect(container.textContent).toContain("已有作品关联继续保留");
});
