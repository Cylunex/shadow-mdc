import { z } from "zod";

export const librarySchema = z.object({
  id: z.string(),
  name: z.string(),
  root_path: z.string(),
  recursive: z.boolean(),
  organize_template: z.string(),
  created_at: z.string()
});
export const librariesSchema = z.array(librarySchema);

export const artworkSchema = z.object({
  url: z.string(),
  kind: z.string(),
  width: z.number().nullable().optional(),
  height: z.number().nullable().optional()
});

export const hintsSchema = z.object({
  term: z.string(),
  mode: z.string(),
  family: z.string(),
  code: z.string().nullable(),
  title: z.string().nullable(),
  source_url: z.string().nullable(),
  external_ids: z.record(z.string(), z.string()),
  fingerprints: z.record(z.string(), z.string()),
  duration_seconds: z.number().nullable(),
  file_path: z.string().nullable(),
  media_locator: z.string().nullable(),
  studio: z.string().nullable(),
  series: z.string().nullable(),
  actors: z.array(z.string()),
  alias_evidence: z.array(z.string())
});

export const identityAliasesSchema = z.object({
  studios: z.record(z.string(), z.string()),
  series: z.record(z.string(), z.string()),
  actors: z.record(z.string(), z.string())
});

export const assetSchema = z.object({
  id: z.string(),
  library_id: z.string(),
  work_id: z.string().nullable(),
  path: z.string(),
  size: z.number(),
  duration_seconds: z.number().nullable(),
  oshash: z.string().nullable(),
  state: z.string(),
  hints: hintsSchema,
  error: z.string().nullable(),
  created_at: z.string(),
  updated_at: z.string()
});
export const assetsSchema = z.array(assetSchema);

export const providerRecordSchema = z.object({
  provider: z.string(),
  external_id: z.string(),
  source_url: z.string().nullable(),
  code: z.string().nullable(),
  title: z.string(),
  original_title: z.string().nullable(),
  family: z.string(),
  release_date: z.string().nullable(),
  runtime_seconds: z.number().nullable(),
  studio: z.string().nullable(),
  label: z.string().nullable(),
  series: z.string().nullable(),
  plot: z.string().nullable(),
  actors: z.array(z.string()),
  directors: z.array(z.string()),
  tags: z.array(z.string()),
  artwork: z.array(artworkSchema),
  fingerprints: z.record(z.string(), z.string()),
  language: z.string().nullable()
});

export const candidateSchema = z.object({
  id: z.string(),
  asset_id: z.string(),
  provider: z.string(),
  external_id: z.string(),
  score: z.number(),
  decision: z.string(),
  state: z.string(),
  record: providerRecordSchema,
  evidence: z.array(z.record(z.string(), z.unknown())),
  created_at: z.string()
});
export const candidatesSchema = z.array(candidateSchema);

export const identitySchema = z.object({
  provider: z.string(),
  kind: z.string(),
  value: z.string(),
  source_url: z.string().nullable()
});

export const workSchema = z.object({
  id: z.string(),
  title: z.string(),
  original_title: z.string().nullable(),
  primary_code: z.string().nullable(),
  family: z.string(),
  release_date: z.string().nullable(),
  runtime_seconds: z.number().nullable(),
  studio: z.string().nullable(),
  label: z.string().nullable(),
  series: z.string().nullable(),
  plot: z.string().nullable(),
  actors: z.array(z.string()),
  directors: z.array(z.string()),
  tags: z.array(z.string()),
  artwork: z.array(z.record(z.string(), z.unknown())),
  identities: z.array(identitySchema),
  created_at: z.string(),
  updated_at: z.string()
});
export const worksSchema = z.array(workSchema);

export const providerListSchema = z.object({
  providers: z.array(z.object({
    id: z.string(),
    name: z.string(),
    query_modes: z.array(z.string()).or(z.set(z.string())),
    families: z.array(z.string()).or(z.set(z.string())),
    requirements: z.array(z.string()).or(z.set(z.string())),
    configured: z.boolean()
  }))
});

export const scanSchema = z.object({
  discovered: z.number(),
  updated: z.number(),
  filtered: z.number(),
  skipped: z.number(),
  errors: z.array(z.string())
});

export const filterWordsSchema = z.object({
  words: z.array(z.string())
});

export const identifySchema = z.object({
  asset_id: z.string(),
  candidate_ids: z.array(z.string()),
  accepted_work_id: z.string().nullable(),
  failures: z.array(z.object({
    provider: z.string(),
    reason: z.string(),
    detail: z.string()
  }))
});

export type Library = z.infer<typeof librarySchema>;
export type Asset = z.infer<typeof assetSchema>;
export type Candidate = z.infer<typeof candidateSchema>;
export type Work = z.infer<typeof workSchema>;
export type IdentityAliases = z.infer<typeof identityAliasesSchema>;
export type FilterWords = z.infer<typeof filterWordsSchema>;
