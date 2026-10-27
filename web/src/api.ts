import { invalidateMutation, queryClient, queryKeys } from "./query/client";
import {
  z } from "zod";

const providerHealthSchema = z.object({
  providers: z.array(z.object({
    provider: z.string(),
  configured: z.boolean(),
  failures: z.number(),
  cooldown: z.boolean(),
  retry_in_seconds: z.number().nullable().optional()
  }))
});

import {
  actorProfilesSchema,
  assetInboxListSchema,
  artworkDownloadSchema,
  batchApplySchema,
  batchPlanSchema,
  bulkIdentifySchema,
  bulkTranslateSchema,
  candidateSchema,
  candidatesSchema,
  directoryActorAssignSchema,
  catalogImportResultSchema,
  filterWordsSchema,
  identifySchema,
  identityAliasesSchema,
  inboxBatchResultSchema,
  librariesSchema,
  librarySchema,
  nonJavActorSchema,
  nonJavActorsSchema,
  providerListSchema,
  providerDiagnoseSchema,
  scanSchema,
  sampleGenerateSchema,
  screenshotGenerateSchema,
  workSampleGenerateSchema,
  taskRunsSchema,
  workDetailSchema,
  workRelatedSchema,
  workSchema,
  workLookupSchema,
  worksSchema,
  workSearchSchema,
  actorWorksSchema,
  workTagFacetsSchema,
  categoriesPageSchema,
  collectionsSchema,
  collectionSchema,
  collectionSeedResultSchema,
  gfriendsFillResultSchema,
  fieldPrioritySchema,
  lexiconExportSchema,
  taskRunSchema,
  discoverPageSchema,
  discoverSeedSchema,
  multiSiteSearchSchema,
  magnetLinkSchema,
  workMagnetSchema,
  libraryPrefsSchema,
  subscriptionScanSchema,
  mediaServerSettingsSchema,
  panStatusSchema,
  panFilesSchema,
  panOfflineTaskSchema,
  panSettingsSchema,
  panPipelineSchema,
  openListTestSchema,
  openListFilesSchema,
  subscriptionWatchStatusSchema,
  panLoginStatusSchema,
  panLoginSchema,
  javRankingSectionsSchema,
  javRankingListSchema,
  javRankingSeedResultSchema
} from "./model";
import type { FilterWords, IdentityAliases } from "./model";

export type DisplayMediaCategory = "Japan" | "China" | "Korea" | "Europe" | "Other";

export type NonJavActorEditPayload = {
  name: string;
  aliases: string[];
  groups: string[];
  categories: DisplayMediaCategory[];
  x_handle?: string | null;
  biography: string | null;
  notes: string | null;
};

export type OrganizePayload = {
  mode: "sidecar" | "copy" | "move" | "hardlink" | "symlink";
  target_root?: string | null;
  template?: string | null;
};

const appBaseUrl = import.meta.env.BASE_URL.replace(/\/$/, "");

export function appUrl(path: string | null): string | null {
  if (path === null || !path.startsWith("/")) return path;
  return `${appBaseUrl}${path}`;
}

async function request<T>(schema: z.ZodType<T>, path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(appUrl(path) ?? path, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...init?.headers
    }
  });
  if (!response.ok) {
    const detail = await response.text();
    throw new Error(detail || `HTTP ${response.status}`);
  }
  let result: T;
  try {
    const payload: unknown = await response.json();
    result = schema.parse(payload);
  } catch (error) {
    if (init?.method && init.method !== "GET") await invalidateMutation(path);
    throw error;
  }
  if (init?.method && init.method !== "GET") {
    const preferences = path.startsWith("/api/library-prefs") ? libraryPrefsSchema.safeParse(result) : null;
    if (preferences?.success) {
      await queryClient.cancelQueries({ queryKey: queryKeys.prefs });
      queryClient.setQueryData(queryKeys.prefs, preferences.data);
    } else await invalidateMutation(path);
  }
  return result;
}

