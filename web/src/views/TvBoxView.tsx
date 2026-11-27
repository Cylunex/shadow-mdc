import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { appUrl } from "../api";
import { InAppPlayer, type PlayerSource } from "../components/InAppPlayer";
import { TvBoxManage } from "./TvBoxManage";
import {
  tvboxCardKey, tvboxItemPath, tvboxRequest,
  type TvBoxCard, type TvBoxDetail, type TvBoxEpisode, type TvBoxGroup, type TvBoxJob,
  type TvBoxPage, type TvBoxSite,
} from "./tvboxApi";

type History = { card: TvBoxCard; episode: TvBoxEpisode; at: number };
type Shelf = "browse" | "history" | "favorites";

function localList<T>(name: string): T[] {
  try {
    const value: unknown = JSON.parse(localStorage.getItem(`shadow-mdc:tvbox:${name}`) || "[]");
    return Array.isArray(value) ? value.slice(0, 100) : [];
  } catch {
    return [];
  }
}

function saveList<T>(name: string, value: T[]) {
  try { localStorage.setItem(`shadow-mdc:tvbox:${name}`, JSON.stringify(value.slice(0, 100))); }
  catch { /* Playback remains available if storage is disabled. */ }
}

function Poster({ card }: { card: TvBoxCard }) {
  const [failed, setFailed] = useState(false);
  return <div className="tvbox-poster">
    {card.image && !failed
      ? <img src={appUrl(`/api/tvbox${tvboxItemPath(card)}/image`)!} alt="" loading="lazy"
        onError={() => setFailed(true)} />
      : <span aria-hidden="true">▷</span>}
    {card.remarks && <span className="tvbox-poster-note">{card.remarks}</span>}
  </div>;
}

function FilmGrid({ cards, onSelect, subtitle }: {
  cards: TvBoxCard[]; onSelect: (card: TvBoxCard) => void; subtitle: (card: TvBoxCard) => string | undefined;
}) {
  return <ul className="tvbox-grid">
    {[...new Map(cards.map((card) => [tvboxCardKey(card), card])).values()].map((card) =>
      <li key={tvboxCardKey(card)}>
        <button className="tvbox-film" onClick={() => onSelect(card)}>
          <Poster card={card} />
          <strong>{card.title}</strong>
          <span className="muted">{[card.year, subtitle(card)].filter(Boolean).join(" · ")}</span>
        </button>
      </li>)}
  </ul>;
}

