import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { memoryStorage } from "../test/storage";

const getRandomValues = globalThis.crypto.getRandomValues.bind(globalThis.crypto);
const uuidPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

beforeEach(() => {
  vi.resetModules();
  vi.stubGlobal("localStorage", memoryStorage());
  vi.stubGlobal("crypto", { getRandomValues });
});
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });

it("creates and persists a UUID on LAN HTTP without randomUUID", async () => {
  const { catalogOwner } = await import("./CatalogManagement");
  const owner = catalogOwner();
  expect(owner).toMatch(uuidPattern);
  expect(localStorage.getItem("mdc:catalog-owner")).toBe(owner);
  vi.resetModules();
  const reloaded = await import("./CatalogManagement");
  expect(reloaded.catalogOwner()).toBe(owner);
});

it("preserves the existing preference namespace without generating another ID", async () => {
  localStorage.setItem("mdc:catalog-owner", "existing-owner");
  vi.stubGlobal("crypto", {});
  const { catalogOwner } = await import("./CatalogManagement");
  expect(catalogOwner()).toBe("existing-owner");
});

it.each(["getItem", "setItem"] as const)("keeps a stable HTTP owner when storage %s fails", async method => {
  vi.spyOn(localStorage, method).mockImplementation(() => { throw new Error("Storage blocked"); });
  const { catalogOwner } = await import("./CatalogManagement");
  const owner = catalogOwner();
  expect(owner).toMatch(uuidPattern);
  expect(catalogOwner()).toBe(owner);
});

it("uses the native UUID API when available", async () => {
  const randomUUID = vi.fn(() => "native-owner");
  vi.stubGlobal("crypto", { randomUUID });
  const { catalogOwner } = await import("./CatalogManagement");
  expect(catalogOwner()).toBe("native-owner");
  expect(catalogOwner()).toBe("native-owner");
  expect(randomUUID).toHaveBeenCalledOnce();
});
