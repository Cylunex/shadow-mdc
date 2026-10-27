import { useCallback, useMemo } from "react";
import { useQuery } from "@tanstack/react-query";

import type { PanOfflineTask } from "../model";
import { queryClient, queryKeys } from "../query/client";
import { resources } from "../query/resources";

export function useWorkPage(workId: string) {
  const primary = useQuery(resources.detail(workId), queryClient);
  const preferences = useQuery(resources.prefs(), queryClient);
  const detail = useMemo(() => primary.data && preferences.data
    ? { ...primary.data, want_list: preferences.data.want_list.includes(workId) }
    : primary.data ?? null, [primary.data, preferences.data, workId]);
  // Optional expensive reads start after the main content can paint.
  const related = useQuery({ ...resources.related(workId), enabled: Boolean(primary.data) }, queryClient);
  const offline = useQuery({ ...resources.offline(workId), enabled: Boolean(primary.data) }, queryClient);
  const reload = useCallback(async (id: string) => {
    void queryClient.invalidateQueries({ queryKey: queryKeys.related(id) });
    void queryClient.invalidateQueries({ queryKey: queryKeys.offline(id) });
    try {
      await queryClient.fetchQuery({ ...resources.detail(id), staleTime: 0 });
    } catch {
      // The shared observer exposes the failure while keeping any existing data.
    }
  }, []);
  const setOfflineTasks = useCallback((next: PanOfflineTask[] | ((current: PanOfflineTask[]) => PanOfflineTask[])) => {
    queryClient.setQueryData<PanOfflineTask[]>(queryKeys.offline(workId), (current) =>
      typeof next === "function" ? next(current ?? []) : next);
  }, [workId]);

  return {
    detail,
    related: related.data ?? { by_actor: [], by_tag: [] },
    offlineTasks: offline.data ?? [],
    setOfflineTasks,
    loadError: primary.error?.message ?? null,
    loading: primary.isPending,
    reload,
  };
}
