import { z } from "zod";

export const librarySchema = z.object({
  id: z.string(),
  name: z.string(),
  root_path: z.string(),
  category: z.enum(["Japan", "China", "Korea", "Europe", "Other"]),
  recursive: z.boolean(),
  recognition_scope: z.enum(["all", "jav_only"]),
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
  category: z.enum(["Japan", "China", "Korea", "Europe", "Other"]),
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

export const mediaTechnicalInfoSchema = z.object({
  duration_seconds: z.number().nullable(),
  container: z.string().nullable(),
  video_codec: z.string().nullable(),
  audio_codec: z.string().nullable(),
  width: z.number().nullable(),
  height: z.number().nullable(),
  frame_rate: z.number().nullable(),
  overall_bitrate: z.number().nullable(),
  video_bitrate: z.number().nullable(),
  audio_bitrate: z.number().nullable(),
  bit_depth: z.number().nullable(),
  audio_channels: z.number().nullable(),
  audio_sample_rate: z.number().nullable(),
  hdr_format: z.string().nullable(),
  quality_label: z.string().nullable()
});

export const assetSchema = z.object({
  id: z.string(),
  library_id: z.string(),
  work_id: z.string().nullable(),
  path: z.string(),
  size: z.number(),
  modified_ns: z.number(),
  duration_seconds: z.number().nullable(),
  media_info: mediaTechnicalInfoSchema,
  oshash: z.string().nullable(),
  state: z.string(),
  hints: hintsSchema,
  error: z.string().nullable(),
  created_at: z.string(),
  updated_at: z.string()
});
export const assetsSchema = z.array(assetSchema);

export const assetInboxSchema = z.object({
  id: z.string(),
  library_id: z.string(),
  path: z.string(),
  state: z.string(),
  hints: z.object({
    family: z.string(),
    category: z.enum(["Japan", "China", "Korea", "Europe", "Other"]),
    code: z.string().nullable(),
    title: z.string().nullable(),
    media_locator: z.string().nullable(),
    studio: z.string().nullable(),
    series: z.string().nullable(),
    actors: z.array(z.string())
  }),
  media_info: z.object({
    video_codec: z.string().nullable(),
    audio_codec: z.string().nullable(),
    hdr_format: z.string().nullable(),
    quality_label: z.string().nullable()
  })
});
export const assetInboxListSchema = z.array(assetInboxSchema);

export const providerRecordSchema = z.object({
  provider: z.string(),
  external_id: z.string(),
  source_url: z.string().nullable(),
  code: z.string().nullable(),
  title: z.string(),
  original_title: z.string().nullable(),
  family: z.string(),
  category: z.enum(["Japan", "China", "Korea", "Europe", "Other"]),
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

export const actorSummarySchema = z.object({
  id: z.string(),
  name: z.string(),
  image_url: z.string().nullable()
});

export const workSchema = z.object({
  id: z.string(),
  title: z.string(),
  original_title: z.string().nullable(),
  primary_code: z.string().nullable(),
  family: z.string(),
  category: z.enum(["Japan", "China", "Korea", "Europe", "Other"]),
  release_date: z.string().nullable(),
  runtime_seconds: z.number().nullable(),
  studio: z.string().nullable(),
  label: z.string().nullable(),
  series: z.string().nullable(),
  plot: z.string().nullable(),
  actors: z.array(z.string()),
  actor_entities: z.array(actorSummarySchema),
  directors: z.array(z.string()),
  tags: z.array(z.string()),
  artwork: z.array(z.record(z.string(), z.unknown())),
  image_url: z.string().nullable(),
  fanart_url: z.string().nullable(),
  field_sources: z.record(z.string(), z.string()),
  identities: z.array(identitySchema),
  created_at: z.string(),
  updated_at: z.string()
});
export const worksSchema = z.array(workSchema);

export const actorProfileSchema = z.object({
  id: z.string().nullable(),
  name: z.string(),
  aliases: z.array(z.string()),
  categories: z.array(z.string()),
  work_count: z.number(),
  works: z.array(z.object({
    id: z.string(),
    title: z.string(),
    code: z.string().nullable(),
    category: z.string(),
    image_url: z.string().nullable()
  })),
  image_url: z.string().nullable()
});
export const actorProfilesSchema = z.array(actorProfileSchema);

export const nonJavActorWorkSchema = z.object({
  id: z.string(),
  title: z.string(),
  code: z.string().nullable(),
  category: z.string(),
  studio: z.string().nullable().optional(),
  series: z.string().nullable().optional(),
  release_date: z.string().nullable().optional(),
  image_url: z.string().nullable()
});

export const nonJavActorSchema = z.object({
  name: z.string(),
  aliases: z.array(z.string()),
  groups: z.array(z.string()),
  categories: z.array(z.enum(["Japan", "China", "Korea", "Europe", "Other"])),
  match_names: z.array(z.string()),
  image_url: z.string().nullable(),
  biography: z.string().nullable(),
  notes: z.string().nullable(),
  work_count: z.number().default(0),
  works: z.array(nonJavActorWorkSchema).default([])
});
export const nonJavActorsSchema = z.array(nonJavActorSchema);

export const directoryActorAssignSchema = z.object({
  directory: z.string(),
  actor: z.string(),
  matched_assets: z.number(),
  cataloged: z.number(),
  skipped: z.number()
});

export const workLookupSchema = z.object({
  work: workSchema.nullable(),
  matched_records: z.number(),
  failures: z.array(z.object({
    provider: z.string(),
    reason: z.string(),
    detail: z.string()
  }))
});

export const bulkTranslateSchema = z.object({
  attempted: z.number(),
  translated: z.number(),
  skipped: z.number(),
  failed: z.number(),
  remaining: z.number(),
  errors: z.array(z.string())
});

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

export const providerDiagnoseSchema = z.object({
  code: z.string(),
  proxy_configured: z.boolean(),
  retries: z.number(),
  diagnostics: z.array(z.object({
    provider: z.string(),
    status: z.string(),
    records: z.number(),
    accepted: z.number(),
    reason: z.string().nullable(),
    detail: z.string().nullable()
  }))
});

export const operationSchema = z.object({
  kind: z.string(),
  source: z.string().nullable(),
  destination: z.string(),
  conflict: z.boolean(),
  detail: z.string().nullable()
});
export const planSchema = z.object({
  asset_id: z.string(),
  token: z.string(),
  operations: z.array(operationSchema)
});
export const batchPlanSchema = z.object({
  token: z.string(),
  asset_count: z.number(),
  operation_count: z.number(),
  conflict_count: z.number(),
  samples: z.array(planSchema),
  truncated: z.boolean()
});
export const batchApplySchema = z.object({
  token: z.string(),
  attempted: z.number(),
  succeeded: z.number(),
  failed: z.number(),
  errors: z.array(z.string())
});

export const artworkDownloadSchema = z.object({
  work_id: z.string(),
  downloaded: z.number(),
  cached: z.number(),
  failed: z.number(),
  errors: z.array(z.string())
});

export const screenshotGenerateSchema = z.object({
  attempted: z.number(),
  generated: z.number(),
  skipped_strm: z.number(),
  skipped_cached: z.number(),
  skipped_untrusted: z.number(),
  failed: z.number(),
  errors: z.array(z.string())
});

export const taskRunSchema = z.object({
  id: z.string(),
  kind: z.string(),
  scope: z.string(),
  status: z.string(),
  summary: z.record(z.string(), z.unknown()),
  error: z.string().nullable(),
  created_at: z.string(),
  finished_at: z.string().nullable()
});
export const taskRunsSchema = z.array(taskRunSchema);

export const scanSchema = z.object({
  discovered: z.number(),
  updated: z.number(),
  queued: z.number(),
  identified: z.number(),
  filtered: z.number(),
  skipped: z.number(),
  errors: z.array(z.string())
});

export const bulkIdentifySchema = z.object({
  queried_identities: z.number(),
  code_queries: z.number(),
  title_queries: z.number(),
  attempted_assets: z.number(),
  identified: z.number(),
  online_identified: z.number(),
  catalog_reused: z.number(),
  local_optimized: z.number(),
  unresolved: z.number(),
  provider_failures: z.number(),
  remaining_identities: z.number(),
  scope_skipped: z.number()
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
export type Asset = z.infer<typeof assetInboxSchema>;
export type Candidate = z.infer<typeof candidateSchema>;
export type Work = z.infer<typeof workSchema>;
export type ActorProfile = z.infer<typeof actorProfileSchema>;
export type NonJavActor = z.infer<typeof nonJavActorSchema>;
export type IdentityAliases = z.infer<typeof identityAliasesSchema>;
export type FilterWords = z.infer<typeof filterWordsSchema>;
export type BatchPlan = z.infer<typeof batchPlanSchema>;
export type TaskRun = z.infer<typeof taskRunSchema>;
