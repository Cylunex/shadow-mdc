import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "./api";
import { InAppPlayer } from "./components/InAppPlayer";
import { copyToClipboard } from "./lib/clipboard";
import { libraryPrefsSchema } from "./model";
import { queryClient } from "./query/client";
import { memoryStorage } from "./test/storage";
import { SubscriptionsView } from "./views/SubscriptionsView";

vi.mock("./api", () => ({
  appUrl: (path: string) => path,
  api: {
    libraryPrefs: vi.fn(),
    subscriptionWatchStatus: vi.fn(),
    upsertSubscription: vi.fn(),
    patchQueueItem: vi.fn(),
    swipeMediaProxyUrl: vi.fn((url: string) => url),
  },
}));

function setInputValue(input: HTMLInputElement, value: string) {
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
  setter?.call(input, value);
  input.dispatchEvent(new Event("input", { bubbles: true }));
  input.dispatchEvent(new Event("change", { bubbles: true }));
}

describe("frontend UX optimizations", () => {
  let root: Root;
  let container: HTMLDivElement;

  beforeEach(() => {
    queryClient.clear();
    vi.clearAllMocks();
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
    vi.stubGlobal("localStorage", memoryStorage());
    vi.stubGlobal("sessionStorage", memoryStorage());
    vi.mocked(api.libraryPrefs).mockResolvedValue(libraryPrefsSchema.parse({}));
    vi.mocked(api.subscriptionWatchStatus).mockResolvedValue({
      enabled: false,
      last_check_at: null,
      last_targets: 0,
      last_refreshed: 0,
      last_submitted: 0,
      last_skipped_pan: 0,
      last_errors: [],
      last_hunting: 0,
      last_queued: 0,
      last_due: 0,
      batch_cursor: 0,
      batch_total: 0,
      draining: false,
    });
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    queryClient.clear();
    container.remove();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("falls back to execCommand copy over LAN HTTP when navigator.clipboard is unavailable", async () => {
    Object.defineProperty(navigator, "clipboard", { value: undefined, configurable: true });
    const execCommand = vi.fn(() => true);
    Object.defineProperty(document, "execCommand", { value: execCommand, configurable: true });

    const ok = await copyToClipboard("SONE-123");
    expect(ok).toBe(true);
    expect(execCommand).toHaveBeenCalledWith("copy");
  });

  it("starts modal InAppPlayer unmuted, falls back to muted on autoplay block, and toggles subtitles", async () => {
    let playCalls = 0;
    const playSpy = vi.spyOn(HTMLMediaElement.prototype, "play").mockImplementation(function (this: HTMLMediaElement) {
      playCalls += 1;
      if (playCalls === 1) {
        return Promise.reject(new DOMException("Autoplay blocked", "NotAllowedError"));
      }
      return Promise.resolve();
    });
    vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => undefined);
    vi.spyOn(HTMLMediaElement.prototype, "load").mockImplementation(() => undefined);
    const onClose = vi.fn();

    await act(async () => {
      root.render(
        <InAppPlayer
          title="Fixture Video"
          playbackKey="work-1"
          onClose={onClose}
          sources={[
            {
              kind: "local",
              label: "Local MP4",
              url: "/api/works/work-1/assets/asset-1/media",
              mime_type: "video/mp4",
              playable_in_app: true,
              subtitle_url: "/api/works/work-1/assets/asset-1/media/subtitle.vtt",
            },
          ]}
        />,
      );
    });

    await vi.waitFor(() => expect(playSpy).toHaveBeenCalledTimes(2));
    const video = container.querySelector("video")!;
    expect(video.muted).toBe(true);

    const subToggle = [...container.querySelectorAll("button")].find((btn) => btn.textContent?.includes("字幕"));
    expect(subToggle?.textContent).toBe("隐藏字幕");
    expect(container.querySelector("track")).not.toBeNull();

    await act(async () => subToggle!.click());
    expect(subToggle?.textContent).toBe("显示字幕");
    expect(container.querySelector("track")).toBeNull();

    await act(async () => {
      window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("restores the selected queue status when returning from a work", async () => {
    const prefs = libraryPrefsSchema.parse({ queue: ["pending", "accepted"].map(status => ({
      id: status, title: status, actor_key: "actor", actor_name: "演员", status, created_at: "",
    })) });
    const render = () => root.render(<SubscriptionsView prefs={prefs} busy={null} report={vi.fn()} onChanged={vi.fn()} />);
    await act(async () => render());
    await act(async () => [...container.querySelectorAll("button")].find(button => button.textContent?.includes("影片队列"))!.click());
    expect(container.querySelector(".queue-card h2")?.textContent).toBe("pending");
    const filter = container.querySelector("select")!;
    await act(async () => { filter.value = "accepted"; filter.dispatchEvent(new Event("change", { bubbles: true })); });
    await act(async () => root.render(null));
    await act(async () => render());
    expect(container.querySelector("select")?.value).toBe("accepted");
    expect(container.querySelector(".queue-card h2")?.textContent).toBe("accepted");
  });

  it("syncs full multi-character actorName into actorKey until manually edited and opens queued work detail", async () => {
    const onOpenWork = vi.fn();
    const prefs = libraryPrefsSchema.parse({
      queue: [
        {
          id: "q-1",
          actor_key: "河北彩伽",
          actor_name: "河北彩伽",
          work_id: "work-99",
          code: "SONE-099",
          title: "Queued Fixture",
          release_date: "2026-01-01",
          cast_count: 1,
          status: "pending",
          created_at: "2026-01-02T00:00:00Z",
        },
      ],
    });
    vi.mocked(api.libraryPrefs).mockResolvedValue(prefs);
    vi.mocked(api.upsertSubscription).mockImplementation(async (sub) =>
      libraryPrefsSchema.parse({
        subscriptions: [
          {
            ...sub,
            created_at: "2026-01-02T00:00:00Z",
            updated_at: "2026-01-02T00:00:00Z",
          },
        ],
      }),
    );

    await act(async () => {
      root.render(
        <SubscriptionsView
          prefs={prefs}
          busy={null}
          report={() => undefined}
          onChanged={() => undefined}
          onOpenWork={onOpenWork}
        />,
      );
    });

    const nameInput = container.querySelector<HTMLInputElement>('input[placeholder="演员姓名"]')!;
    const keyInput = container.querySelector<HTMLInputElement>('input[placeholder="稳定键（留空默认同姓名）"]')!;

    await act(async () => setInputValue(nameInput, "河"));
    await act(async () => setInputValue(nameInput, "河北彩伽"));
    expect(keyInput.value).toBe("河北彩伽");

    await act(async () => setInputValue(keyInput, "saika-kawakita"));
    await act(async () => setInputValue(nameInput, "河北彩花"));
    expect(keyInput.value).toBe("saika-kawakita");

    const queueTab = [...container.querySelectorAll("button")].find((btn) => btn.textContent?.includes("影片队列"))!;
    await act(async () => queueTab.click());

    const detailBtn = [...container.querySelectorAll("button")].find((btn) => btn.textContent === "详情")!;
    await act(async () => detailBtn.click());
    expect(onOpenWork).toHaveBeenCalledWith("work-99");
  });
});
