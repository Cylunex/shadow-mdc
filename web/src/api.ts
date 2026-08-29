import { z } from "zod";

import {
  assetsSchema,
  candidateSchema,
  candidatesSchema,
  filterWordsSchema,
  identifySchema,
  identityAliasesSchema,
  librariesSchema,
  librarySchema,
  providerListSchema,
  scanSchema,
  workSchema,
  worksSchema
} from "./model";
import type { FilterWords, IdentityAliases } from "./model";

async function request<T>(schema: z.ZodType<T>, path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
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
  createLibrary: (payload: { name: string; root_path: string; category: string }) =>
    request(librarySchema, "/api/libraries", { method: "POST", body: JSON.stringify(payload) }),
  scan: (libraryId: string) =>
    request(scanSchema, `/api/libraries/${libraryId}/scan`, { method: "POST" }),
  assets: () => request(assetsSchema, "/api/assets"),
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
  works: () => request(worksSchema, "/api/works"),
  refreshWork: (workId: string) =>
    request(identifySchema, `/api/works/${workId}/refresh`, { method: "POST" }),
  providers: () => request(providerListSchema, "/api/providers"),
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
    })
};
