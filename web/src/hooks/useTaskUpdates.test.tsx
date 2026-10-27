import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useTaskUpdates } from "./useTaskUpdates";

vi.mock("../api", () => ({ appUrl: (path: string) => path }));

class TaskSource extends EventTarget {
  static instances: TaskSource[] = [];
  onerror: (() => void) | null = null;
  close = vi.fn();
  constructor() {
    super();
    TaskSource.instances.push(this);
  }
  snapshot(data = "[]") { this.dispatchEvent(new MessageEvent("tasks", { data })); }
}

describe("task progress connection", () => {
  let root: Root;
  let container: HTMLDivElement;
  const changed = vi.fn<() => Promise<void>>();
  const snapshot = vi.fn();
  function Harness({ enabled = true, refresh = changed, receive = snapshot }) {
    const live = useTaskUpdates({ enabled, onChanged: refresh, onTasksSnapshot: receive });
    return <output>{live ? "live" : "polling"}</output>;
  }

  beforeEach(() => {
    vi.useFakeTimers();
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
    vi.stubGlobal("EventSource", TaskSource);
    vi.clearAllMocks();
    changed.mockResolvedValue(undefined);
    TaskSource.instances = [];
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
  });
  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("keeps a single connection across snapshots and callback changes", async () => {
    await act(async () => root.render(<Harness />));
    const source = TaskSource.instances[0]!;
    await act(async () => source.snapshot());
    const latest = vi.fn();
    await act(async () => root.render(<Harness receive={latest} />));
    await act(async () => source.snapshot());
    expect(TaskSource.instances).toHaveLength(1);
    expect(source.close).not.toHaveBeenCalled();
    expect(snapshot).toHaveBeenCalledTimes(1);
    expect(latest).toHaveBeenCalledTimes(1);
    expect(container.textContent).toBe("live");
  });

  it("polls on disconnect and stops polling after a valid snapshot", async () => {
    await act(async () => root.render(<Harness />));
    const source = TaskSource.instances[0]!;
    await act(async () => source.onerror?.());
    expect(changed).toHaveBeenCalledTimes(1);
    await act(async () => vi.advanceTimersByTimeAsync(2500));
    expect(changed).toHaveBeenCalledTimes(2);
    await act(async () => source.snapshot());
    await act(async () => vi.advanceTimersByTimeAsync(7500));
    expect(changed).toHaveBeenCalledTimes(2);
  });

  it("does not overlap slow fallback requests", async () => {
    let finish!: () => void;
    changed.mockReturnValue(new Promise<void>((resolve) => { finish = resolve; }));
    await act(async () => root.render(<Harness />));
    await act(async () => TaskSource.instances[0]!.onerror?.());
    await act(async () => vi.advanceTimersByTimeAsync(7500));
    expect(changed).toHaveBeenCalledTimes(1);
    await act(async () => finish());
    await act(async () => vi.advanceTimersByTimeAsync(2500));
    expect(changed).toHaveBeenCalledTimes(2);
  });

  it("falls back on invalid events and cleans up when disabled", async () => {
    await act(async () => root.render(<Harness />));
    const source = TaskSource.instances[0]!;
    await act(async () => source.snapshot("invalid json"));
    expect(changed).toHaveBeenCalledTimes(1);
    await act(async () => root.render(<Harness enabled={false} />));
    expect(source.close).toHaveBeenCalledOnce();
    await act(async () => {
      source.snapshot();
      await vi.advanceTimersByTimeAsync(5000);
    });
    expect(changed).toHaveBeenCalledTimes(1);
    expect(snapshot).not.toHaveBeenCalled();
  });

  it("polls when EventSource is unavailable even before the first task", async () => {
    vi.stubGlobal("EventSource", undefined);
    await act(async () => root.render(<Harness />));
    expect(changed).toHaveBeenCalledOnce();
    await act(async () => vi.advanceTimersByTimeAsync(2500));
    expect(changed).toHaveBeenCalledTimes(2);
  });
});
