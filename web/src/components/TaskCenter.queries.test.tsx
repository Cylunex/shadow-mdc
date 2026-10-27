import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { notifyManager } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { api } from "../api";
import { queryClient } from "../query/client";
import { TaskCenter } from "./TaskCenter";

vi.mock("../api", () => ({ api: { panOfflineTasks: vi.fn() } }));
vi.mock("../hooks/useTaskUpdates", () => ({ useTaskUpdates: () => true }));
let root: Root;
beforeEach(() => {
  queryClient.clear();
  notifyManager.setScheduler(queueMicrotask);
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  vi.mocked(api.panOfflineTasks).mockClear().mockResolvedValue([]);
  root = createRoot(document.createElement("div"));
});
afterEach(async () => {
  await act(async () => root.unmount());
  queryClient.clear();
  vi.unstubAllGlobals();
});

it("does not reload offline tasks every time the event stream sends a new task snapshot", async () => {
  for (let progress = 0; progress < 5; progress++) {
    await act(async () => root.render(<TaskCenter tasks={[{
      id: "task", kind: "scan", scope: "fixture", status: "running", summary: { progress },
      error: null, created_at: "", finished_at: null,
    }]} busy={null} onChanged={async () => undefined} report={() => undefined} />));
  }
  expect(api.panOfflineTasks).toHaveBeenCalledTimes(1);
});
