import { expect, it, vi } from "vitest";
import { EMPTY_CATALOG_FILTERS } from "./catalogFilters";
import { migrateSavedClassifications } from "./classifications";

it("migrates saved names to IDs once while preserving unrelated filter conditions", async () => {
  const create = vi.fn();
  const schema = [{ id: "genres" as const, name: "题材", description: "", values: [
    { id: "genres:drama", name: "剧情", aliases: ["Drama"], count: 0, revision: 1 },
  ] }];
  const original = [{ name: "My filter", filters: { ...EMPTY_CATALOG_FILTERS, q: "keep", actors: ["actor"], genres: ["剧情", "DRAMA"] } }];
  const migrated = await migrateSavedClassifications(original, schema, create);
  expect(migrated[0]!.filters).toMatchObject({ q: "keep", actors: ["actor"], genres: ["genres:drama"] });
  expect(await migrateSavedClassifications(migrated, schema, create)).toEqual(migrated);
  expect(create).not.toHaveBeenCalled();
  expect(original[0]!.filters.genres).toEqual(["剧情", "DRAMA"]);
});

it("creates a formal custom term for an unknown saved value and deduplicates repeated references", async () => {
  const create = vi.fn(async () => ({ id: "genres:custom", name: "Custom", aliases: [], count: 0, revision: 1 }));
  const saved = [{ name: "A", filters: { ...EMPTY_CATALOG_FILTERS, genres: ["Custom", "CUSTOM"] } }];
  const migrated = await migrateSavedClassifications(saved, [], create);
  expect(migrated[0]!.filters.genres).toEqual(["genres:custom"]);
  expect(create).toHaveBeenCalledOnce();
});
