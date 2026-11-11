import { notifyManager } from "@tanstack/react-query";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { api } from "../api";
import { taxonomySchema } from "../lib/classifications";
import { queryClient } from "../query/client";
import { ClassificationTermEditor } from "./ClassificationTermEditor";
import { ClassificationManagement } from "./ClassificationManagement";
import { TaxonomyEditor } from "./TaxonomyEditor";

vi.mock("../api", () => ({ api: { classifications: vi.fn(), workTaxonomy: vi.fn(), saveWorkTaxonomy: vi.fn(), editClassification: vi.fn(), createClassification: vi.fn() } }));
let root: Root;
let container: HTMLDivElement;
const doc = taxonomySchema.parse({ languages: ["languages:ja"], regions: ["regions:us"], release_year: 2020 });
beforeEach(() => {
  vi.clearAllMocks(); queryClient.clear(); notifyManager.setScheduler(queueMicrotask);
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  vi.mocked(api.workTaxonomy).mockResolvedValue(doc);
  vi.mocked(api.classifications).mockResolvedValue({ dimensions: [
    { id: "forms", name: "形式", description: "形式", values: [
      { id: "forms:drama", name: "剧情", aliases: [], count: 0, revision: 1 },
      { id: "forms:comedy", name: "喜剧", aliases: [], count: 0, revision: 1 },
    ] },
    ...(["languages", "regions"] as const).map(id => ({
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

it("edits retained dimensions and excludes subtitle and genre fields", async () => {
  const saved = vi.fn();
  await act(async () => root.render(<TaxonomyEditor work={{ id: "a", title: "A" }} onClose={vi.fn()} onSaved={saved} />));
  await select("编辑表现形式", ["forms:drama", "forms:comedy"]);
  await act(async () => container.querySelector("form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
  expect(api.saveWorkTaxonomy).toHaveBeenCalledWith("a", expect.objectContaining({
    forms: ["forms:drama", "forms:comedy"], languages: ["languages:ja"], regions: ["regions:us"],
  }));
  expect(vi.mocked(api.saveWorkTaxonomy).mock.calls[0]![1]).not.toHaveProperty("classification_refs");
  expect(saved).toHaveBeenCalledOnce();
});

it("keeps edited values and reports a failed save", async () => {
  vi.mocked(api.saveWorkTaxonomy).mockRejectedValueOnce(new Error("保存失败"));
  const saved = vi.fn();
  await act(async () => root.render(<TaxonomyEditor work={{ id: "a", title: "A" }} onClose={vi.fn()} onSaved={saved} />));
  await select("编辑表现形式", ["forms:comedy"]);
  await act(async () => container.querySelector("form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
  expect(container.querySelector('[role="alert"]')?.textContent).toContain("保存失败");
  expect(saved).not.toHaveBeenCalled();
  expect([...container.querySelector<HTMLSelectElement>('[aria-label="编辑表现形式"]')!.selectedOptions].map(option => option.value)).toEqual(["forms:comedy"]);
});

it("renames a classification without changing its ID or revision guard", async () => {
  const term = { id: "forms:drama", name: "剧情", aliases: [], count: 2, revision: 3 };
  vi.mocked(api.editClassification).mockResolvedValue({ ...term, name: "新名称", revision: 4 });
  await act(async () => root.render(<ClassificationTermEditor dimension="forms" terms={[term]} />));
  await select("管理分类项", [term.id]);
  await input("分类展示名称", "新名称");
  await act(async () => container.querySelector("form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
  expect(api.editClassification).toHaveBeenCalledWith("forms:drama", "新名称", [], 3);
  expect(container.textContent).toContain("已有作品关联继续保留");
});

it("keeps dimension dictionaries available in works tools and loads them only when opened", async () => {
  await act(async () => root.render(<ClassificationManagement />));
  expect(api.classifications).not.toHaveBeenCalled();
  const details = container.querySelector("details")!;
  await act(async () => {
    details.open = true;
    details.dispatchEvent(new Event("toggle"));
  });
  expect(api.classifications).toHaveBeenCalledOnce();
  await select("分类字典维度", ["forms"]);
  expect(container.querySelector('[aria-label="管理分类项"]')?.textContent).toContain("剧情");
  expect(container.querySelector('[aria-label="管理分类项"]')?.textContent).toContain("喜剧");
});

it("keeps a newly created classification selectable for subsequent edits", async () => {
  const created = { id: "forms:custom-fixture", name: "新形式", aliases: [], count: 0, revision: 1 };
  vi.mocked(api.createClassification).mockResolvedValue(created);
  vi.mocked(api.editClassification).mockResolvedValue({ ...created, name: "再次改名", revision: 2 });
  await act(async () => root.render(<ClassificationTermEditor dimension="forms" terms={[]} />));
  await input("分类展示名称", "新形式");
  await act(async () => container.querySelector("form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
  expect(container.querySelector<HTMLSelectElement>("select")?.value).toBe(created.id);
  await input("分类展示名称", "再次改名");
  await act(async () => container.querySelector("form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
  expect(api.editClassification).toHaveBeenCalledWith(created.id, "再次改名", [], 1);
});
