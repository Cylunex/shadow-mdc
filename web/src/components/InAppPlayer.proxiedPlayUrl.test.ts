import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../api", () => ({
  api: {
    swipeMediaProxyUrl: (raw: string, opts: { type?: string; referer?: string } = {}) =>
      `./api/swipe/${opts.type === "hls" ? "hls" : "stream"}?u=${encodeURIComponent(raw)}`,
  },
  appUrl: (path: string | null) => (path === null || !path.startsWith("/") ? path : `.${path}`),
}));

import { proxiedPlayUrl, type PlayerSource } from "./InAppPlayer";

function source(url: string, extra: Partial<PlayerSource> = {}): PlayerSource {
  return { kind: "tube", label: "t", url, playable_in_app: true, ...extra };
}

describe("proxiedPlayUrl", () => {
  const originalLocation = window.location;

  beforeEach(() => {
    Object.defineProperty(window, "location", {
      configurable: true,
      value: {
        href: "https://life.example/mdc/swipe/",
        origin: "https://life.example",
        pathname: "/mdc/swipe/",
      },
    });
  });

  afterEach(() => {
    Object.defineProperty(window, "location", {
      configurable: true,
      value: originalLocation,
    });
  });

  it("rewrites absolute /api/swipe paths through the SPA base", () => {
    const out = proxiedPlayUrl(source("/api/swipe/hls?u=https%3A%2F%2Fcdn.example%2Fa.m3u8"));
    expect(out).toBe("./api/swipe/hls?u=https%3A%2F%2Fcdn.example%2Fa.m3u8");
  });

  it("leaves already-prefixed /mdc/api/swipe alone when same-origin", () => {
    const raw = "/mdc/api/swipe/stream?u=https%3A%2F%2Fcdn.example%2Fa.mp4";
    expect(proxiedPlayUrl(source(raw))).toBe(raw);
  });

  it("honors skip_proxy", () => {
    const raw = "/api/swipe/hls?u=x";
    expect(proxiedPlayUrl(source(raw, { skip_proxy: true }))).toBe(raw);
  });
});
