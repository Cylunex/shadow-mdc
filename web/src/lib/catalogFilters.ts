import { z } from "zod";
import { catalogCategorySchema, catalogSortSchema, catalogStatusSchema, mediaAvailabilitySchema, organizationRoleSchema } from "./catalogTypes";

export const filtersSchema = z.object({
  q: z.string().default(""),
  category: catalogCategorySchema.optional().catch(undefined),
  collection: z.string().optional(), seed: z.number().int().min(0).max(1000000).optional().catch(undefined),
  actors: z.array(z.string()).default([]), actors_all: z.boolean().default(false),
  actor_name: z.string().optional(), studio_name: z.string().optional(),
  series_name: z.string().optional(), series: z.array(z.string()).default([]),
  tags: z.array(z.string()).default([]), tags_all: z.boolean().default(false),
  exclude_tags: z.array(z.string()).default([]), genres: z.array(z.string()).default([]),
  forms: z.array(z.string()).default([]), regions: z.array(z.string()).default([]),
  contents: z.array(z.string()).default([]), types: z.array(z.string()).default([]),
  languages: z.array(z.string()).default([]), subtitle_languages: z.array(z.string()).default([]),
  organizations: z.array(z.string()).default([]), organization_role: z.union([organizationRoleSchema, z.literal(""), z.literal("series")]).optional().catch(undefined),
  labels: z.array(z.string()).default([]), platforms: z.array(z.string()).default([]),
  year_from: z.number().int().optional().catch(undefined), year_to: z.number().int().optional().catch(undefined),
  unknown_year: z.boolean().default(false),
  duration_min: z.number().int().min(0).optional().catch(undefined),
  duration_max: z.number().int().positive().optional().catch(undefined),
  unknown_duration: z.boolean().default(false),
  sort: catalogSortSchema.catch("added"),
  status: catalogStatusSchema.catch("all"),
  availability: mediaAvailabilitySchema.catch("all"), wanted: z.boolean().optional(),
});
export type Filters = z.infer<typeof filtersSchema>;
export const EMPTY_CATALOG_FILTERS: Filters = filtersSchema.parse({});

const lists = {
  actors: "person", tags: "tag", exclude_tags: "exclude", genres: "genre",
  forms: "form", regions: "region", organizations: "org", series: "series", labels: "label", platforms: "platform",
  contents: "content", types: "type", languages: "language", subtitle_languages: "subtitle",
} as const;
const strings = { q: "q", actor_name: "cast", studio_name: "studio", series_name: "series_name", organization_role: "role", collection: "col" } as const;
const keys = [...Object.values(lists), ...Object.values(strings), "year", "from", "to", "sort", "status", "availability", "wanted", "cast_all", "match", "cat", "seed", "page", "duration_min", "duration_max", "duration_unknown"];

function year(value: string | null): number | undefined {
  return value && /^\d{4}$/.test(value) ? Number(value) : undefined;
}

/** Accept existing JSON/legacy links; the URL is authoritative, including a clean root. */
export function readCatalogFilters(search: string): Filters {
  const params = new URLSearchParams(search);
  let value: unknown = {};
  if (params.has("catalog") && !keys.some(key => key !== "sort" && key !== "status" && params.has(key))) {
    try {
      const legacy = JSON.parse(params.get("catalog")!);
      value = legacy && typeof legacy === "object" ? { ...legacy,
        ...(params.has("sort") ? { sort: params.get("sort") } : {}),
        ...(params.has("status") ? { status: params.get("status") } : {}),
      } : legacy;
    } catch { /* Malformed old links reset safely. */ }
  } else if (keys.some(key => params.has(key))) {
    const selectedYear = year(params.get("year"));
    value = {
      ...Object.fromEntries(Object.entries(lists).map(([field, key]) => [field, params.getAll(key).filter(Boolean)])),
      ...Object.fromEntries(Object.entries(strings).flatMap(([field, key]) => params.get(key) ? [[field, params.get(key)]] : [])),
      category: params.get("cat") ?? undefined, seed: params.has("seed") ? Number(params.get("seed")) : undefined,
      actors_all: params.get("cast_all") === "1",
      // Older tag links use AND; new OR selections explicitly carry match=any.
      tags_all: params.has("tag") && params.get("match") !== "any",
      year_from: selectedYear ?? year(params.get("from")),
      year_to: selectedYear ?? year(params.get("to")),
      unknown_year: params.get("year") === "unknown",
      duration_min: params.has("duration_min") ? Number(params.get("duration_min")) : undefined,
      duration_max: params.has("duration_max") ? Number(params.get("duration_max")) : undefined,
      unknown_duration: params.get("duration_unknown") === "1",
      sort: params.get("sort") ?? "added", status: params.get("status") ?? "all",
      availability: params.get("availability") ?? "all",
      wanted: params.get("wanted") === "1" ? true : params.get("wanted") === "0" ? false : undefined,
    };
  }
  const parsed = filtersSchema.safeParse(value);
  return parsed.success ? parsed.data : filtersSchema.parse({});
}

/** Only active filters are written; page/detail parameters and fragments are preserved. */
export function applyCatalogFiltersToUrl(url: URL, filters: Filters): void {
  for (const key of ["catalog", ...keys]) url.searchParams.delete(key);
  for (const [field, key] of Object.entries(strings)) {
    const value = filters[field as keyof typeof strings];
    if (value) url.searchParams.set(key, value);
  }
  for (const [field, key] of Object.entries(lists)) {
    for (const value of filters[field as keyof typeof lists]) if (value) url.searchParams.append(key, value);
  }
  if (filters.category && filters.category !== "all") url.searchParams.set("cat", filters.category);
  if (filters.sort === "random" && filters.seed) url.searchParams.set("seed", String(filters.seed));
  if (filters.actors.length && filters.actors_all) url.searchParams.set("cast_all", "1");
  if (filters.tags.length && !filters.tags_all) url.searchParams.set("match", "any");
  if (filters.unknown_year) url.searchParams.set("year", "unknown");
  else if (filters.year_from && filters.year_from === filters.year_to) url.searchParams.set("year", String(filters.year_from));
  else {
    if (filters.year_from) url.searchParams.set("from", String(filters.year_from));
    if (filters.year_to) url.searchParams.set("to", String(filters.year_to));
  }
  if (filters.sort !== "added") url.searchParams.set("sort", filters.sort);
  if (filters.status !== "all") url.searchParams.set("status", filters.status);
  if (filters.availability !== "all") url.searchParams.set("availability", filters.availability);
  if (filters.wanted !== undefined) url.searchParams.set("wanted", filters.wanted ? "1" : "0");
  if (filters.duration_min !== undefined) url.searchParams.set("duration_min", String(filters.duration_min));
  if (filters.duration_max !== undefined) url.searchParams.set("duration_max", String(filters.duration_max));
  if (filters.unknown_duration) url.searchParams.set("duration_unknown", "1");
}
