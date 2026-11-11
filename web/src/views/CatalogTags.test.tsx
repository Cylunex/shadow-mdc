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

it("browses groups without changing selection or tag identities", async () => {
  const toggle = vi.fn();
  const grouped = [
    { id: "tag:drama", name: "剧情", group: "题材", count: 2 },
    { id: "tag:scene", name: "户外", group: "场景", count: 1 },
  ];
  await act(async () => root.render(<CatalogTags tags={grouped} selected={["tag:scene"]} preview={false} onToggle={toggle} />));
  const select = container.querySelector<HTMLSelectElement>('[aria-label="浏览标签分组"]')!;
  await act(async () => { select.value = "题材"; select.dispatchEvent(new Event("change", { bubbles: true })); });
  const buttons = container.querySelectorAll<HTMLButtonElement>("button[aria-pressed]");
  expect(buttons).toHaveLength(1);
  expect(buttons[0]!.textContent).toContain("剧情");
  expect(toggle).not.toHaveBeenCalled();
});