function TvBoxWatch({ card, history, favorite, onFavorite, onBack, onPlay }: {
  card: TvBoxCard; history?: History; favorite: boolean;
  onFavorite: () => void; onBack: () => void; onPlay: (episode: TvBoxEpisode) => void;
}) {
  const path = tvboxItemPath(card);
  const detail = useQuery({
    queryKey: ["tvbox", "detail", path],
    queryFn: ({ signal }) => tvboxRequest<TvBoxDetail>(path, "GET", undefined, signal),
  });
  const [line, setLine] = useState<string>();
  const [playing, setPlaying] = useState<{ episode: TvBoxEpisode; source: PlayerSource }>();
  const [autoNext, setAutoNext] = useState(true);
  const [error, setError] = useState("");
  const [pending, setPending] = useState<string>();
  const controller = useRef<AbortController | undefined>(undefined);
  useEffect(() => () => controller.current?.abort(), []);
  const episodes = detail.data?.episodes ?? [];
  const lines = [...new Set(episodes.map((episode) => episode.line))];
  const saved = episodes.find((episode) => episode.id === history?.episode.id);
  const chosenLine = line ?? saved?.line ?? lines[0] ?? "";
  const selected = episodes.filter((episode) => episode.line === chosenLine);
  const queue = playing ? episodes.filter((episode) => episode.line === playing.episode.line) : [];
  const index = queue.findIndex((episode) => episode.id === playing?.episode.id);

  async function resolve(episode: TvBoxEpisode, signal?: AbortSignal): Promise<PlayerSource> {
    const stream = await tvboxRequest<{ url: string; mime_type: string }>(
      path + "/watch", "POST", { episode_id: episode.id }, signal,
    );
    return {
      url: appUrl(stream.url)!, mime_type: stream.mime_type, label: episode.line || episode.title,
      source_id: episode.id, version_id: episode.id, kind: "vod", playable_in_app: true, skip_proxy: true,
    };
  }

  async function play(episode: TvBoxEpisode | undefined) {
    if (!episode) return;
    controller.current?.abort();
    const request = new AbortController();
    controller.current = request;
    setPending(episode.id);
    setError("");
    setLine(episode.line);
    try {
      const source = await resolve(episode, request.signal);
      if (!request.signal.aborted) setPlaying({ episode, source });
    } catch (problem) {
      if (!request.signal.aborted) setError(problem instanceof Error ? problem.message : "线路暂时无法播放");
    } finally {
      if (!request.signal.aborted) setPending(undefined);
    }
  }

  return <section className="tvbox-detail">
    <div className="tvbox-actions"><button type="button" className="ghost" onClick={onBack}>‹ 返回目录</button>
      <button type="button" className="ghost" aria-pressed={favorite} onClick={onFavorite}>
        {favorite ? "取消收藏" : "收藏"}</button></div>
    <header><h1>{detail.data?.title || card.title}</h1>
      <p className="muted">{[card.year, card.remarks].filter(Boolean).join(" · ")}</p></header>
    <div className="tvbox-watch-layout">
      <div className="tvbox-watch-main">
        {playing ? <div className="tvbox-screen">
          <InAppPlayer title={`${card.title} · ${playing.episode.title}`}
            playbackKey={`tvbox:${tvboxCardKey(card)}`} sources={[playing.source]}
            onClose={() => { controller.current?.abort(); setPending(undefined); setPlaying(undefined); }}
            onRefreshSource={() => resolve(playing.episode)}
            onPlaybackStarted={() => onPlay(playing.episode)}
            onEnded={() => { if (autoNext && queue[index + 1]) void play(queue[index + 1]); }} />
        </div> : <div className="tvbox-feature">
          <Poster card={card} />
          <div><h2>{card.title}</h2>
            {detail.isPending && <p role="status">正在读取分集…</p>}
            {selected.length > 0 && <button onClick={() => void play(saved?.line === chosenLine ? saved : selected[0])}
              disabled={Boolean(pending)}>{saved?.line === chosenLine ? "继续播放" : "立即播放"}</button>}
            {detail.data && episodes.length === 0 && <p>暂无可用线路</p>}</div>
        </div>}
        {detail.error && <p role="alert" className="tvbox-error">{detail.error.message} <button className="ghost"
          disabled={detail.isFetching} onClick={() => void detail.refetch()}>重试分集</button></p>}
        {error && <p role="alert" className="tvbox-error">{error}</p>}
        {detail.data?.overview && <p className="tvbox-overview">{detail.data.overview}</p>}
      </div>
      {lines.length > 0 && <aside className="tvbox-surface tvbox-episodes">
        <h2>线路与分集</h2>
        <label>播放线路<select value={chosenLine} onChange={(event) => {
          const next = event.target.value;
          setLine(next);
          if (playing) {
            const matches = episodes.filter((ep) => ep.line === next && ep.title === playing.episode.title);
            if (matches.length === 1) void play(matches[0]);
          }
        }}>{lines.map((name) => <option key={name} value={name}>{name || "默认线路"}</option>)}</select></label>
        <div className="tvbox-episode-list">{selected.map((episode) => <button key={episode.id}
          type="button" className="ghost" aria-current={episode.id === playing?.episode.id ? "true" : undefined}
          onClick={() => void play(episode)}>{episode.id === pending ? "加载中…" : episode.title}</button>)}</div>
        {playing && <div className="tvbox-actions">
          <button className="ghost" disabled={index < 1} onClick={() => void play(queue[index - 1])}>上一集</button>
          <button className="ghost" disabled={!queue[index + 1]} onClick={() => void play(queue[index + 1])}>下一集</button>
          <label><input type="checkbox" checked={autoNext} onChange={(event) => setAutoNext(event.target.checked)} /> 连播</label>
        </div>}
      </aside>}
    </div>
  </section>;
}

