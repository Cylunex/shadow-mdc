import { queryOptions } from "@tanstack/react-query";

import { api } from "../api";
import { queryKeys } from "./client";

/** Resource identity and loader live together, shared by pages and prefetch callers. */
export const resources = {
  works: () => queryOptions({
    queryKey: queryKeys.works,
    queryFn: ({ signal }) => api.works(undefined, signal),
  }),
  actors: () => queryOptions({
    queryKey: queryKeys.actors,
    queryFn: ({ signal }) => api.actors(signal),
  }),
  nonJavActors: () => queryOptions({
    queryKey: queryKeys.nonJavActors,
    queryFn: ({ signal }) => api.nonJavActors(signal),
  }),
  libraries: () => queryOptions({
    queryKey: queryKeys.libraries,
    queryFn: ({ signal }) => api.libraries(signal),
  }),
  prefs: () => queryOptions({
    queryKey: queryKeys.prefs,
    queryFn: ({ signal }) => api.libraryPrefs(signal),
    staleTime: 5_000,
  }),
  tasks: () => queryOptions({
    queryKey: queryKeys.tasks,
    queryFn: ({ signal }) => api.tasks(signal),
    staleTime: 0,
  }),
  offlineList: () => queryOptions({
    queryKey: queryKeys.offlineList,
    queryFn: ({ signal }) => api.panOfflineTasks(signal),
    staleTime: 5_000,
  }),
  panStatus: () => queryOptions({
    queryKey: queryKeys.panStatus,
    queryFn: ({ signal }) => api.panStatus(signal),
    staleTime: 0,
  }),
  pipeline: () => queryOptions({
    queryKey: queryKeys.pipeline,
    queryFn: ({ signal }) => api.panPipeline(signal),
    staleTime: 0,
  }),
  inbox: () => queryOptions({
    queryKey: queryKeys.inbox,
    queryFn: ({ signal }) => api.assets(signal),
  }),
  detail: (id: string) => queryOptions({
    queryKey: queryKeys.detail(id),
    queryFn: ({ signal }) => api.workDetail(id, signal),
  }),
  related: (id: string) => queryOptions({
    queryKey: queryKeys.related(id),
    queryFn: ({ signal }) => api.workRelated(id, 18, signal),
  }),
  offline: (id: string) => queryOptions({
    queryKey: queryKeys.offline(id),
    queryFn: ({ signal }) => api.workOfflineTasks(id, signal),
    staleTime: 0,
  }),
  categories: (onlyWithWorks = false) => queryOptions({
    queryKey: queryKeys.categories(onlyWithWorks),
    queryFn: ({ signal }) => api.categories({ onlyWithWorks }, signal),
  }),
  actorWorks: (ref: string) => queryOptions({
    queryKey: queryKeys.actorWorks(ref),
    queryFn: ({ signal }) => api.actorWorks(ref, signal),
  }),
};
