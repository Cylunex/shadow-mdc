import { z } from "zod";
import { filtersSchema, type Filters } from "./catalogFilters";

export const CLASSIFICATION_KEYS = ["contents", "types", "genres", "forms", "languages", "subtitle_languages", "regions"] as const;
export type ClassificationKey = typeof CLASSIFICATION_KEYS[number];
export const CLASSIFICATION_NAMES: Record<ClassificationKey, string> = {
  contents: "内容体系", types: "作品类型", genres: "题材", forms: "表现形式",
  languages: "音轨语言", subtitle_languages: "字幕语言", regions: "制作地区",
};
export const classificationTermSchema = z.object({
  id: z.string(), name: z.string(), aliases: z.array(z.string()).default([]),
  count: z.number().default(0), revision: z.number().default(1),
});
export type ClassificationTerm = z.infer<typeof classificationTermSchema>;
export const classificationsSchema = z.object({ dimensions: z.array(z.object({
  id: z.enum([...CLASSIFICATION_KEYS, "decades"]), name: z.string(), description: z.string(),
  values: z.array(classificationTermSchema),
})) });
export const taxonomySchema = z.object({
  contents: z.array(z.string()).default([]), types: z.array(z.string()).default([]),
  genres: z.array(z.string()).default([]), forms: z.array(z.string()).default([]),
  languages: z.array(z.string()).default([]), subtitle_languages: z.array(z.string()).default([]),
  regions: z.array(z.string()).default([]), release_year: z.number().nullable().default(null),
  classification_refs: z.record(z.string(), z.array(z.object({ id: z.string(), name: z.string() }))).default({}),
});
export type Taxonomy = z.infer<typeof taxonomySchema>;
export const taxonomyEditSchema = taxonomySchema.omit({ classification_refs: true });
export type TaxonomyEdit = Pick<Taxonomy, ClassificationKey | "release_year">;

export async function migrateSavedClassifications(
  saved: { name: string; filters: Filters }[],
  dimensions: z.infer<typeof classificationsSchema>["dimensions"],
  create: (dimension: ClassificationKey, name: string) => Promise<ClassificationTerm>,
): Promise<{ name: string; filters: Filters }[]> {
  const fold = (text: string) => text.normalize("NFKC").trim().toLowerCase();
  const indexes = new Map(dimensions.map(dimension => [dimension.id, [...dimension.values]]));
  const result: { name: string; filters: Filters }[] = [];
  for (const item of saved) {
    const filters = filtersSchema.parse(item.filters);
    for (const key of CLASSIFICATION_KEYS) {
      const values: string[] = [];
      const terms = indexes.get(key) ?? [];
      for (const raw of filters[key]) {
        let term = terms.find(term => term.id === raw || [term.name, ...term.aliases].some(name => fold(name) === fold(raw)));
        if (!term) { term = await create(key, raw); terms.push(term); indexes.set(key, terms); }
        values.push(term.id);
      }
      filters[key] = [...new Set(values)];
    }
    result.push({ name: item.name, filters });
  }
  return result;
}
