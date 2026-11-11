import { notifyManager } from "@tanstack/react-query";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { api } from "../api";
import { queryClient } from "../query/client";
import { TagManagement } from "./TagManagement";

vi.mock("../api", () => ({ api: { tagDictionary: vi.fn(), createTag: vi.fn(), editTag: vi.fn() } }));
let root: Root;
let container: HTMLDivElement;
const existing = { id: "tag:fixture", name: "原名称", aliases: ["Original"], hidden: false, group: "", revision: 3, count: 2 };
beforeEach(() => {
  vi.clearAllMocks(); queryClient.clear(); notifyManager.setScheduler(queueMicrotask);
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  vi.mocked(api.tagDictionary).mockResolvedValue({ terms: [existing], total: 1, revision: 1 });
  container = document.createElement("div"); document.body.append(container); root = createRoot(container);
});
afterEach(async () => { await act(async () => root.unmount()); container.remove(); queryClient.clear(); vi.unstubAllGlobals(); });
async function open() {
  await act(async () => root.render(<TagManagement />));
  expect(api.tagDictionary).not.toHaveBeenCalled();
  const details = container.querySelector("details")!;
  await act(async () => { details.open = true; details.dispatchEvent(new Event("toggle")); });
}
async function input(label: string, value: string) {
  const element = container.querySelector<HTMLInputElement>(`input[aria-label="${label}"]`)!;
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(element, value);
    element.dispatchEvent(new Event("input", { bubbles: true }));
  });
}
async function submit() {
  await act(async () => container.querySelector("form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
}
it("creates a data-backed word and retains its identity for a second edit", async () => {
  const created = { ...existing, id: "raw:new", name: "新词条", revision: 1, count: 0, aliases: [] };
  vi.mocked(api.createTag).mockResolvedValue(created);
  vi.mocked(api.editTag).mockResolvedValue({ ...created, name: "改名词条", revision: 2 });
  await open();
  await input("标签展示名称", "新词条"); await submit();
  expect(api.createTag).toHaveBeenCalledWith({ name: "新词条", aliases: [], group: "", hidden: false });
  expect(container.querySelector<HTMLSelectElement>("select")?.value).toBe(created.id);
  await input("标签展示名称", "改名词条"); await submit();
  expect(api.editTag).toHaveBeenCalledWith(created.id, { name: "改名词条", aliases: [], group: "", hidden: false, revision: 1 });
  expect(container.textContent).toContain("已有作品和片单关联继续保留");
});
it("renames and hides a selected term using its current revision", async () => {
  vi.mocked(api.editTag).mockResolvedValue({ ...existing, name: "新展示", hidden: true, revision: 4 });
  await open();
  const select = container.querySelector<HTMLSelectElement>("select")!;
  await act(async () => { select.value = existing.id; select.dispatchEvent(new Event("change", { bubbles: true })); });
  await input("标签展示名称", "新展示");
  await act(async () => container.querySelector<HTMLInputElement>('[aria-label="隐藏标签"]')!.click());
  await submit();
  expect(api.editTag).toHaveBeenCalledWith(existing.id, { name: "新展示", aliases: ["Original"], group: "", hidden: true, revision: 3 });
  expect(container.querySelector("select")?.textContent).toContain("已隐藏");
});
it("keeps input on a rejected write and shows the actual error", async () => {
  vi.mocked(api.createTag).mockRejectedValue(new Error("标签别名已存在"));
  await open(); await input("标签展示名称", "用户输入"); await submit();
  expect(container.querySelector('[role="alert"]')?.textContent).toContain("标签别名已存在");
  expect(container.querySelector<HTMLInputElement>('[aria-label="标签展示名称"]')?.value).toBe("用户输入");
});
