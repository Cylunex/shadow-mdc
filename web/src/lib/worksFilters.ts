/** URL-synced works list filter state (q/cat/tag/studio/cast/year/col/page). */

export type WorksCategory = "all" | "Japan" | "China" | "Korea" | "Europe" | "Other";

export type WorksFilterState = {
  q: string;
  category: WorksCategory;
  tags: string[];
  studio: string;
  cast: string;
  year: string;
  collection: string;
  page: number;
};

export const EMPTY_WORKS_FILTERS: WorksFilterState = {
  q: "",
  category: "all",
  tags: [],
  studio: "",
  cast: "",
  year: "",
  collection: "",
  page: 1
};

const CATEGORIES = new Set<string>(["all", "Japan", "China", "Korea", "Europe", "Other"]);

function parseCategory(value: string | null): WorksCategory {
  if (value && CATEGORIES.has(value)) return value as WorksCategory;
  return "all";
}

export function readWorksFiltersFromSearch(search: string = typeof window !== "undefined" ? window.location.search : ""): WorksFilterState {
  try {
    const params = new URLSearchParams(search);
    const tags = params.getAll("tag").map((item) => item.trim()).filter(Boolean);
    const pageRaw = Number(params.get("page") ?? "1");
    return {
      q: (params.get("q") ?? "").trim(),
      category: parseCategory(params.get("cat")),
      tags,
      studio: (params.get("studio") ?? "").trim(),
      cast: (params.get("cast") ?? "").trim(),
      year: (params.get("year") ?? "").trim(),
      collection: (params.get("col") ?? "").trim(),
      page: Number.isFinite(pageRaw) && pageRaw > 0 ? Math.floor(pageRaw) : 1
    };
  } catch {
    return { ...EMPTY_WORKS_FILTERS };
  }
}

export function worksFiltersActive(state: WorksFilterState): boolean {
  return Boolean(
    state.q
    || state.category !== "all"
    || state.tags.length > 0
    || state.studio
    || state.cast
    || state.year
    || state.collection
  );
}

export function worksFiltersEqual(a: WorksFilterState, b: WorksFilterState): boolean {
  return (
    a.q === b.q
    && a.category === b.category
    && a.studio === b.studio
    && a.cast === b.cast
    && a.year === b.year
    && a.collection === b.collection
    && a.page === b.page
    && a.tags.length === b.tags.length
    && a.tags.every((tag, index) => tag === b.tags[index])
  );
}

/** Apply filter params onto a URL; preserves work/actor detail params unless clearDetail. */
export function applyWorksFiltersToUrl(
  url: URL,
  state: WorksFilterState,
  options: { clearDetail?: boolean } = {}
): void {
  for (const key of ["q", "cat", "tag", "studio", "cast", "year", "col", "page"] as const) {
    url.searchParams.delete(key);
  }
  if (options.clearDetail) {
    url.searchParams.delete("work");
    url.searchParams.delete("actor");
  }
  if (state.q) url.searchParams.set("q", state.q);
  if (state.category !== "all") url.searchParams.set("cat", state.category);
  for (const tag of state.tags) {
    if (tag.trim()) url.searchParams.append("tag", tag.trim());
  }
  if (state.studio) url.searchParams.set("studio", state.studio);
  if (state.cast) url.searchParams.set("cast", state.cast);
  if (state.year) url.searchParams.set("year", state.year);
  if (state.collection) url.searchParams.set("col", state.collection);
  if (state.page > 1) url.searchParams.set("page", String(state.page));
}

export function writeWorksFiltersToUrl(
  state: WorksFilterState,
  mode: "replace" | "push" = "replace",
  options: { clearDetail?: boolean } = {}
): void {
  if (typeof window === "undefined") return;
  const url = new URL(window.location.href);
  applyWorksFiltersToUrl(url, state, options);
  const next = `${url.pathname}${url.search}${url.hash}`;
  const current = `${window.location.pathname}${window.location.search}${window.location.hash}`;
  if (next === current) return;
  if (mode === "push") window.history.pushState({ worksFilters: state }, "", next);
  else window.history.replaceState({ worksFilters: state }, "", next);
}

/**
 * Merge a patch into current URL filters and write.
 * Non-page field changes reset page to 1 unless `page` is explicitly set.
 * `replaceAll: true` starts from empty filters.
 */
export function patchWorksFilters(
  patch: Partial<WorksFilterState>,
  options: { replaceAll?: boolean; clearDetail?: boolean; mode?: "replace" | "push" } = {}
): WorksFilterState {
  const base = options.replaceAll ? { ...EMPTY_WORKS_FILTERS } : readWorksFiltersFromSearch();
  const keys = Object.keys(patch) as Array<keyof WorksFilterState>;
  const touchesNonPage = keys.some((key) => key !== "page");
  const next: WorksFilterState = {
    q: patch.q !== undefined ? patch.q : base.q,
    category: patch.category !== undefined ? patch.category : base.category,
    tags: patch.tags !== undefined ? [...patch.tags] : [...base.tags],
    studio: patch.studio !== undefined ? patch.studio : base.studio,
    cast: patch.cast !== undefined ? patch.cast : base.cast,
    year: patch.year !== undefined ? patch.year : base.year,
    collection: patch.collection !== undefined ? patch.collection : base.collection,
    page: patch.page !== undefined ? patch.page : touchesNonPage ? 1 : base.page
  };
  writeWorksFiltersToUrl(next, options.mode ?? "replace", { clearDetail: options.clearDetail });
  return next;
}
