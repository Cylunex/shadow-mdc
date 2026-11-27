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
let container: HTMLDivElement;
beforeEach(() => {
  queryClient.clear();
  notifyManager.setScheduler(queueMicrotask);
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  vi.mocked(api.panOfflineTasks).mockClear().mockResolvedValue([]);
  container = document.createElement("div");
  root = createRoot(container);
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

it("combines task type and status filters and clears empty results", async () => {
  const tasks = ([["scan", "failed"], ["scan", "succeeded"], ["translate", "running"]] as const).map(([kind, status], i) => ({
    id: String(i), kind, status, scope: "fixture", summary: {}, error: null, created_at: "", finished_at: null,
  }));
  await act(async () => root.render(<TaskCenter tasks={tasks} busy={null} onChanged={vi.fn()} report={vi.fn()} />));
  async function choose(label: string, value: string) {
    const field = container.querySelector<HTMLSelectElement>(`[aria-label="${label}"]`)!;
    await act(async () => { field.value = value; field.dispatchEvent(new Event("change", { bubbles: true })); });
  }
  await choose("按任务状态筛选", "failed");
  expect(container.querySelectorAll(".task-list article")).toHaveLength(1);
  expect(container.querySelector(".task-state")?.textContent).toBe("失败");
  await choose("按任务类型筛选", "translate");
  expect(container.querySelectorAll(".task-list article")).toHaveLength(0);
  await act(async () => [...container.querySelectorAll("button")].find(node => node.textContent === "清除筛选")!.click());
  expect(container.querySelectorAll(".task-list article")).toHaveLength(3);
});