export function TvBoxView() {
  const cache = useQueryClient();
  const [url, setUrl] = useState("");
  const [file, setFile] = useState<File>();
  const [allowHttp, setAllowHttp] = useState(false);
  const [jobId, setJobId] = useState("");
  const [group, setGroup] = useState("");
  const [siteId, setSiteId] = useState("");
  const [category, setCategory] = useState("");
  const [input, setInput] = useState("");
  const [query, setQuery] = useState("");
  const [page, setPage] = useState(1);
  const [card, setCard] = useState<TvBoxCard>();
  const [shelf, setShelf] = useState<Shelf>("browse");
  const [management, setManagement] = useState(false);
  const [history, setHistory] = useState<History[]>(() => localList("history"));
  const [favorites, setFavorites] = useState<TvBoxCard[]>(() => localList("favorites"));
  const job = useQuery({
    queryKey: ["tvbox", "import", jobId], queryFn: ({ signal }) => tvboxRequest<TvBoxJob>(`/imports/${jobId}`, "GET", undefined, signal),
    enabled: Boolean(jobId), retry: false,
    refetchInterval: (state) => state.state.data?.result || state.state.error ? false : 900,
  });
  const importing = Boolean(jobId && !job.data?.result && !job.error);
  const completed = job.data?.result;
  useEffect(() => {
    if (completed) void cache.invalidateQueries({ queryKey: ["tvbox"] });
  }, [cache, completed]);
  const sites = useQuery({
    queryKey: ["tvbox", "sites"], queryFn: async ({ signal }) =>
      (await tvboxRequest<{ items: TvBoxSite[] }>("/sites", "GET", undefined, signal)).items,
    refetchInterval: importing ? 1000 : false,
  });
  const groups = useQuery({
    queryKey: ["tvbox", "groups"],
    queryFn: async ({ signal }) => (await tvboxRequest<{ items: TvBoxGroup[] }>("/groups", "GET", undefined, signal)).items,
  });
  const active = (sites.data ?? []).filter((site) => site.enabled && !site.hidden && site.active_revision);
  const groupRow = groups.data?.find((row) => row.id === group);
  const available = groupRow ? groupRow.site_ids.flatMap((id) => active.filter((site) => site.id === id)) : active;
  const effectiveSite = available.some((site) => site.id === siteId) ? siteId : available[0]?.id ?? "";
  const showManagement = management || (sites.isSuccess && !active.length);
  const categories = useQuery({
    queryKey: ["tvbox", "categories", effectiveSite],
    queryFn: async ({ signal }) => (await tvboxRequest<{ items: { id: string; name: string }[] }>(
      `/sites/${effectiveSite}/categories`, "GET", undefined, signal,
    )).items,
    enabled: Boolean(effectiveSite) && !query && shelf === "browse" && !card,
  });
  const items = useQuery({
    queryKey: ["tvbox", "items", effectiveSite, category, query, group, page],
    enabled: Boolean(effectiveSite) && shelf === "browse" && !card,
    queryFn: ({ signal }) => {
      const params = new URLSearchParams({ page: String(page) });
      if (query) {
        params.set("q", query);
        if (groupRow) params.set("group_id", groupRow.id);
        return tvboxRequest<TvBoxPage>(`/search?${params}`, "GET", undefined, signal);
      }
      if (category) params.set("category", category);
      return tvboxRequest<TvBoxPage>(`/sites/${effectiveSite}/items?${params}`, "GET", undefined, signal);
    },
  });
  const upload = useMutation({
    mutationFn: async () => {
      if (file && (/\.(tgz|tar\.gz)$/i.test(file.name) || file.size > 2 * 1024 * 1024)) {
        const body = new FormData();
        body.append("file", file);
        body.append("allow_http", String(allowHttp));
        return tvboxRequest<TvBoxJob>("/imports/relay", "POST", body);
      }
      const source = file ? { content: await file.text() } : { url: url.trim() };
      return tvboxRequest<TvBoxJob>("/imports", "POST", { ...source, allow_http: allowHttp });
    },
    onSuccess: async (value) => {
      setJobId(value.id);
      await cache.invalidateQueries({ queryKey: ["tvbox", "import", value.id] });
    },
  });
  const retry = useMutation({
    mutationFn: (site: TvBoxSite) => tvboxRequest(`/sites/${site.id}/refresh`, "POST"),
    onSuccess: () => cache.invalidateQueries({ queryKey: ["tvbox"] }),
  });
  function record(film: TvBoxCard, episode: TvBoxEpisode) {
    setHistory((rows) => {
      const next = [{ card: film, episode, at: Date.now() },
        ...rows.filter((entry) => tvboxCardKey(entry.card) !== tvboxCardKey(film))].slice(0, 100);
      saveList("history", next);
      return next;
    });
  }
  function toggleFavorite(film: TvBoxCard) {
    setFavorites((rows) => {
      const key = tvboxCardKey(film);
      const next = rows.some((row) => tvboxCardKey(row) === key) ? rows.filter((row) => tvboxCardKey(row) !== key) : [film, ...rows].slice(0, 100);
      saveList("favorites", next);
      return next;
    });
  }
  if (card) return <div className="tvbox-view">
    <TvBoxWatch key={tvboxCardKey(card)} card={card}
      favorite={favorites.some((entry) => tvboxCardKey(entry) === tvboxCardKey(card))}
      history={history.find((entry) => tvboxCardKey(entry.card) === tvboxCardKey(card))}
      onFavorite={() => toggleFavorite(card)} onPlay={(episode) => record(card, episode)} onBack={() => setCard(undefined)} />
  </div>;

  return <section className="tvbox-view">
    <header className="tvbox-header">
      <h1>影视仓</h1>
      <button type="button" className="ghost" aria-expanded={management}
        onClick={() => setManagement(!management)}>{management ? "返回观看" : "导入和管理片源"}</button>
    </header>
    {showManagement && <div className="tvbox-surface">
      <h2>导入片源</h2>
      <form className="tvbox-form" onSubmit={(event) => { event.preventDefault(); upload.mutate(); }}>
        <label>配置地址<input type="url" value={url} onChange={(event) => setUrl(event.target.value)} disabled={Boolean(file)} /></label>
        <label>或选择配置 / Relay 导出包<input type="file" accept=".json,.jsonc,.txt,.tar.gz,.tgz" onChange={(event) => setFile(event.target.files?.[0])} /></label>
        <label><input type="checkbox" checked={allowHttp} onChange={(event) => setAllowHttp(event.target.checked)} />允许 HTTP</label>
        <button disabled={importing || upload.isPending || (!file && !url.trim())}>导入并测试</button>
      </form>
    </div>}
    {(importing || upload.isPending) && <p role="status">正在测试，已通过的站点可直接观看</p>}
    {completed && <p role="status">
      {completed.sites.filter((site) => !site.failure && site.enabled).length} 个站点通过测试
      {completed.skipped > 0 && ` · ${completed.skipped} 项格式不受支持`}
    </p>}
    {completed?.migrated?.sources ? <p role="status">
      已迁移 {completed.migrated.sources} 个 Relay 源包，保留 {completed.migrated.revisions ?? 0} 个修订
    </p> : null}
    {completed?.warnings.map((warning, index) => <p key={index}>{warning}</p>)}
    {completed && <ul className="tvbox-manage-list">
      {completed.sites.map((result) => sites.data?.find((site) => site.id === result.id) ?? result)
        .filter((site) => site.failure).map((site) => <li key={site.id}>
          <span>{site.name} · {site.failure}</span>
          <button type="button" className="ghost" disabled={retry.isPending} onClick={() => retry.mutate(site)}>重试</button>
        </li>)}
    </ul>}
    {([[sites, "站点"], [groups, "仓库"], [categories, "分类"], [items, "影片"]] as const).map(([resource, label]) =>
      resource.isError && resource.isEnabled && <p role="alert" className="tvbox-error" key={label}>{resource.error.message} <button
        type="button" className="ghost" disabled={resource.isFetching} onClick={() => void resource.refetch()}>重试{label}</button></p>)}
    {[upload.error, job.error, retry.error]
      .filter(Boolean).map((problem, index) => <p role="alert" className="tvbox-error" key={index}>{problem?.message}</p>)}
    {management && <TvBoxManage sites={sites.data ?? []} groups={groups.data ?? []} onImport={setJobId} allowHttp={allowHttp} />}
    {sites.isPending && <p role="status">正在读取影视站点…</p>}
    {active.length > 0 && !management && <>
      <div className="tvbox-toolbar">
        <div className="tvbox-actions" aria-label="影视目录">
          {(["browse", "history", "favorites"] as const).map((value) => <button type="button" className="ghost"
            aria-pressed={value === shelf} key={value} onClick={() => setShelf(value)}>
            {{ browse: "浏览", history: "继续观看", favorites: "收藏" }[value]}</button>)}
        </div>
        <form className="tvbox-search" onSubmit={(event) => {
          event.preventDefault(); setQuery(input.trim()); setPage(1); setShelf("browse");
        }}>
          <input value={input} maxLength={200} placeholder="搜电影、电视剧" aria-label="搜索影视" onChange={(event) => setInput(event.target.value)} />
          <button>搜索</button>
          {query && <button type="button" className="ghost" onClick={() => { setInput(""); setQuery(""); setPage(1); }}>清除</button>}
        </form>
      </div>
      {shelf === "browse" ? <>
        <div className="tvbox-filters">
          {(groups.data?.length ?? 0) > 0 && <label>仓库 <select value={group} onChange={(event) => {
            setGroup(event.target.value); setSiteId(""); setCategory(""); setPage(1);
          }}><option value="">全部仓库</option>{groups.data?.map((row) => <option key={row.id} value={row.id}>{row.name}</option>)}</select></label>}
          {!query && available.length > 0 && <label>站点 <select value={effectiveSite} onChange={(event) => {
            setSiteId(event.target.value); setCategory(""); setPage(1);
          }}>{available.map((site) => <option key={site.id} value={site.id}>{site.name}</option>)}</select></label>}
        </div>
        {!query && categories.data && <div className="tvbox-categories" aria-label="影视分类">
          {[{ id: "", name: "最近更新" }, ...categories.data].map((row) => <button className="ghost"
            key={row.id} aria-pressed={category === row.id} onClick={() => { setCategory(row.id); setPage(1); }}>{row.name}</button>)}
        </div>}
        {items.isFetching && <p role="status">正在读取影片…</p>}
        {available.length === 0 && <p>这个仓库暂无可用站点。</p>}
        {!!items.data?.failures.length && <p className="muted">部分站点未返回结果 · {items.data.failures.join("、")}</p>}
        {items.data?.items.length === 0 && <p>没有找到影片</p>}
        <FilmGrid cards={items.data?.items ?? []} onSelect={setCard}
          subtitle={(film) => query ? active.find((site) => site.id === film.site_id)?.name : undefined} />
        {items.data && <div className="tvbox-actions tvbox-pages">
          <button className="ghost" disabled={page <= 1} onClick={() => setPage(page - 1)}>上一页</button>
          <span>第 {page} 页</span>
          <button className="ghost" disabled={!items.data.next_page} onClick={() => {
            if (items.data.next_page) setPage(items.data.next_page);
          }}>下一页</button>
        </div>}
      </> : <>
        <FilmGrid cards={shelf === "history" ? history.map((entry) => entry.card) : favorites}
          onSelect={setCard} subtitle={(film) => shelf === "history"
            ? history.find((entry) => tvboxCardKey(entry.card) === tvboxCardKey(film))?.episode.title : undefined} />
        {(shelf === "history" ? history : favorites).length === 0 && <p>{shelf === "history" ? "暂无观看记录" : "暂无收藏"}</p>}
      </>}
    </>}
  </section>;
}
