import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { appUrl } from "../api";
import { InAppPlayer, type PlayerSource } from "../components/InAppPlayer";

type Site = {
  id: string; name: string; enabled: boolean; failure: string | null;
  active_revision: string | null; previous_revision: string | null; priority: number;
};
type Card = { site_id: string; id: string; title: string; image: boolean; overview: string };
type Episode = { id: string; title: string; line: string };
type Detail = Card & { episodes: Episode[] };
type Page = { items: Card[]; next_page: number | null; failures: string[] };
type Job = { id: string; result: { sites: Site[]; skipped: number; warnings: string[] } | null };

async function request<T>(path: string, method = "GET", body?: unknown): Promise<T> {
  const response = await fetch(appUrl(`/api/tvbox${path}`)!, {
    method, headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => null);
    throw new Error(typeof payload?.detail === "string" ? payload.detail : `请求失败（${response.status}）`);
  }
  return response.json() as Promise<T>;
}

function itemPath(card: Card) {
  return `/sites/${card.site_id}/items/${card.id}`;
}

export function TvBoxView() {
  const cache = useQueryClient();
  const [url, setUrl] = useState("");
  const [file, setFile] = useState<File>();
  const [allowHttp, setAllowHttp] = useState(false);
  const [jobId, setJobId] = useState("");
  const [siteId, setSiteId] = useState("");
  const [category, setCategory] = useState("");
  const [input, setInput] = useState("");
  const [query, setQuery] = useState("");
  const [page, setPage] = useState(1);
  const [card, setCard] = useState<Card>();
  const [play, setPlay] = useState<{ source: PlayerSource; episode: Episode; item: string }>();
  const [management, setManagement] = useState(false);

  const job = useQuery({
    queryKey: ["tvbox", "import", jobId], queryFn: () => request<Job>(`/imports/${jobId}`),
    enabled: Boolean(jobId), retry: false,
    refetchInterval: (state) => state.state.data?.result || state.state.error ? false : 900,
  });
  const importing = Boolean(jobId && !job.data?.result && !job.error);
  const completed = job.data?.result;
  useEffect(() => {
    if (completed) void cache.invalidateQueries({ queryKey: ["tvbox", "sites"] });
  }, [cache, completed]);
  const sites = useQuery({
    queryKey: ["tvbox", "sites"], queryFn: async () => (await request<{ items: Site[] }>("/sites")).items,
    refetchInterval: importing ? 1000 : false,
  });
  const active = (sites.data ?? []).filter((site) => site.enabled && site.active_revision);
  const playing = card && itemPath(card) === play?.item ? play : undefined;
  const effectiveSite = active.some((site) => site.id === siteId) ? siteId : active[0]?.id ?? "";
  const categories = useQuery({
    queryKey: ["tvbox", "categories", effectiveSite],
    queryFn: async () => (await request<{ items: { id: string; name: string }[] }>(
      `/sites/${effectiveSite}/categories`,
    )).items,
    enabled: Boolean(effectiveSite),
  });
  const items = useQuery({
    queryKey: ["tvbox", "items", effectiveSite, category, query, page],
    enabled: Boolean(effectiveSite),
    queryFn: () => {
      const params = new URLSearchParams({ page: String(page) });
      if (query) {
        params.set("q", query);
        return request<Page>(`/search?${params}`);
      }
      if (category) params.set("category", category);
      return request<Page>(`/sites/${effectiveSite}/items?${params}`);
    },
  });
  const detail = useQuery({
    queryKey: ["tvbox", "detail", card?.site_id, card?.id],
    enabled: Boolean(card),
    queryFn: () => request<Detail>(itemPath(card!)),
  });
  const upload = useMutation({
    mutationFn: async () => {
      if (file && file.size > 2 * 1024 * 1024) throw new Error("配置文件应小于 2 MiB");
      const source = file ? { content: await file.text() } : { url: url.trim() };
      return request<Job>("/imports", "POST", { ...source, allow_http: allowHttp });
    },
    onSuccess: async (value) => {
      setJobId(value.id);
      await cache.invalidateQueries({ queryKey: ["tvbox", "import", value.id] });
    },
  });
  const change = useMutation({
    mutationFn: ({ site, patch }: { site: Site; patch?: object }) => patch
      ? request(`/sites/${site.id}`, "PATCH", patch)
      : request(`/sites/${site.id}/refresh`, "POST"),
    onSuccess: () => cache.invalidateQueries({ queryKey: ["tvbox"] }),
  });
  async function resolve(episode: Episode) {
    const stream = await request<{ url: string; mime_type: string; headers: Record<string, string> }>(
      itemPath(card!) + "/resolve", "POST", { episode_id: episode.id },
    );
    return {
      label: episode.line || episode.title, url: stream.url, mime_type: stream.mime_type,
      kind: "vod", playable_in_app: true, skip_proxy: true,
    };
  }
  const start = useMutation({
    mutationFn: async (episode: Episode) => ({ source: await resolve(episode), episode, item: itemPath(card!) }),
    onSuccess: setPlay,
  });

  return <section className="panel tvbox-view">
    <h1>TVBox</h1>
    <form className="tvbox-form" onSubmit={(event) => { event.preventDefault(); upload.mutate(); }}>
      <label>配置地址 <input type="url" value={url} onChange={(event) => setUrl(event.target.value)} disabled={Boolean(file)} /></label>
      <label>或选择文件 <input type="file" accept=".json,.jsonc,.txt" onChange={(event) => setFile(event.target.files?.[0])} /></label>
      <label><input type="checkbox" checked={allowHttp} onChange={(event) => setAllowHttp(event.target.checked)} />允许此配置使用 HTTP</label>
      <button disabled={importing || upload.isPending || (!file && !url.trim())}>导入并测试</button>
    </form>
    {(importing || upload.isPending) && <p role="status">正在测试，已通过的站点可直接浏览</p>}
    {job.data?.result && <p role="status">
      {job.data.result.sites.filter((site) => !site.failure && site.enabled).length} 个站点通过测试
      {job.data.result.skipped > 0 && ` · ${job.data.result.skipped} 项格式不受支持`}
    </p>}
    {job.data?.result?.warnings.map((warning, index) => <p key={index}>{warning}</p>)}
    {job.data?.result?.sites.filter((site) => site.failure).map((site) => <p key={site.id}>
      {site.name} · {site.failure} <button disabled={change.isPending} onClick={() => change.mutate({ site })}>重试</button>
    </p>)}
    {[upload.error, job.error, change.error, sites.error, categories.error, items.error, detail.error, start.error]
      .filter(Boolean).map((error, index) => <p role="alert" key={index}>{error?.message}</p>)}
    <button onClick={() => setManagement(!management)}>{management ? "收起源管理" : "管理来源"}</button>
    {management && <ul>
      {(sites.data ?? []).map((site) => <li key={site.id}>
        <strong>{site.name}</strong> · {site.enabled ? (site.active_revision ? "可使用" : "等待可用修订") : "已停用"}
        {site.failure && ` · ${site.failure}`}
        <button disabled={change.isPending} onClick={() => change.mutate({ site })}>重新测试</button>
        <button disabled={change.isPending} onClick={() => change.mutate({ site, patch: { enabled: !site.enabled } })}>
          {site.enabled ? "停用" : "启用"}
        </button>
        <button disabled={change.isPending || !site.previous_revision} onClick={() => change.mutate({ site, patch: { rollback: true } })}>回滚</button>
        <button disabled={change.isPending || site.priority >= 1000} onClick={() => change.mutate({ site, patch: { priority: site.priority + 1 } })}>提高优先级</button>
      </li>)}
    </ul>}
    {card ? <>
      <button onClick={() => { setCard(undefined); setPlay(undefined); start.reset(); }}>返回目录</button>
      <h2>{card.title}</h2>
      <p>{detail.data?.overview}</p>
      {playing && <InAppPlayer title={card.title} playbackKey={`tvbox:${card.site_id}:${card.id}:${playing.episode.id}`}
        sources={[playing.source]} autoPlay onClose={() => setPlay(undefined)} onRefreshSource={() => resolve(playing.episode)} />}
      {detail.isFetching && <p>正在读取分集…</p>}
      {detail.data?.episodes.length === 0 && <p>暂无可用线路</p>}
      {detail.data?.episodes.map((episode) => <button key={episode.id} disabled={start.isPending}
        onClick={() => start.mutate(episode)}>{episode.title}{episode.line && ` · ${episode.line}`}</button>)}
    </> : active.length > 0 && <>
      <label>站点 <select value={effectiveSite} onChange={(event) => {
        setSiteId(event.target.value); setCategory(""); setQuery(""); setPage(1);
      }}>{active.map((site) => <option key={site.id} value={site.id}>{site.name}</option>)}</select></label>
      <label>分类 <select value={category} onChange={(event) => {
        setCategory(event.target.value); setQuery(""); setPage(1);
      }}><option value="">全部</option>
        {categories.data?.map((row) => <option key={row.id} value={row.id}>{row.name}</option>)}
      </select></label>
      <form onSubmit={(event) => { event.preventDefault(); setQuery(input.trim()); setPage(1); }}>
        <label>搜索全部站点 <input value={input} maxLength={200} onChange={(event) => setInput(event.target.value)} /></label>
        <button>搜索</button>
      </form>
      {items.isFetching && <p>正在读取影片…</p>}
      {items.data?.failures.length ? <p>部分站点未返回结果 · {items.data.failures.join("、")}</p> : null}
      {items.data?.items.length === 0 && <p>没有找到影片</p>}
      <ul className="tvbox-results">{items.data?.items.map((item) => <li key={`${item.site_id}:${item.id}`}>
        <button onClick={() => setCard(item)}>{item.title}</button>
        {query && active.find((site) => site.id === item.site_id)?.name}
      </li>)}</ul>
      <button disabled={page <= 1} onClick={() => setPage(page - 1)}>上一页</button>
      <button disabled={!items.data?.next_page} onClick={() => setPage(items.data!.next_page!)}>下一页</button>
    </>}
  </section>;
}
