import { QueryClient } from "@tanstack/react-query";

export const queryKeys = {
  all: ["mdc"] as const,
  works: ["mdc", "works"] as const,
  actors: ["mdc", "actors"] as const,
  nonJavActors: ["mdc", "non-jav-actors"] as const,
  libraries: ["mdc", "libraries"] as const,
  prefs: ["mdc", "preferences"] as const,
  tasks: ["mdc", "tasks"] as const,
  offlineList: ["mdc", "offline-list"] as const,
  panStatus: ["mdc", "pan-status"] as const,
  pipeline: ["mdc", "pipeline"] as const,
  inbox: ["mdc", "inbox"] as const,
  detail: (id: string) => ["mdc", "detail", id] as const,
  related: (id: string) => ["mdc", "related", id] as const,
  offline: (id: string) => ["mdc", "offline", id] as const,
  categories: (onlyWithWorks: boolean) => ["mdc", "categories", onlyWithWorks] as const,
  actorWorks: (ref: string) => ["mdc", "actor-works", ref] as const,
};

/** One client for imperative readers and React observers; no disk persistence. */
export const queryClient = new QueryClient({
  defaultOptions: { queries: {
    networkMode: "always",
    retry: false,
    staleTime: 30_000,
    gcTime: 5 * 60_000,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
    // Parsed large library responses need not be recursively compared with old rows.
    structuralSharing: false,
  } },
});

const libraryGroups = new Set(["works", "actors", "non-jav-actors", "detail", "related", "categories", "actor-works", "inbox"]);
async function invalidateGroups(groups: ReadonlySet<string>): Promise<void> {
  const filters = { predicate: (query: { queryKey: readonly unknown[] }) =>
    query.queryKey[0] === "mdc" && groups.has(String(query.queryKey[1])) };
  // Cancel old reads before refetching: a response begun before a write must not win.
  await queryClient.cancelQueries(filters);
  void queryClient.invalidateQueries(filters);
}
export const invalidateLibrary = (): Promise<void> => invalidateGroups(libraryGroups);

/** Successful API mutations share the same invalidation path as background tasks. */
export async function invalidateMutation(path: string): Promise<void> {
  // Playback resolution and diagnostics use POST but do not change library data.
  if (path.startsWith("/api/swipe/") || path === "/api/pan/openlist/test") return;
  if (path.startsWith("/api/library-prefs")) {
    await queryClient.cancelQueries({ queryKey: queryKeys.prefs });
    void queryClient.invalidateQueries({ queryKey: queryKeys.prefs });
    return;
  }
  if (path.startsWith("/api/tasks")) {
    await invalidateGroups(new Set(["tasks"]));
    return;
  }
  if (path.startsWith("/api/providers") || path.endsWith("/organize/plan")) return;
  if (path.startsWith("/api/settings") && !path.endsWith("/identity-aliases")) return;
  if (path.startsWith("/api/pan")) {
    await invalidateGroups(new Set(["offline", "offline-list", "pan-status", "pipeline"]));
    return;
  }
  if (/^\/api\/works\/[^/]+\/offline$/.test(path)) {
    await invalidateGroups(new Set(["offline", "offline-list", "pipeline"]));
    return;
  }
  await invalidateLibrary();
  void queryClient.invalidateQueries({ queryKey: queryKeys.libraries });
  void queryClient.invalidateQueries({ queryKey: queryKeys.tasks });
}

// Time-based GC alone can retain many distinct details during fast browsing.
queryClient.getQueryCache().subscribe((event) => {
  if (event.type !== "updated") return;
  const inactive = queryClient.getQueryCache().getAll()
    .filter((query) => query.getObserversCount() === 0 && query.state.fetchStatus === "idle")
    .sort((a, b) => a.state.dataUpdatedAt - b.state.dataUpdatedAt);
  for (const query of inactive.slice(0, Math.max(0, inactive.length - 96))) {
    queryClient.removeQueries({ queryKey: query.queryKey, exact: true });
  }
});
