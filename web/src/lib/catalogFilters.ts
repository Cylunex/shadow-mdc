import { z } from "zod";

const filtersSchema = z.object({
  q: z.string().default(""),
  actors: z.array(z.string()).default([]), actors_all: z.boolean().default(false),
  actor_name: z.string().optional(), studio_name: z.string().optional(),
  tags: z.array(z.string()).default([]), tags_all: z.boolean().default(false),
  exclude_tags: z.array(z.string()).default([]), genres: z.array(z.string()).default([]),
  forms: z.array(z.string()).default([]), regions: z.array(z.string()).default([]),
  organizations: z.array(z.string()).default([]), organization_role: z.string().optional(),
  year_from: z.number().int().optional().catch(undefined), year_to: z.number().int().optional().catch(undefined),
  unknown_year: z.boolean().default(false),
  sort: z.enum(["added", "newest", "oldest", "title"]).catch("added"),
  status: z.enum(["all", "emby", "files", "metadata"]).catch("all"),
});
export type Filters = z.infer<typeof filtersSchema>;
export const EMPTY_CATALOG_FILTERS: Filters = filtersSchema.parse({});

const lists = {
  actors: "person", tags: "tag", exclude_tags: "exclude", genres: "genre",
  forms: "form", regions: "region", organizations: "org",
} as const;
const strings = { q: "q", actor_name: "cast", studio_name: "studio", organization_role: "role" } as const;
const keys = [...Object.values(lists), ...Object.values(strings), "year", "from", "to", "sort", "status", "cast_all", "match"];

function year(value: string | null): number | undefined {
  return value && /^\d{4}$/.test(value) ? Number(value) : undefined;
}

/** Accept existing JSON/legacy links; the URL is authoritative, including a clean root. */
export function readCatalogFilters(search: string): Filters {
  const params = new URLSearchParams(search);
  let value: unknown = {};
  if (params.has("catalog") && !keys.some(key => key !== "sort" && key !== "status" && params.has(key))) {
    try { value = JSON.parse(params.get("catalog")!); } catch { /* Malformed old links reset safely. */ }
  } else if (keys.some(key => params.has(key))) {
    const selectedYear = year(params.get("year"));
    value = {
      ...Object.fromEntries(Object.entries(lists).map(([field, key]) => [field, params.getAll(key).filter(Boolean)])),
      ...Object.fromEntries(Object.entries(strings).flatMap(([field, key]) => params.get(key) ? [[field, params.get(key)]] : [])),
      actors_all: params.get("cast_all") === "1",
      // Older tag links use AND; new OR selections explicitly carry match=any.
      tags_all: params.has("tag") && params.get("match") !== "any",
      year_from: selectedYear ?? year(params.get("from")),
      year_to: selectedYear ?? year(params.get("to")),
      unknown_year: params.get("year") === "unknown",
      sort: params.get("sort") ?? "added", status: params.get("status") ?? "all",
    };
  }
  const parsed = filtersSchema.safeParse(value);
  return parsed.success ? parsed.data : filtersSchema.parse({});
}

/** Only active filters are written; page/detail parameters and fragments are preserved. */
export function applyCatalogFiltersToUrl(url: URL, filters: Filters): void {
  const legacySort = url.searchParams.get("sort") ?? "";
  const legacyStatus = url.searchParams.get("status") ?? "";
  for (const key of ["catalog", ...keys]) url.searchParams.delete(key);
  for (const [field, key] of Object.entries(strings)) {
    const value = filters[field as keyof typeof strings];
    if (value) url.searchParams.set(key, value);
  }
  for (const [field, key] of Object.entries(lists)) {
    for (const value of filters[field as keyof typeof lists]) if (value) url.searchParams.append(key, value);
  }
  if (filters.actors.length && filters.actors_all) url.searchParams.set("cast_all", "1");
  if (filters.tags.length && !filters.tags_all) url.searchParams.set("match", "any");
  if (filters.unknown_year) url.searchParams.set("year", "unknown");
  else if (filters.year_from && filters.year_from === filters.year_to) url.searchParams.set("year", String(filters.year_from));
  else {
    if (filters.year_from) url.searchParams.set("from", String(filters.year_from));
    if (filters.year_to) url.searchParams.set("to", String(filters.year_to));
  }
  if (filters.sort !== "added") url.searchParams.set("sort", filters.sort);
  else if (["ranked", "code", "random"].includes(legacySort)) url.searchParams.set("sort", legacySort);
  if (filters.status !== "all") url.searchParams.set("status", filters.status);
  else if (["undownloaded", "want", "downloaded"].includes(legacyStatus)) url.searchParams.set("status", legacyStatus);
}