export const api = {
  libraries: (signal?: AbortSignal) => request(librariesSchema, "/api/libraries", { signal }),
  createLibrary: (payload: {
    name: string;
    root_path: string;
    recognition_scope: "all" | "jav_only";
  }) =>
    request(librarySchema, "/api/libraries", { method: "POST", body: JSON.stringify(payload) }),
  updateLibrary: (libraryId: string, payload: { recognition_scope: "all" | "jav_only" }) =>
    request(librarySchema, `/api/libraries/${libraryId}`, {
      method: "PATCH",
      body: JSON.stringify(payload)
    }),
  scan: (libraryId: string, options: { only_new?: boolean } = {}) =>
    request(scanSchema, `/api/libraries/${libraryId}/scan`, {
      method: "POST",
      body: JSON.stringify({ only_new: Boolean(options.only_new) })
    }),
  generateScreenshots: (libraryId: string, limit = 50) =>
    request(screenshotGenerateSchema, `/api/libraries/${libraryId}/screenshots`, {
      method: "POST",
      body: JSON.stringify({ limit })
    }),
  generateLibrarySamples: (libraryId: string, limit = 50, targetCount = 5) =>
    request(sampleGenerateSchema, `/api/libraries/${libraryId}/samples`, {
      method: "POST",
      body: JSON.stringify({ limit, target_count: targetCount })
    }),
  generateWorkSamples: (workId: string, targetCount = 5) =>
    request(workSampleGenerateSchema, `/api/works/${workId}/samples?target_count=${targetCount}`, {
      method: "POST"
    }),
  identifyLibrary: (
    libraryId: string,
    limit = 20,
    options: { continue_failed?: boolean; skip_identified?: boolean; skip_remote_when_identified?: boolean } = {}
  ) =>
    request(bulkIdentifySchema, `/api/libraries/${libraryId}/identify`, {
      method: "POST",
      body: JSON.stringify({
        limit,
        continue_failed: Boolean(options.continue_failed),
        skip_identified: options.skip_identified !== false,
        skip_remote_when_identified: options.skip_remote_when_identified !== false
      })
    }),
  tasks: (signal?: AbortSignal) => request(taskRunsSchema, "/api/tasks", { signal }),
  cancelTask: (taskId: string) => request(taskRunSchema, `/api/tasks/${taskId}/cancel`, { method: "POST" }),
  discoverBrowse: (params: { provider?: string; list?: string; page?: number } = {}) => {
    const query = new URLSearchParams();
    if (params.provider) query.set("provider", params.provider);
    if (params.list) query.set("list", params.list);
    if (params.page) query.set("page", String(params.page));
    const suffix = query.toString() ? `?${query}` : "";
    return request(discoverPageSchema, `/api/discover/browse${suffix}`);
  },
  discoverSearch: (q: string, params: { provider?: string; page?: number } = {}) => {
    const query = new URLSearchParams({ q });
    if (params.provider) query.set("provider", params.provider);
    if (params.page) query.set("page", String(params.page));
    return request(discoverPageSchema, `/api/discover/search?${query}`);
  },
  discoverMultiSearch: (q: string, includeMagnets = true) => {
    const query = new URLSearchParams({ q, include_magnets: includeMagnets ? "true" : "false" });
    return request(multiSiteSearchSchema, `/api/discover/multi-search?${query}`);
  },
  discoverSeed: (payload: { provider: string; external_id?: string; source_url?: string; code?: string }) =>
    request(discoverSeedSchema, "/api/discover/seed", { method: "POST", body: JSON.stringify(payload) }),
  javrankingSections: (forceRefresh = false) => {
    const query = forceRefresh ? "?force_refresh=true" : "";
    return request(javRankingSectionsSchema, `/api/javranking/sections${query}`);
  },
  javrankingList: (slug: string, forceRefresh = false) => {
    const query = forceRefresh ? "?force_refresh=true" : "";
    return request(javRankingListSchema, `/api/javranking/lists/${encodeURIComponent(slug)}${query}`);
  },
  javrankingSeed: (payload: {
    list_slug?: string;
    ranking_slug?: string;
    min_rank?: number;
    limit?: number;
    dry_run?: boolean;
    force_refresh?: boolean;
  }) =>
    request(javRankingSeedResultSchema, "/api/javranking/seed", {
      method: "POST",
      body: JSON.stringify(payload)
    }),
  discoverMagnets: (provider: string, externalId: string, sourceUrl?: string) => {
    const query = sourceUrl ? `?${new URLSearchParams({ source_url: sourceUrl })}` : "";
    return request(z.array(magnetLinkSchema), `/api/discover/${provider}/${externalId}/magnets${query}`);
  },
  workMagnets: (workId: string) => request(z.array(workMagnetSchema), `/api/works/${workId}/magnets`),
  saveWorkMagnets: (workId: string, magnets: unknown[], provider?: string) =>
    request(z.array(workMagnetSchema), `/api/works/${workId}/magnets`, {
      method: "POST",
      body: JSON.stringify({ magnets, provider })
    }),
  deleteWorkMagnet: async (workId: string, magnetId: string): Promise<void> => {
    const path = `/api/works/${workId}/magnets/${magnetId}`;
    const response = await fetch(appUrl(path) ?? path, { method: "DELETE" });
    if (!response.ok) throw new Error(await response.text());
    await invalidateMutation(path);
  },
  retryTask: (taskId: string) => request(taskRunSchema, `/api/tasks/${taskId}/retry`, { method: "POST" }),
  providerHealth: () => request(providerHealthSchema, "/api/providers/health"),
  assets: (signal?: AbortSignal) => request(assetInboxListSchema, "/api/inbox", { signal }),
  candidates: (assetId: string) => request(candidatesSchema, `/api/assets/${assetId}/candidates`),
  manualCandidate: (assetId: string, payload: { title?: string }) =>
    request(candidateSchema, `/api/assets/${assetId}/manual-candidate`, {
      method: "POST",
      body: JSON.stringify(payload)
    }),
  identify: (assetId: string, payload: { title?: string; source_url?: string }) =>
    request(identifySchema, `/api/assets/${assetId}/identify`, {
      method: "POST",
      body: JSON.stringify(payload)
    }),
  accept: (candidateId: string) =>
    request(workSchema, `/api/candidates/${candidateId}/accept`, { method: "POST" }),
  batchAcceptInbox: (assetIds: string[]) =>
    request(inboxBatchResultSchema, "/api/inbox/batch/accept", {
      method: "POST",
      body: JSON.stringify({ asset_ids: assetIds })
    }),
  batchIgnoreInbox: (assetIds: string[]) =>
    request(inboxBatchResultSchema, "/api/inbox/batch/ignore", {
      method: "POST",
      body: JSON.stringify({ asset_ids: assetIds })
    }),
  batchApplyActorInbox: (
    assetIds: string[],
    actor: string,
    category: DisplayMediaCategory
  ) =>
    request(inboxBatchResultSchema, "/api/inbox/batch/actor", {
      method: "POST",
      body: JSON.stringify({ asset_ids: assetIds, actor, category })
    }),
  works: (params?: {
    collection_id?: string;
    collection_kind?: string;
    collection?: string;
    tags?: string[];
  }, signal?: AbortSignal) => {
    const query = new URLSearchParams();
    query.set("compact", "true");
    if (params?.collection_id) query.set("collection_id", params.collection_id);
    if (params?.collection_kind) query.set("collection_kind", params.collection_kind);
    if (params?.collection) query.set("collection", params.collection);
    for (const tag of params?.tags ?? []) {
      if (tag.trim()) query.append("tag", tag.trim());
    }
    const suffix = query.toString() ? `?${query}` : "";
    return request(worksSchema, `/api/works${suffix}`, { signal });
  },
  searchWorks: (params: {
    q: string;
    limit?: number;
    offset?: number;
    category?: string;
    tags?: string[];
  }) => {
    const query = new URLSearchParams();
    query.set("q", params.q);
    if (params.limit != null) query.set("limit", String(params.limit));
    if (params.offset != null) query.set("offset", String(params.offset));
    if (params.category && params.category !== "all") query.set("category", params.category);
    for (const tag of params.tags ?? []) {
      if (tag.trim()) query.append("tag", tag.trim());
    }
    return request(workSearchSchema, `/api/works/search?${query}`);
  },
  actorWorks: (actorRef: string, signal?: AbortSignal) =>
    request(actorWorksSchema, `/api/actors/${encodeURIComponent(actorRef)}/works`, { signal }),
  workTagFacets: (limit = 40) =>
    request(workTagFacetsSchema, `/api/works/tags?limit=${limit}`),
  categories: (params: { onlyWithWorks?: boolean } = {}, signal?: AbortSignal) => {
    const query = params.onlyWithWorks ? "?only_with_works=true" : "";
    return request(categoriesPageSchema, `/api/categories${query}`, { signal });
  },
  collections: (params?: { kind?: string; q?: string; seed_if_empty?: boolean }) => {
    const query = new URLSearchParams();
    if (params?.kind) query.set("kind", params.kind);
    if (params?.q) query.set("q", params.q);
    if (params?.seed_if_empty) query.set("seed_if_empty", "true");
    const suffix = query.toString() ? `?${query}` : "";
    return request(collectionsSchema, `/api/collections${suffix}`);
  },
  collection: (collectionId: string) => request(collectionSchema, `/api/collections/${collectionId}`),
  seedCollections: () =>
    request(collectionSeedResultSchema, "/api/collections/seed", { method: "POST" }),
  fillGfriendsActorImages: (payload: {
    dry_run?: boolean;
    download?: boolean;
    limit?: number | null;
    force_refresh?: boolean;
    localize?: boolean;
  } = {}) =>
    request(gfriendsFillResultSchema, "/api/actors/fill-gfriends-images", {
      method: "POST",
      body: JSON.stringify({
        dry_run: payload.dry_run ?? false,
        download: payload.download ?? true,
        limit: payload.limit ?? null,
        force_refresh: payload.force_refresh ?? false,
        localize: payload.localize ?? false
      })
    }),
  workDetail: (workId: string, signal?: AbortSignal) => request(workDetailSchema, `/api/works/${workId}`, { signal }),
  workRelated: (workId: string, limit = 18, signal?: AbortSignal) =>
    request(workRelatedSchema, `/api/works/${workId}/related?limit=${limit}`, { signal }),
  updateWork: (
    workId: string,
    payload: {
      title?: string;
      actors?: string[];
      studio?: string | null;
      series?: string | null;
      tags?: string[];
      plot?: string | null;
      lock_edited?: boolean;
    }
  ) =>
    request(workDetailSchema, `/api/works/${workId}`, {
      method: "PATCH",
      body: JSON.stringify(payload)
    }),
  updateWorkLocks: (workId: string, locks: string[]) =>
    request(workDetailSchema, `/api/works/${workId}/locks`, {
      method: "PUT",
      body: JSON.stringify({ locks })
    }),
  preferWorkPoster: (workId: string, artworkIndex: number) =>
    request(workDetailSchema, `/api/works/${workId}/artwork/prefer`, {
      method: "POST",
      body: JSON.stringify({ artwork_index: artworkIndex })
    }),
  actors: (signal?: AbortSignal) => request(actorProfilesSchema, "/api/actors", { signal }),
  nonJavActors: (signal?: AbortSignal) => request(nonJavActorsSchema, "/api/non-jav-actors", { signal }),
  createNonJavActor: (payload: NonJavActorEditPayload) =>
    request(nonJavActorSchema, "/api/non-jav-actors", {
      method: "POST",
      body: JSON.stringify(payload)
    }),
  updateNonJavActor: (actorName: string, payload: NonJavActorEditPayload) =>
    request(nonJavActorSchema, `/api/non-jav-actors/${encodeURIComponent(actorName)}`, {
      method: "PATCH",
      body: JSON.stringify(payload)
    }),
  deleteNonJavActor: async (actorName: string): Promise<void> => {
    const path = `/api/non-jav-actors/${encodeURIComponent(actorName)}`;
    const response = await fetch(appUrl(path) ?? path, {
      method: "DELETE"
    });
    if (!response.ok) throw new Error(await response.text());
    await invalidateMutation(path);
  },
  uploadNonJavActorImage: (actorName: string, file: File) =>
    request(nonJavActorSchema, `/api/non-jav-actors/${encodeURIComponent(actorName)}/image`, {
      method: "POST",
      body: file,
      headers: { "Content-Type": file.type || "application/octet-stream" }
    }),
  assignDirectoryActor: (
    assetId: string,
    actor: string,
    category: DisplayMediaCategory,
    directory: string
  ) =>
    request(directoryActorAssignSchema, `/api/assets/${assetId}/directory-actor`, {
      method: "POST",
      body: JSON.stringify({ actor, category, directory })
    }),
  translateWorks: (limit = 200) => request(bulkTranslateSchema, "/api/works/translate", {
    method: "POST",
    body: JSON.stringify({ limit })
  }),
  lookupWork: (payload: { code?: string; source_url?: string; external_ids?: Record<string, string> }) =>
    request(workLookupSchema, "/api/works/lookup", {
      method: "POST",
      body: JSON.stringify(payload)
    }),
  patchActor: (actorId: string, x_handle: string | null) =>
    request(z.object({
      id: z.string(),
      name: z.string(),
      image_url: z.string().nullable().optional(),
      x_handle: z.string().nullable().optional(),
      x_url: z.string().nullable().optional()
    }), `/api/actors/${actorId}`, {
      method: "PATCH",
      body: JSON.stringify({ x_handle })
    }),
  fieldPriority: () => request(fieldPrioritySchema, "/api/settings/field-priority"),
  saveFieldPriority: (priorities: Record<string, string[]>) =>
    request(fieldPrioritySchema, "/api/settings/field-priority", {
      method: "PUT",
      body: JSON.stringify({ priorities })
    }),
  exportLexicon: () => request(lexiconExportSchema, "/api/lexicon/export"),
  refreshWork: (workId: string) =>
    request(identifySchema, `/api/works/${workId}/refresh`, { method: "POST" }),
  downloadArtwork: (workId: string) =>
    request(artworkDownloadSchema, `/api/works/${workId}/artwork/download`, { method: "POST" }),
  providers: () => request(providerListSchema, "/api/providers"),
  diagnoseProviders: (code: string) => request(providerDiagnoseSchema, "/api/providers/diagnose", {
    method: "POST",
    body: JSON.stringify({ code })
  }),
  planLibrary: (libraryId: string, payload: OrganizePayload) =>
    request(batchPlanSchema, `/api/libraries/${libraryId}/organize/plan`, {
      method: "POST",
      body: JSON.stringify(payload)
    }),
  applyLibraryPlan: (
    libraryId: string,
    payload: OrganizePayload & { token: string; nfo_policy: "error" | "skip" | "replace" }
  ) => request(batchApplySchema, `/api/libraries/${libraryId}/organize/apply`, {
    method: "POST",
    body: JSON.stringify(payload)
  }),
  identityAliases: () => request(identityAliasesSchema, "/api/settings/identity-aliases"),
  saveIdentityAliases: (payload: IdentityAliases) =>
    request(identityAliasesSchema, "/api/settings/identity-aliases", {
      method: "PUT",
      body: JSON.stringify(payload)
    }),
  filterWords: () => request(filterWordsSchema, "/api/settings/filter-words"),
  saveFilterWords: (payload: FilterWords) =>
    request(filterWordsSchema, "/api/settings/filter-words", {
      method: "PUT",
      body: JSON.stringify(payload)
    }),
  importCatalogFromPath: (payload: {
    path: string;
    dry_run?: boolean;
    actors_only?: boolean;
    works_only?: boolean;
    include_formal?: boolean;
  }) =>
    request(catalogImportResultSchema, "/api/catalog/import", {
      method: "POST",
      body: JSON.stringify(payload)
    }),
  importCatalogUpload: async (
    file: File,
    options: { dry_run?: boolean; actors_only?: boolean; works_only?: boolean; include_formal?: boolean } = {}
  ) => {
    const params = new URLSearchParams();
    if (options.dry_run) params.set("dry_run", "true");
    if (options.actors_only) params.set("actors_only", "true");
    if (options.works_only) params.set("works_only", "true");
    if (options.include_formal === false) params.set("include_formal", "false");
    const query = params.toString();
    const path = `/api/catalog/import/upload${query ? `?${query}` : ""}`;
    const body = new FormData();
    body.append("file", file);
    const response = await fetch(appUrl(path) ?? path, { method: "POST", body });
    if (!response.ok) throw new Error(await response.text());
    const result = catalogImportResultSchema.parse(await response.json());
    if (!options.dry_run) await invalidateMutation(path);
    return result;
  },
  libraryPrefs: (signal?: AbortSignal) => request(libraryPrefsSchema, "/api/library-prefs", { signal }),
  setWantList: (work_id: string, wanted: boolean) =>
    request(libraryPrefsSchema, "/api/library-prefs/want-list", {
      method: "PUT", body: JSON.stringify({ work_id, wanted })
    }),
  setActorTags: (payload: { actor_key: string; favorite?: boolean; subscribe?: boolean; blacklist?: boolean }) =>
    request(libraryPrefsSchema, "/api/library-prefs/actor-tags", {
      method: "PUT", body: JSON.stringify(payload)
    }),
  upsertSubscription: (payload: {
    actor_key: string; actor_name: string; start_date: string; max_cast: number; enabled: boolean; notes?: string | null;
  }) => request(libraryPrefsSchema, "/api/library-prefs/subscriptions", {
    method: "PUT", body: JSON.stringify(payload)
  }),
  removeSubscription: (actorKey: string) =>
    request(libraryPrefsSchema, `/api/library-prefs/subscriptions/${encodeURIComponent(actorKey)}`, { method: "DELETE" }),
  patchQueueItem: (itemId: string, status: "pending" | "accepted" | "dismissed") =>
    request(libraryPrefsSchema, `/api/library-prefs/queue/${itemId}`, {
      method: "PATCH", body: JSON.stringify({ status })
    }),
  scanSubscriptions: () =>
    request(subscriptionScanSchema, "/api/library-prefs/subscriptions/scan", { method: "POST" }),
  mediaServer: () => request(mediaServerSettingsSchema, "/api/settings/media-server"),
  saveMediaServer: (payload: {
    enabled: boolean; kind: string; base_url: string | null; api_key: string | null;
    verify_nfo_fields: boolean; deep_link_template?: string | null;
    notify_debounce_seconds?: number;
  }) => request(mediaServerSettingsSchema, "/api/settings/media-server", {
    method: "PUT", body: JSON.stringify(payload)
  }),
  panStatus: (signal?: AbortSignal) => request(panStatusSchema, "/api/pan/status", { signal }),
  panImportTokens: (payload: {
    access_token: string; refresh_token: string; expires_in?: number;
    user_id?: string | null; user_name?: string | null;
  }) => request(panStatusSchema, "/api/pan/credentials", {
    method: "POST", body: JSON.stringify(payload)
  }),
  panLogin: () => request(panLoginSchema, "/api/pan/login", { method: "POST" }),
  panLoginStatus: (id: string) => request(panLoginStatusSchema, `/api/pan/login/${id}`),
  panAccount: () => request(z.object({
    connected: z.boolean(),
    account: z.unknown().optional(),
    directory: z.object({ id: z.string().nullable() }).nullable().optional(),
    strm: z.unknown().optional(),
    use_proxy: z.boolean().optional(),
    client_id: z.string().optional()
  }).passthrough(), "/api/pan/account"),
  panDisconnect: async (): Promise<void> => {
    const path = "/api/pan/account";
    const response = await fetch(appUrl(path) ?? path, { method: "DELETE" });
    if (!response.ok) throw new Error(await response.text());
    await invalidateMutation(path);
  },
  panFiles: (directoryId = "0", page = 1) => {
    const query = new URLSearchParams({ directory_id: directoryId, page: String(page) });
    return request(panFilesSchema, `/api/pan/files?${query}`);
  },
  panSetDirectory: (id: string) =>
    request(z.object({ id: z.string().nullable() }), "/api/pan/directory", {
      method: "PUT", body: JSON.stringify({ id })
    }),
  panSettings: () => request(panSettingsSchema, "/api/pan/settings"),
  savePanSettings: (payload: {
    offline_directory_id: string | null;
    strm_enabled: boolean;
    strm_output_root: string | null;
    strm_url_prefix: string;
    strm_mode?: "openlist" | "relay";
    strm_public_base_url?: string | null;
    strm_token?: string;
    strm_user_agent?: string | null;
    strm_emby_root?: string | null;
    strm_layout_template?: string;
    strm_reconcile_interval_hours?: number;
    use_proxy: boolean;
    subscription_auto_offline: boolean;
    auto_switch?: boolean;
    client_id?: string;
    client_secret?: string;
    pan_backend?: "115_open" | "openlist";
    openlist_base_url?: string | null;
    openlist_strm_base_url?: string | null;
    openlist_offline_path?: string | null;
    openlist_offline_tool?: string;
    openlist_delete_policy?: string;
    openlist_strm_sign?: boolean;
  }) => request(panSettingsSchema, "/api/pan/settings", {
    method: "PUT", body: JSON.stringify(payload)
  }),
  savePanBackend: (payload: {
    pan_backend?: "115_open" | "openlist";
    openlist_base_url?: string | null;
    openlist_strm_base_url?: string | null;
    openlist_offline_path?: string | null;
    openlist_offline_tool?: string;
    openlist_delete_policy?: string;
    openlist_strm_sign?: boolean;
  }) => request(panSettingsSchema, "/api/pan/settings", {
    method: "PUT", body: JSON.stringify(payload)
  }),
  saveOpenListCredentials: (payload: { token?: string; username?: string; password?: string }) =>
    request(z.object({
      openlist_username: z.string().nullable(),
      openlist_token_set: z.boolean(),
      openlist_password_set: z.boolean()
    }), "/api/pan/openlist/credentials", { method: "POST", body: JSON.stringify(payload) }),
  clearOpenListCredentials: async (): Promise<void> => {
    const path = "/api/pan/openlist/credentials";
    const response = await fetch(appUrl(path) ?? path, { method: "DELETE" });
    if (!response.ok) throw new Error(await response.text());
    await invalidateMutation(path);
  },
  testOpenList: () => request(openListTestSchema, "/api/pan/openlist/test", { method: "POST" }),
  openListFiles: (path = "/", page = 1) => {
    const query = new URLSearchParams({ path, page: String(page) });
    return request(openListFilesSchema, `/api/pan/openlist/files?${query}`);
  },
  strmRewrite: () =>
    request(z.object({ scanned: z.number(), rewritten: z.number(), skipped: z.number() }),
      "/api/pan/strm/rewrite", { method: "POST" }),
  strmReconcile: () =>
    request(z.object({ started: z.boolean() }).passthrough(), "/api/pan/strm/reconcile", { method: "POST" }),
  panOfflineTasks: (signal?: AbortSignal) => request(z.array(panOfflineTaskSchema), "/api/pan/offline/tasks", { signal }),
  panOfflineCancel: (taskId: string) =>
    request(panOfflineTaskSchema, `/api/pan/offline/tasks/${taskId}/cancel`, { method: "POST" }),
  panOfflineNext: (taskId: string) =>
    request(panOfflineTaskSchema, `/api/pan/offline/tasks/${taskId}/next`, { method: "POST" }),
  panPipeline: (signal?: AbortSignal) => request(panPipelineSchema, "/api/pan/pipeline", { signal }),
  panOfflineIntake: (payload: { url: string; work_id?: string; code?: string }) =>
    request(panOfflineTaskSchema, "/api/pan/offline/intake", {
      method: "POST", body: JSON.stringify(payload)
    }),
  strmStatus: () => request(z.record(z.string(), z.unknown()), "/api/pan/strm/status"),
  subscriptionWatchStatus: () =>
    request(subscriptionWatchStatusSchema, "/api/pan/subscription-watch/status"),
  runSubscriptionWatch: () =>
    request(subscriptionWatchStatusSchema, "/api/pan/subscription-watch/run", { method: "POST" }),
  submitWorkOffline: (workId: string, payload: { magnet_id?: string; url?: string }) =>
    request(panOfflineTaskSchema, `/api/works/${workId}/offline`, {
      method: "POST", body: JSON.stringify(payload)
    }),
  workOfflineTasks: (workId: string, signal?: AbortSignal) =>
    request(z.array(panOfflineTaskSchema), `/api/works/${workId}/offline`, { signal }),
  workEmbyLink: async (workId: string): Promise<string | null> => {
    const path = `/api/works/${workId}/emby-link`;
    const response = await fetch(appUrl(path) ?? path);
    if (!response.ok) throw new Error(await response.text());
    const payload = z.object({ url: z.string().nullable() }).parse(await response.json());
    return payload.url;
  },
  workPlayResolve: (workId: string, signal?: AbortSignal) =>
    request(
      z.object({
        work_id: z.string(),
        code: z.string().nullable().optional(),
        preferred: z.object({
            kind: z.string(),
            label: z.string(),
            url: z.string(),
            playable_in_app: z.boolean().optional(),
            app_scheme: z.string().nullable().optional(),
            item_id: z.string().nullable().optional(),
            provider: z.string().nullable().optional(),
            note: z.string().nullable().optional(),
            probe_ok: z.boolean().nullable().optional(),
            probe_reason: z.string().nullable().optional(),
          })
          .nullable()
          .optional(),
        chosen: z.object({
            kind: z.string(),
            label: z.string(),
            url: z.string(),
            playable_in_app: z.boolean().optional(),
            app_scheme: z.string().nullable().optional(),
            item_id: z.string().nullable().optional(),
            provider: z.string().nullable().optional(),
            note: z.string().nullable().optional(),
            probe_ok: z.boolean().nullable().optional(),
            probe_reason: z.string().nullable().optional(),
          })
          .nullable()
          .optional(),
        sources: z.array(
          z.object({
            kind: z.string(),
            label: z.string(),
            url: z.string(),
            playable_in_app: z.boolean().optional(),
            app_scheme: z.string().nullable().optional(),
            item_id: z.string().nullable().optional(),
            provider: z.string().nullable().optional(),
            note: z.string().nullable().optional(),
            probe_ok: z.boolean().nullable().optional(),
            probe_reason: z.string().nullable().optional(),
          }),
        ),
        failures: z
          .array(
            z.object({
              stage: z.string(),
              provider: z.string().nullable().optional(),
              reason: z.string(),
              detail: z.string().nullable().optional(),
              elapsed_ms: z.number().nullable().optional(),
            }),
          )
          .optional(),
        emby_in_library: z.boolean(),
        sites_loaded: z.boolean().optional(),
        external_site_count: z.number().optional(),
        vod_provider_count: z.number().optional(),
        resolve_ms: z.number().optional(),
      }),
      `/api/works/${workId}/play`,
      { signal },
    ),
  workPlayerUrl: (workId: string) => appUrl(`/api/works/${workId}/player`) ?? `/api/works/${workId}/player`,

  /** Tube swipefeed sidecar (proxied under /api/swipe). */
  swipeFeed: (params: {
    cursor?: string | number | Record<string, unknown> | null;
    limit?: number;
    category?: string;
    mode?: string;
  } = {}) => {
    const q = new URLSearchParams();
    q.set("mode", params.mode ?? "random");
    // Sidecar cursors are JSON objects (e.g. {page:1}) or strings; mirror swipefeed-web.
    if (params.cursor != null && params.cursor !== "") {
      q.set(
        "cursor",
        typeof params.cursor === "string" ? params.cursor : JSON.stringify(params.cursor),
      );
    }
    if (params.limit) q.set("limit", String(params.limit));
    if (params.category) q.set("category", params.category);
    const qs = q.toString();
    return request(
      z.object({
        items: z.array(z.record(z.string(), z.unknown())),
        nextCursor: z.union([z.string(), z.number(), z.record(z.string(), z.unknown())]).nullable().optional(),
        hasMore: z.boolean().optional(),
        error: z.string().optional(),
      }).passthrough(),
      `/api/swipe/feed${qs ? `?${qs}` : ""}`,
    );
  },
  swipeResolve: (payload: { site: string; id: string; pageUrl?: string }, signal?: AbortSignal) =>
    request(
      z
        .object({
          url: z.string().optional(),
          type: z.string().optional(),
          directUrl: z.string().optional(),
          qualities: z.array(z.record(z.string(), z.unknown())).optional(),
          error: z.string().optional(),
        })
        .passthrough(),
      "/api/swipe/resolve",
      { method: "POST", body: JSON.stringify(payload), signal },
    ),
  swipeSites: () =>
    request(z.array(z.record(z.string(), z.unknown())).or(z.record(z.string(), z.unknown())), "/api/swipe/sites"),
  swipeHealth: () =>
    request(
      z.object({
        ok: z.boolean(),
        upstream: z.string().nullable().optional(),
        status: z.number().optional(),
        error: z.string().optional(),
      }),
      "/api/swipe/health",
    ),
  /** Same-origin media proxy for external m3u8/mp4 (Referer/ORB). */
  swipeMediaProxyUrl: (rawUrl: string, opts: { type?: "hls" | "stream"; referer?: string } = {}) => {
    const kind = opts.type === "stream" || (!/\.m3u8/i.test(rawUrl) && opts.type !== "hls") ? "stream" : "hls";
    const q = new URLSearchParams({ u: rawUrl });
    if (opts.referer) q.set("r", opts.referer);
    return appUrl(`/api/swipe/${kind}?${q.toString()}`) ?? `/api/swipe/${kind}?${q.toString()}`;
  },
};
