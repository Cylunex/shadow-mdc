import { useEffect, useRef, useState } from "react";

import { appUrl } from "../api";
import { taskRunsSchema, type TaskRun } from "../model";

type Options = {
  enabled: boolean;
  onChanged: () => Promise<void>;
  onTasksSnapshot?: (tasks: TaskRun[]) => void;
};

/** Keep one stream per mounted view; callback and snapshot changes do not reconnect it. */
export function useTaskUpdates({ enabled, onChanged, onTasksSnapshot }: Options): boolean {
  const [live, setLive] = useState(false);
  const callbacks = useRef({ onChanged, onTasksSnapshot });
  useEffect(() => { callbacks.current = { onChanged, onTasksSnapshot }; }, [onChanged, onTasksSnapshot]);

  useEffect(() => {
    setLive(false);
    if (!enabled) return;
    let disposed = false;
    let polling: number | undefined;
    let refreshing = false;
    const refresh = async () => {
      if (disposed || refreshing) return;
      refreshing = true;
      try {
        await callbacks.current.onChanged();
      } catch {
        // Keep retrying while the stream reconnects; request failures are transient.
      } finally {
        refreshing = false;
      }
    };
    const startPolling = () => {
      if (polling !== undefined) return;
      void refresh();
      polling = window.setInterval(() => { void refresh(); }, 2500);
    };
    const stopPolling = () => {
      if (polling !== undefined) window.clearInterval(polling);
      polling = undefined;
    };
    const url = appUrl("/api/tasks/events");
    const source = url && typeof EventSource !== "undefined" ? new EventSource(url) : null;
    if (source) {
      source.addEventListener("tasks", (event) => {
        if (disposed) return;
        try {
          const payload = taskRunsSchema.parse(JSON.parse((event as MessageEvent).data));
          setLive(true);
          stopPolling();
          if (callbacks.current.onTasksSnapshot) callbacks.current.onTasksSnapshot(payload);
          else void refresh();
        } catch {
          setLive(false);
          startPolling();
        }
      });
      source.onerror = () => {
        if (disposed) return;
        setLive(false);
        startPolling();
      };
    } else {
      startPolling();
    }
    return () => {
      disposed = true;
      stopPolling();
      source?.close();
    };
  }, [enabled]);
  return live;
}
