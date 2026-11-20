import { appUrl } from "../api";

export type TvBoxSite = {
  id: string; name: string; enabled: boolean; failure: string | null;
  hidden?: boolean; pinned?: boolean; active_revision: string | null; previous_revision: string | null; priority: number;
};
export type TvBoxCard = {
  site_id: string; id: string; title: string; image: boolean; overview: string;
  year?: string; remarks?: string;
};
export type TvBoxEpisode = { id: string; title: string; line: string };
export type TvBoxDetail = TvBoxCard & { episodes: TvBoxEpisode[] };
export type TvBoxGroup = { id: string; name: string; site_ids: string[] };
export type TvBoxPage = { items: TvBoxCard[]; next_page: number | null; failures: string[] };
export type TvBoxJob = {
  id: string;
  result: { sites: TvBoxSite[]; skipped: number; warnings: string[]; migrated?: Record<string, number> } | null;
};

export async function tvboxRequest<T>(path: string, method = "GET", body?: unknown, signal?: AbortSignal): Promise<T> {
  const response = await fetch(appUrl(`/api/tvbox${path}`)!, {
    method, signal,
    headers: body && !(body instanceof FormData) ? { "Content-Type": "application/json" } : undefined,
    body: body instanceof FormData ? body : body ? JSON.stringify(body) : undefined,
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => null);
    throw new Error(typeof payload?.detail === "string" ? payload.detail : `请求失败（${response.status}）`);
  }
  return response.json() as Promise<T>;
}

export function tvboxItemPath(card: TvBoxCard): string {
  return `/sites/${card.site_id}/items/${card.id}`;
}

export function tvboxCardKey(card: TvBoxCard): string {
  return `${card.site_id}:${card.id}`;
}
