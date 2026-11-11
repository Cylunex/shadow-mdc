import { expect, it, vi } from "vitest";
import { EMPTY_CATALOG_FILTERS } from "./catalogFilters";
import { migrateSavedClassifications } from "./classifications";

it("migrates saved names to IDs once while preserving unrelated filter conditions", async () => {
  const create = vi.fn();
  const schema = [{ id: "forms" as const, name: "形式", description: "", values: [
    { id: "forms:drama", name: "剧情", aliases: ["Drama"], count: 0, revision: 1 },
  ] }];
  const original = [{ name: "My filter", filters: { ...EMPTY_CATALOG_FILTERS, q: "keep", actors: ["actor"], forms: ["剧情", "DRAMA"] } }];
  const migrated = await migrateSavedClassifications(original, schema, create);
  expect(migrated[0]!.filters).toMatchObject({ q: "keep", actors: ["actor"], forms: ["forms:drama"] });
  expect(await migrateSavedClassifications(migrated, schema, create)).toEqual(migrated);
  expect(create).not.toHaveBeenCalled();
  expect(original[0]!.filters.forms).toEqual(["剧情", "DRAMA"]);
});

it("creates a formal custom term for an unknown saved value and deduplicates repeated references", async () => {
  const create = vi.fn(async () => ({ id: "forms:custom", name: "Custom", aliases: [], count: 0, revision: 1 }));
  const saved = [{ name: "A", filters: { ...EMPTY_CATALOG_FILTERS, forms: ["Custom", "CUSTOM"] } }];
  const migrated = await migrateSavedClassifications(saved, [], create);
  expect(migrated[0]!.filters.forms).toEqual(["forms:custom"]);
  expect(create).toHaveBeenCalledOnce();
});

it("keeps retired conditions without creating new classifications or changing tag Boolean logic", async () => {
  const create = vi.fn();
  const term = (id: string, name: string) => ({ id, name, aliases: [], count: 0, revision: 1 });
  const saved = [{ name: "Old", filters: { ...EMPTY_CATALOG_FILTERS, tags: ["tag:keep"], tags_all: true,
    genres: ["剧情"], subtitle_languages: ["中文"], forms: ["短片"] } }];
  const [migrated] = await migrateSavedClassifications(saved, [], create, {
    genres: [term("genres:drama", "剧情")], subtitle_languages: [term("subtitle_languages:zh", "中文")],
    forms: [term("forms:short", "短片")],
  });
  expect(migrated!.filters).toMatchObject({ tags: ["tag:keep"], tags_all: true, genres: ["genres:drama"],
    subtitle_languages: ["subtitle_languages:zh"], forms: ["forms:short"] });
  expect(create).not.toHaveBeenCalled();
});
