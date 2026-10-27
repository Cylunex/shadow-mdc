import { useCallback, useEffect, useRef, useState } from "react";

import { api } from "../api";
import type { PanOfflineTask, WorkDetail, WorkRelated } from "../model";

export function useWorkPage(workId: string) {
  const [detail, setDetail] = useState<WorkDetail | null>(null);
  const [related, setRelated] = useState<WorkRelated>({ by_actor: [], by_tag: [] });
  const [offlineTasks, setOfflineTasks] = useState<PanOfflineTask[]>([]);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const active = useRef<AbortController | null>(null);

  const reload = useCallback(async (id: string) => {
    active.current?.abort();
    const controller = new AbortController();
    active.current = controller;
    const { signal } = controller;
    setLoading(true);
    setLoadError(null);
    setRelated({ by_actor: [], by_tag: [] });
    setOfflineTasks([]);

    // Primary content paints as soon as its own request completes.
    const primary = api.workDetail(id, signal);
    void api.workRelated(id, 18, signal).then((rows) => {
      if (!signal.aborted) setRelated(rows);
    }).catch(() => undefined);
    void api.workOfflineTasks(id, signal).then((rows) => {
      if (!signal.aborted) setOfflineTasks(rows);
    }).catch(() => undefined);
    try {
      const next = await primary;
      if (!signal.aborted) setDetail(next);
    } catch (error) {
      if (!signal.aborted) {
        setDetail(null);
        setLoadError(error instanceof Error ? error.message : String(error));
      }
    } finally {
      if (!signal.aborted) setLoading(false);
    }
  }, []);

  useEffect(() => {
    void reload(workId);
    return () => active.current?.abort();
  }, [workId, reload]);

  return { detail: detail?.id === workId ? detail : null, related, offlineTasks, setOfflineTasks, loadError, loading, reload };
}
