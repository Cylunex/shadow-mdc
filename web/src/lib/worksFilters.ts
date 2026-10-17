/** URL-synced works list filter state (q/cat/tag/studio/cast/year/col/page). */

export type WorksCategory = "all" | "Japan" | "China" | "Korea" | "Europe" | "Other";

/** Quick status chips (JHS-inspired 「全部未下载」). */
export type WorksStatusFilter = "all" | "undownloaded" | "want" | "downloaded";

/** Card ordering for the library grid (JavRanking explore-style controls). */
export type WorksSort = "added" | "newest" | "oldest" | "ranked" | "code" | "random";

export type WorksFilterState = {
  q: string;
  category: WorksCategory;
  status: WorksStatusFilter;
  sort: WorksSort;
  tags: string[];
  studio: string;
  cast: string;
  year: string;
  collection: string;
  page: number;
};

export const WORKS_SORT_OPTIONS: Array<{ value: WorksSort; label: string; hint: string }> = [
  { value: "added", label: "最新收录", hint: "按入库时间倒序（默认）" },
  { value: "newest", label: "最新发行", hint: "按发行日期从新到旧" },
  { value: "oldest", label: "最早发行", hint: "按发行日期从旧到新" },
  { value: "ranked", label: "榜单优先", hint: "有 JavRanking 名次的作品优先，名次靠前在前" },
  { value: "code", label: "番号 A→Z", hint: "按番号排序" },
  { value: "random", label: "手气不错", hint: "随机打散（每次点击重新抽）" }
];

export const EMPTY_WORKS_FILTERS: WorksFilterState = {
  q: "",
  category: "all",
  status: "all",
  sort: "added",
  tags: [],
  studio: "",
  cast: "",
  year: "",
  collection: "",
  page: 1
};

const SORTS = new Set<string>(WORKS_SORT_OPTIONS.map((item) => item.value));

function parseSort(value: string | null): WorksSort {
  if (value && SORTS.has(value)) return value as WorksSort;
  return "added";
}

const CATEGORIES = new Set<string>(["all", "Japan", "China", "Korea", "Europe", "Other"]);

function parseCategory(value: string | null): WorksCategory {
  if (value && CATEGORIES.has(value)) return value as WorksCategory;
  return "all";
}

const STATUS_FILTERS = new Set<string>(["all", "undownloaded", "want", "downloaded"]);

function parseStatus(value: string | null): WorksStatusFilter {
  if (value && STATUS_FILTERS.has(value)) return value as WorksStatusFilter;
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
      status: parseStatus(params.get("status")),
      sort: parseSort(params.get("sort")),
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

/** Minimal work shape the grid sorter needs (keeps this module dependency-free). */
export type SortableWork = {
  id: string;
  release_date?: string | null;
  primary_code?: string | null;
  created_at?: string;
  javranking_rank?: number | null;
  javranking_score?: number | null;
};

function seededRandom(seed: number): () => number {
  let state = seed >>> 0;
  return () => {
    state = (state + 0x6d2b79f5) >>> 0;
    let t = state;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function dateValue(value: string | null | undefined): number {
  if (!value) return Number.NEGATIVE_INFINITY;
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : Number.NEGATIVE_INFINITY;
}

/** Order grid rows for the selected sort; unrated/unreleased rows sink to the end. */
export function sortWorkRows<T extends { work: SortableWork }>(
  rows: T[],
  sort: WorksSort,
  seed = 1
): T[] {
  if (sort === "added") return rows;
  const sorted = [...rows];
  if (sort === "random") {
    const random = seededRandom(seed);
    for (let index = sorted.length - 1; index > 0; index -= 1) {
      const swap = Math.floor(random() * (index + 1));
      const held = sorted[index]!;
      sorted[index] = sorted[swap]!;
      sorted[swap] = held;
    }
    return sorted;
  }
  sorted.sort((left, right) => {
    const a = left.work;
    const b = right.work;
    if (sort === "newest" || sort === "oldest") {
      const diff = dateValue(b.release_date) - dateValue(a.release_date);
      if (diff !== 0) return sort === "newest" ? diff : -diff;
      return dateValue(b.created_at) - dateValue(a.created_at);
    }
    if (sort === "ranked") {
      const rankA = a.javranking_rank ?? Number.POSITIVE_INFINITY;
      const rankB = b.javranking_rank ?? Number.POSITIVE_INFINITY;
      if (rankA !== rankB) return rankA - rankB;
      const scoreDiff = (b.javranking_score ?? Number.NEGATIVE_INFINITY)
        - (a.javranking_score ?? Number.NEGATIVE_INFINITY);
      if (scoreDiff !== 0) return scoreDiff;
      return dateValue(b.release_date) - dateValue(a.release_date);
    }
    return (a.primary_code ?? "").localeCompare(b.primary_code ?? "", "en");
  });
  return sorted;
}

export function worksFiltersActive(state: WorksFilterState): boolean {  return Boolean(
    state.q
    || state.category !== "all"
    || state.status !== "all"
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
    && a.status === b.status
    && a.sort === b.sort
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
  for (const key of ["q", "cat", "status", "sort", "tag", "studio", "cast", "year", "col", "page"] as const) {
    url.searchParams.delete(key);
  }
  if (options.clearDetail) {
    url.searchParams.delete("work");
    url.searchParams.delete("actor");
  }
  if (state.q) url.searchParams.set("q", state.q);
  if (state.category !== "all") url.searchParams.set("cat", state.category);
  if (state.status !== "all") url.searchParams.set("status", state.status);
  if (state.sort !== "added") url.searchParams.set("sort", state.sort);
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
    status: patch.status !== undefined ? patch.status : base.status,
    sort: patch.sort !== undefined ? patch.sort : base.sort,
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
