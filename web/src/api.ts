import { z } from "zod";

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
  screenshotGenerateSchema,
  taskRunsSchema,
  workDetailSchema,
  workSchema,
  workLookupSchema,
  worksSchema,
  collectionsSchema,
  collectionSchema,
  collectionSeedResultSchema
} from "./model";
import type { FilterWords, IdentityAliases } from "./model";

export type DisplayMediaCategory = "Japan" | "China" | "Korea" | "Europe" | "Other";

export type NonJavActorEditPayload = {
  name: string;
  aliases: string[];
  groups: string[];
  categories: DisplayMediaCategory[];
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
  const payload: unknown = await response.json();
  return schema.parse(payload);
}

export const api = {
  libraries: () => request(librariesSchema, "/api/libraries"),
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
  scan: (libraryId: string) =>
    request(scanSchema, `/api/libraries/${libraryId}/scan`, { method: "POST" }),
  generateScreenshots: (libraryId: string, limit = 50) =>
    request(screenshotGenerateSchema, `/api/libraries/${libraryId}/screenshots`, {
      method: "POST",
      body: JSON.stringify({ limit })
    }),
  identifyLibrary: (libraryId: string, limit = 20) =>
    request(bulkIdentifySchema, `/api/libraries/${libraryId}/identify`, {
      method: "POST",
      body: JSON.stringify({ limit })
    }),
  tasks: () => request(taskRunsSchema, "/api/tasks"),
  assets: () => request(assetInboxListSchema, "/api/inbox"),
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
  works: (params?: { collection_id?: string; collection_kind?: string; collection?: string }) => {
    const query = new URLSearchParams();
    if (params?.collection_id) query.set("collection_id", params.collection_id);
    if (params?.collection_kind) query.set("collection_kind", params.collection_kind);
    if (params?.collection) query.set("collection", params.collection);
    const suffix = query.toString() ? `?${query}` : "";
    return request(worksSchema, `/api/works${suffix}`);
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
  workDetail: (workId: string) => request(workDetailSchema, `/api/works/${workId}`),
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
  actors: () => request(actorProfilesSchema, "/api/actors"),
  nonJavActors: () => request(nonJavActorsSchema, "/api/non-jav-actors"),
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
  lookupWork: (code: string) => request(workLookupSchema, "/api/works/lookup", {
    method: "POST",
    body: JSON.stringify({ code })
  }),
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
    return catalogImportResultSchema.parse(await response.json());
  }
};
