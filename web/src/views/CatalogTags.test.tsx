import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { CatalogTags } from "./CatalogTags";

let container: HTMLDivElement;
let root: Root;
const tags = Array.from({ length: 4500 }, (_, index) => ({ id: `tag-${index}`, name: `分类 ${index}`, count: index + 1 }));
beforeEach(() => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  container = document.createElement("div"); document.body.append(container);
  root = createRoot(container);
});
afterEach(async () => { await act(async () => root.unmount()); container.remove(); vi.unstubAllGlobals(); });

it("bounds a large category directory and loads more tags on demand", async () => {
  await act(async () => root.render(<CatalogTags tags={tags} selected={[]} preview={false} onToggle={() => undefined} />));
  expect(container.querySelectorAll("button[aria-pressed]")).toHaveLength(48);
  await act(async () => [...container.querySelectorAll("button")].find(button => button.textContent?.startsWith("更多标签"))!.click());
  expect(container.querySelectorAll("button[aria-pressed]")).toHaveLength(96);
});

it("searches the entire directory and toggles a result beyond the first page", async () => {
  const toggle = vi.fn();
  await act(async () => root.render(<CatalogTags tags={tags} selected={["tag-4499"]} preview={false} onToggle={toggle} />));
  const input = container.querySelector("input")!;
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, "4499");
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
  const buttons = container.querySelectorAll<HTMLButtonElement>("button[aria-pressed]");
  expect(buttons).toHaveLength(1);
  expect(buttons[0]!.getAttribute("aria-pressed")).toBe("true");
  await act(async () => buttons[0]!.click());
  expect(toggle).toHaveBeenCalledWith("tag-4499");
});

it("shows a small common-tag preview without expanding thousands of tags", async () => {
  await act(async () => root.render(<CatalogTags tags={tags} selected={[]} preview onToggle={() => undefined} />));
  expect(container.querySelectorAll(".catalog-tag-preview button")).toHaveLength(8);
  expect(container.querySelector(".catalog-tag-preview")?.textContent).toContain("分类 4499");
  expect(container.querySelector("details")?.open).toBe(false);
});

it("finds aliases, highlights old selections, and emits a stable ID after a rename", async () => {
  const toggle = vi.fn();
  const renamed = [{ id: "tag:amateur", name: "爱好者", aliases: ["素人", "Amateur"], count: 3 }];
  await act(async () => root.render(<CatalogTags tags={renamed} selected={["ＡＭＡＴＥＵＲ"]} preview={false} onToggle={toggle} />));
  const input = container.querySelector("input")!;
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, " ＡＭＡＴＥＵＲ ");
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
  const button = container.querySelector<HTMLButtonElement>("button[aria-pressed]")!;
  expect(button.textContent).toBe("爱好者 · 3");
  expect(button.getAttribute("aria-pressed")).toBe("true");
  await act(async () => button.click());
  expect(toggle).toHaveBeenCalledWith("tag:amateur");
});

it("treats high-level groups as selectable tree nodes and expands their children", async () => {
  const toggle = vi.fn();
  const grouped = [
    { id: "tag:genres", name: "题材", parent_id: null, count: 2 },
    { id: "tag:drama", name: "剧情", parent_id: "tag:genres", count: 2 },
    { id: "tag:scene", name: "户外", parent_id: null, count: 1 },
  ];
  await act(async () => root.render(<CatalogTags tags={grouped} selected={["tag:scene"]} preview={false} onToggle={toggle} />));
  expect(container.querySelectorAll("button[aria-pressed]")).toHaveLength(2);
  await act(async () => container.querySelector<HTMLButtonElement>('[aria-label="展开题材"]')!.click());
  const buttons = container.querySelectorAll<HTMLButtonElement>("button[aria-pressed]");
  expect(buttons).toHaveLength(3);
  expect(buttons[1]!.textContent).toContain("剧情");
  expect(toggle).not.toHaveBeenCalled();
  await act(async () => buttons[0]!.click());
  expect(toggle).toHaveBeenCalledWith("tag:genres");
});

it("reveals a selected leaf and keeps its ancestors when searching a deep branch", async () => {
  const nested = [
    { id: "clothing", name: "服饰", parent_id: null, count: 4 },
    { id: "stockings", name: "丝袜", parent_id: "clothing", count: 4 },
    { id: "black", name: "黑丝", parent_id: "stockings", aliases: ["Black stockings"], count: 2 },
    { id: "white", name: "白丝", parent_id: "stockings", count: 2 },
  ];
  await act(async () => root.render(<CatalogTags tags={nested} selected={["black"]} preview={false} onToggle={vi.fn()} />));
  expect(container.querySelectorAll("button[aria-pressed]")).toHaveLength(4);
  expect(container.querySelector('button[aria-pressed="true"]')?.textContent).toContain("黑丝");
  const input = container.querySelector("input")!;
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, "Black stockings");
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
  const labels = [...container.querySelectorAll("button[aria-pressed]")].map(button => button.textContent);
  expect(labels).toEqual(["服饰 · 4", "丝袜 · 4", "黑丝 · 2"]);
});
