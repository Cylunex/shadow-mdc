import { act } from "react";
import { createRoot } from "react-dom/client";
import { notifyManager } from "@tanstack/react-query";
import { expect, it, vi } from "vitest";
import { api } from "../api";
import { queryClient } from "../query/client";
import { OfflineDownloadDialog } from "./OfflineDownloadDialog";

vi.mock("../api", () => ({ api: { panStatus: vi.fn(), openListFiles: vi.fn() } }));

it("browses paginated folders and submits a per-task directory without a default", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  notifyManager.setScheduler(queueMicrotask);
  const showModal = Object.getOwnPropertyDescriptor(HTMLDialogElement.prototype, "showModal");
  Object.defineProperty(HTMLDialogElement.prototype, "showModal", {
    configurable: true,
    value: function (this: HTMLDialogElement) { this.open = true; },
  });
  vi.mocked(api.panStatus).mockResolvedValue({
    provider: "openlist", backend: "openlist", connected: true, offline_target: null,
    configured: true, available: true, reason: "",
  });
  vi.mocked(api.openListFiles).mockImplementation(async (path = "/", page = 1) => ({
    path, page,
    total: path === "/" ? 101 : 0,
    items: path === "/" && page === 2
      ? [{ name: "归档", path: "/归档", is_directory: true }]
      : [],
  }));
  const host = document.createElement("div");
  document.body.append(host);
  const root = createRoot(host);
  const submit = vi.fn(async () => undefined);
  const onClose = vi.fn();
  const button = (name: string) => Array.from(host.querySelectorAll("button"))
    .find((item) => item.textContent === name)!;
  try {
    await act(async () => root.render(<OfflineDownloadDialog name="Fixture" submit={submit} onClose={onClose} />));
    expect(button("提交离线下载").disabled).toBe(true);
    await act(async () => button("浏览目录").click());
    expect(button("选用此目录")).toBeUndefined();
    await act(async () => button("下一页").click());
    await act(async () => button("归档").click());
    await act(async () => button("选用此目录").click());
    expect(host.querySelector("input")?.value).toBe("/归档");
    await act(async () => {
      button("提交离线下载").click();
      button("提交离线下载").click();
    });
    expect(submit).toHaveBeenCalledExactlyOnceWith("/归档");
    expect(onClose).toHaveBeenCalledTimes(1);
  } finally {
    await act(async () => root.unmount());
    host.remove();
    queryClient.clear();
    notifyManager.setScheduler((callback) => setTimeout(callback, 0));
    if (showModal) Object.defineProperty(HTMLDialogElement.prototype, "showModal", showModal);
    else Reflect.deleteProperty(HTMLDialogElement.prototype, "showModal");
    vi.unstubAllGlobals();
  }
});
