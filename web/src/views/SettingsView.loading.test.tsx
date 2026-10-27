import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { api } from "../api";
import { queryClient } from "../query/client";
import { panSettingsSchema, panStatusSchema } from "../model";
import { SettingsView } from "./SettingsView";

vi.mock("../api", () => ({ api: {
  panStatus: vi.fn(), panSettings: vi.fn(), panOfflineTasks: vi.fn(), panPipeline: vi.fn(), mediaServer: vi.fn(),
} }));
vi.mock("../panels", () => ({
  AliasEditor: () => null, CatalogImportEditor: () => null, FilterWordsEditor: () => null,
  Libraries: () => null, ProviderDiagnostics: () => null,
}));
vi.mock("../components/FieldPrioritySettings", () => ({ FieldPrioritySettings: () => null }));
vi.mock("../components/IdentifyByUrlPanel", () => ({ IdentifyByUrlPanel: () => null }));

let root: Root;
let container: HTMLDivElement;
beforeEach(() => {
  queryClient.clear();
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  vi.clearAllMocks();
  container = document.createElement("div");
  root = createRoot(container);
});
afterEach(async () => {
  await act(async () => root.unmount());
  queryClient.clear();
  vi.unstubAllGlobals();
});

it("shows status and local settings while offline tasks and pipeline remain pending", async () => {
  vi.mocked(api.panStatus).mockResolvedValue(panStatusSchema.parse({
    provider: "fixture", configured: true, available: true, reason: "ready", account: { user_name: "fixture-user" },
  }));
  vi.mocked(api.panSettings).mockResolvedValue(panSettingsSchema.parse({
    offline_directory_id: null, strm_enabled: false, strm_output_root: null,
    strm_url_prefix: "/media", use_proxy: false, client_id: "fixture-client", client_secret_set: false,
  }));
  vi.mocked(api.panOfflineTasks).mockReturnValue(new Promise(() => undefined));
  vi.mocked(api.panPipeline).mockReturnValue(new Promise(() => undefined));
  vi.mocked(api.mediaServer).mockRejectedValue(new Error("not configured"));
  await act(async () => root.render(<SettingsView libraries={[]} busy={null} run={async () => undefined} report={() => undefined} />));
  expect(container.textContent).toContain("fixture-user");
  expect(container.textContent).toContain("fixture · 可用");
  expect([...container.querySelectorAll("input")].some((input) => input.value === "fixture-client")).toBe(true);
});
