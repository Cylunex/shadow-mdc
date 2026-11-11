import { useEffect, useRef, useState } from "react";
import { z } from "zod";
import { api, appUrl } from "../api";
import { applyCatalogFiltersToUrl, EMPTY_CATALOG_FILTERS, filtersSchema, readCatalogFilters, type Filters } from "../lib/catalogFilters";
import {
  CatalogPeopleEditor, CatalogOrganizations, CatalogSeriesEditor, CatalogPlaylists, PersonPicker, catalogRequest,
  usePeopleCards, usePersonLabels, type ManagedPerson
} from "./CatalogManagement";
import { workSchema } from "../model";
import { WorkCard } from "../components/WorkCard";
import { CatalogLibraryTools, CatalogPersonActions, useCatalogActions } from "./CatalogActions";
import { CoverImage } from "../components/CoverImage";
import { CatalogTags, matchesTag } from "./CatalogTags";
import { Pagination } from "../components/Pagination";
import { useSessionState } from "../hooks/useSessionState";
import { CLASSIFICATION_KEYS, CLASSIFICATION_NAMES, migrateSavedClassifications } from "../lib/classifications";
import { queryClient } from "../query/client";
import { resources } from "../query/resources";
import { TaxonomyEditor } from "./TaxonomyEditor";
import { TagManagement } from "./TagManagement";
import { DURATION_OPTIONS, EMPTY_DURATION, durationLabel, durationOption } from "../lib/catalogDuration";
import { ClassificationManagement } from "./ClassificationManagement";

type Card = { id: string; title: string; year: number | null; actors: string[]; tags: string[]; poster: string; studio: string | null; code?: string | null; category?: string; release_date?: string | null; has_local_media?: boolean; rating_value?: number | null; rating_max?: number | null; javranking_rank?: number | null; javranking_score?: number | null };
type Person = ManagedPerson;
type Facet = { id: string; name: string; count: number; role: string; aliases?: string[]; group?: string };
export type { Filters } from "../lib/catalogFilters";
export type CatalogFeedStart = { filters: Filters; items: Card[]; nextCursor: string | null; index?: number };
const empty = EMPTY_CATALOG_FILTERS;
const actorBrowsingSchema = z.object({
  search: z.string().max(300), sort: z.enum(["works", "name"]), followed: z.boolean(),
  page: z.number().int().min(1), pageSize: z.union([z.literal(24), z.literal(48), z.literal(96)]),
});
const actorDefaults: z.infer<typeof actorBrowsingSchema> = { search: "", sort: "works", followed: false, page: 1, pageSize: 48 };
async function request<T>(path: string, body?: unknown, signal?: AbortSignal): Promise<T> {
  const response = await fetch(appUrl("/api/catalog/" + path)!, { signal, ...(body ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {}) });
  if (!response.ok) throw new Error("读取失败（" + response.status + "）");
  return response.json() as Promise<T>;
}
export function UnifiedCatalogView({ mode, onOpenWork, onOpenActor, initialPersonRef, onStartFeed, refresh = 0 }: { mode: "works" | "actors"; initialPersonRef?: string; refresh?: number; onStartFeed?: (state: CatalogFeedStart) => void; onOpenWork: (id: string) => void; onOpenActor: (id: string) => void }) {
  const [filters, setFilters] = useState<Filters>(() => readCatalogFilters(window.location.search));
  const workRequest = useRef<AbortController | null>(null);
  const initialFilters = useRef(filters);
  const loadedFilters = useRef<Filters | null>(null);
  const [rows, setRows] = useState<Card[]>([]);
  const [facets, setFacets] = useState<Record<string, Facet[]>>({});
  const [cursor, setCursor] = useState<string | null>(null);
  const [total, setTotal] = useState(0);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [workError, setWorkError] = useState("");
  const [appendFailed, setAppendFailed] = useState(false);
  const [facetError, setFacetError] = useState("");
  const [showFilters, setShowFilters] = useState(false);
  const [showMore, setShowMore] = useState(false);
  const [actorBrowsing, setActorBrowsing] = useSessionState("mdc:actor-browsing", actorBrowsingSchema, actorDefaults);
  const actorHeading = useRef<HTMLDivElement>(null);
  const actorOffset = (actorBrowsing.page - 1) * actorBrowsing.pageSize;
  const activePersonRef = initialPersonRef;
  const [resolvedRef, setResolvedRef] = useState("");
  const { page: people, error: peopleError, loading: peopleLoading, retry: retryPeople } =
    usePeopleCards(actorBrowsing.search, actorBrowsing.sort, actorBrowsing.followed, actorOffset,
      mode === "actors" && !initialPersonRef, actorBrowsing.pageSize);
  useEffect(() => {
    if (!people) return;
    const lastPage = Math.max(1, Math.ceil(people.total / actorBrowsing.pageSize));
    if (actorBrowsing.page > lastPage) setActorBrowsing(previous => ({ ...previous, page: lastPage }));
  }, [people, actorBrowsing.page, actorBrowsing.pageSize, setActorBrowsing]);
  const actorPagination = {
    label: "演员", page: actorBrowsing.page, pageSize: actorBrowsing.pageSize, total: people?.total ?? 0,
    busy: peopleLoading,
    onPageChange: (page: number) => {
      setActorBrowsing(previous => ({ ...previous, page }));
      actorHeading.current?.scrollIntoView?.({ block: "start" });
    },
    onPageSizeChange: (pageSize: number) => setActorBrowsing(previous => ({ ...previous, page: 1, pageSize: pageSize as 24 | 48 | 96 })),
  };
  const actorNames = usePersonLabels(filters.actors);
  const [selectedPerson, setSelectedPerson] = useState<Person | null>(null);
  const [saved, setSaved] = useState<{ name: string; filters: Filters }[]>(() => {
    try { return JSON.parse(localStorage.getItem("mdc:saved-filters") ?? "[]"); } catch { return []; }
  });
  const [migratingSaved, setMigratingSaved] = useState(saved.length > 0);
  useEffect(() => {
    let active = true;
    async function migrate() {
      try {
        if (!saved.length || localStorage.getItem("mdc:saved-classification-version") === "1") return;
        const schema = await queryClient.fetchQuery(resources.classifications());
        if (!active) return;
        const migrated = await migrateSavedClassifications(saved, schema.dimensions,
          (dimension, name) => api.createClassification(dimension, name, []), schema.legacy_terms);
        if (active) {
          localStorage.setItem("mdc:saved-filters", JSON.stringify(migrated));
          localStorage.setItem("mdc:saved-classification-version", "1");
          setSaved(migrated);
        }
      } catch { if (active) setError("已保存的分类筛选迁移失败，原数据已保留。重新打开页面可重试。"); }
      finally { if (active) setMigratingSaved(false); }
    }
    void migrate();
    return () => { active = false; };
  }, []);
  const [editing, setEditing] = useState<Card | null>(null);
  const [revision, setRevision] = useState(0);
  const actions = useCatalogActions(() => setRevision(value => value + 1));
  const showingWorks = mode !== "actors" || Boolean(initialPersonRef);
  const [saveName, setSaveName] = useState("");
  const [workRetry, setWorkRetry] = useState(0);
  const [facetRetry, setFacetRetry] = useState(0);
  const patch = (value: Partial<Filters>) => setFilters(previous => ({ ...previous, ...value }));
  useEffect(() => {
    if (!activePersonRef) return;
    const controller = new AbortController();
    catalogRequest<Person>("people/" + encodeURIComponent(activePersonRef), "GET", undefined, controller.signal)
      .then(person => {
        if (controller.signal.aborted) return;
        setSelectedPerson(person);
        setFilters(previous => {
          const mapped = [...new Set(previous.actors.map(id => id === activePersonRef ? person.id : id))];
          const actors = mapped.includes(person.id) ? mapped : [person.id];
          const unchanged = actors.length === previous.actors.length
            && actors.every((id, index) => id === previous.actors[index]);
          return unchanged && !previous.actor_name ? previous : { ...previous, actors, actor_name: "" };
        });
        setResolvedRef(activePersonRef);
      }).catch(e => { if (!controller.signal.aborted) setError(String(e)); });
    return () => controller.abort();
  }, [activePersonRef, revision, refresh]);
  useEffect(() => { if (!showingWorks) return; try {
      const url = new URL(window.location.href);
      applyCatalogFiltersToUrl(url, filters);
      if (url.href !== window.location.href) history.replaceState(history.state, "", url);
    } catch { /* Navigation can remain usable if history updates are blocked. */ } }, [filters, showingWorks]);
  useEffect(() => {
    const restore = () => setFilters(readCatalogFilters(window.location.search));
    window.addEventListener("popstate", restore);
    return () => window.removeEventListener("popstate", restore);
  }, []);
  const waitingForPerson = Boolean(initialPersonRef && initialPersonRef !== resolvedRef);
  useEffect(() => {
    if (waitingForPerson || !showingWorks) { setLoading(false); return; }
    const controller = new AbortController();
    workRequest.current = controller;
    const { signal } = controller;
    setWorkError(""); setAppendFailed(false);
    let snapshot: { filters: Filters; rows: Card[]; cursor: string | null; total: number; scroll: number; at: number } | null = null;
    try { snapshot = JSON.parse(sessionStorage.getItem("mdc:catalog-return") ?? "null"); } catch { /* Optional cache. */ }
    if (snapshot && Date.now() - snapshot.at < 300000 && JSON.stringify(snapshot.filters) === JSON.stringify(filters)) {
      loadedFilters.current = filters;
      setRows(snapshot.rows); setCursor(snapshot.cursor); setTotal(snapshot.total); setLoading(false);
      const position = snapshot.scroll;
      const frame = requestAnimationFrame(() => window.scrollTo(0, position));
      return () => { controller.abort(); cancelAnimationFrame(frame); };
    }
    setLoading(true);
    if (loadedFilters.current !== filters) {
      setRows([]); setCursor(null); setTotal(0);
    }
    function loadWorks() {
      request<{ items: Card[]; next_cursor: string | null; total: number }>("works/query", filters, signal)
        .then(page => {
          if (signal.aborted) return;
          loadedFilters.current = filters;
          setRows(page.items); setCursor(page.next_cursor); setTotal(page.total);
        }).catch(e => { if (!signal.aborted) setWorkError(String(e)); })
        .finally(() => { if (!signal.aborted) setLoading(false); });
    }
    if (filters === initialFilters.current) {
      loadWorks();
      return () => controller.abort();
    }
    const timer = setTimeout(loadWorks, 180);
    return () => { clearTimeout(timer); controller.abort(); };
  }, [filters, revision, refresh, waitingForPerson, showingWorks, workRetry]);
  useEffect(() => {
    if (!showingWorks || waitingForPerson) return;
    const controller = new AbortController();
    setFacetError("");
    const load = () => {
      void request<Record<string, Facet[]>>("facets", filters, controller.signal)
        .then(values => { if (!controller.signal.aborted) setFacets(values); })
        .catch(e => { if (!controller.signal.aborted) setFacetError("分类统计读取失败 · " + String(e)); });
    };
    if (filters === initialFilters.current) {
      load();
      return () => controller.abort();
    }
    const timer = setTimeout(load, 180);
    return () => { clearTimeout(timer); controller.abort(); };
  }, [facetRetry, filters, revision, refresh, showingWorks, waitingForPerson]);
  const loadingMore = useRef(false);
  async function more() {
    const signal = workRequest.current?.signal;
    if (!cursor || loading || loadingMore.current || !signal || signal.aborted) return;
    loadingMore.current = true;
    setLoading(true); setWorkError(""); setAppendFailed(true);
    try {
      const page = await request<{ items: Card[]; next_cursor: string | null }>("works/query", { ...filters, cursor }, signal);
      if (signal.aborted) return;
      setRows(previous => [...new Map([...previous, ...page.items].map(row => [row.id, row])).values()]);
      setCursor(page.next_cursor);
    } catch (e) { if (!signal.aborted) setWorkError(String(e)); }
    finally { loadingMore.current = false; if (!signal.aborted) setLoading(false); }
  }
  function openWork(id: string) {
    try {
      sessionStorage.setItem("mdc:catalog-return", JSON.stringify({
        filters, rows, cursor, total, scroll: window.scrollY, at: Date.now()
      }));
    } catch { /* Navigation remains available without storage. */ }
    onOpenWork(id);
  }
  function save() {
    if (!saveName.trim()) return;
    const next = [...saved.filter(item => item.name !== saveName.trim()), { name: saveName.trim(), filters }];
    try { localStorage.setItem("mdc:saved-filters", JSON.stringify(next)); setSaved(next); setSaveName(""); } catch { setError("无法保存本机筛选"); }
  }
  return <section className="panel-section unified-catalog">
    <div className="catalog-heading">
    <div><h1>{mode === "actors" ? selectedPerson?.name ?? "演员库" : "作品库"}</h1>
    <p className="muted">{showingWorks ? `${total} 部作品` : people ? `${people.total} 位演员 · 查看档案与参演作品` : "演员档案与参演作品"}</p></div>
    {showingWorks && onStartFeed && <button disabled={rows.length === 0 || loading} onClick={() => {
      try { sessionStorage.setItem("mdc:catalog-return", JSON.stringify({
        filters, rows, cursor, total, scroll: window.scrollY, at: Date.now()
      })); } catch { /* Storage is optional. */ }
      onStartFeed({ filters, items: rows, nextCursor: cursor, index: 0 });
    }}>按当前筛选刷片</button>}
    </div>

    {actions.message && <p role="status">{actions.message}</p>}
    {error && <p role="alert">{error}</p>}
    {workError && <div className="catalog-error" role="alert"><span>{workError}</span>
      <button className="ghost" disabled={loading} onClick={() => appendFailed ? void more() : setWorkRetry(value => value + 1)}>重试作品</button></div>}
    {facetError && <div className="catalog-error" role="alert"><span>{facetError}</span>
      <button className="ghost" onClick={() => setFacetRetry(value => value + 1)}>重试统计</button></div>}
    {mode === "actors" && <>
      {!initialPersonRef && <>
        <div className="catalog-people-search" ref={actorHeading}>
        <select aria-label="演员排序" value={actorBrowsing.sort} onChange={e => setActorBrowsing(previous => ({ ...previous, sort: e.target.value as "works" | "name", page: 1 }))}>
          <option value="works">库内作品数</option><option value="name">姓名</option>
        </select>
        <label><input type="checkbox" checked={actorBrowsing.followed}
          onChange={e => setActorBrowsing(previous => ({ ...previous, followed: e.target.checked, page: 1 }))} />仅已关注</label>
        <input id="shadow-mdc-search" aria-label="搜索演员与别名" placeholder="演员、别名" value={actorBrowsing.search} maxLength={300}
          onChange={e => setActorBrowsing(previous => ({ ...previous, search: e.target.value, page: 1 }))} />
        {(actorBrowsing.search || actorBrowsing.followed) && <button className="ghost"
          onClick={() => setActorBrowsing(previous => ({ ...previous, search: "", followed: false, page: 1 }))}>清除演员筛选</button>}
        </div>
        {peopleLoading && <p role="status">正在读取人物…</p>}
        {peopleError && <div className="catalog-error" role="alert"><span>{peopleError.message}</span>
          <button className="ghost" disabled={peopleLoading} onClick={() => void retryPeople()}>重试演员</button></div>}
        {people && <Pagination {...actorPagination} />}
        {!peopleLoading && people?.items.length === 0 && <div className="empty"><h2>没有匹配的演员</h2>
          <p>试试姓名或别名，或关闭“仅已关注”。</p></div>}
        <div className="actor-grid" aria-label="演员列表" aria-busy={peopleLoading}>{people?.items.map(p =>
          <button key={p.id} className="actor-card" onClick={() => onOpenActor(p.id)}>
            <CoverImage src={appUrl(p.image_url) ?? ""} /><strong>{p.name}</strong>
            {p.same_name && <span>档案 {p.id.slice(-6)}</span>}
            {p.aliases.length > 0 && <span className="actor-card-aliases" title={p.aliases.join(" / ")}>{p.aliases.join(" / ")}</span>}
            <span className="actor-card-count">{p.work_count} 部作品</span>
          </button>)}</div>
        {people && people.total > actorBrowsing.pageSize && <Pagination {...actorPagination} position="底部" />}
      </>}
      {selectedPerson && <article><h2>{selectedPerson.name}</h2><p>{selectedPerson.biography}</p>
        {selectedPerson.x_handle && <a href={"https://x.com/" + encodeURIComponent(selectedPerson.x_handle)}
          target="_blank" rel="noreferrer">X · {selectedPerson.x_handle}</a>}
        <CatalogPersonActions person={selectedPerson} actions={actions} />
        <p>常合作演员</p>{selectedPerson.collaborators?.map(other => <button key={other.id}
          onClick={() => patch({ actors: [selectedPerson.id, other.id], actors_all: true })}>{other.name} · {other.count}</button>)}
        <p>合作机构</p>{selectedPerson.organizations?.map(other => <button key={other.id}
          onClick={() => patch({ actors: [selectedPerson.id], organizations: [other.id] })}>{other.name} · {other.count}</button>)}
        <p>参演系列</p>{selectedPerson.series?.map(other => <button key={other.id}
          onClick={() => patch({ actors: [selectedPerson.id], series: [other.id] })}>{other.name} · {other.count}</button>)}
      </article>}
      <CatalogPeopleEditor key={selectedPerson?.id} person={selectedPerson} onSaved={() => {
        try { sessionStorage.removeItem("mdc:catalog-return"); } catch { /* Storage is optional. */ }
        setRevision(value => value + 1);
      }} />
    </>}
    {showingWorks && <>
    <div className="catalog-search">
      <input id="shadow-mdc-search" value={filters.q} maxLength={200} aria-label="检索作品" placeholder="片名、番号、演员、片商" onChange={e => patch({ q: e.target.value })} />
      <button className="ghost" aria-expanded={showFilters} onClick={() => setShowFilters(value => !value)}>{showFilters ? "收起筛选" : "分类与筛选"}</button>
    </div>
    {showFilters && <div className="works-extra-filters">
      <label>系列<select aria-label="筛选系列" value={filters.series[0] ?? ""} onChange={e => patch({ series: e.target.value ? [e.target.value] : [], collection: "", series_name: "" })}><option value="">全部系列</option>{(facets.series ?? []).map(f => <option key={f.id} value={f.id}>{f.name} · {f.count}</option>)}</select></label>
      <select aria-label="年代" value={filters.unknown_year ? "unknown" : filters.year_from && filters.year_to === filters.year_from + 9 ? String(filters.year_from) : ""} onChange={e => patch(e.target.value === "unknown" ? { unknown_year: true, year_from: undefined, year_to: undefined } : { unknown_year: false, year_from: e.target.value ? Number(e.target.value) : undefined, year_to: e.target.value ? Number(e.target.value) + 9 : undefined })}>
        <option value="">全部年代</option>{(facets.decades ?? []).map(f => <option key={f.id} value={f.id}>{f.id === "unknown" ? "年份待补" : f.name + " 年代"} · {f.count}</option>)}
      </select>
      <select aria-label="发行年份" value={filters.year_from === filters.year_to ? String(filters.year_from ?? "") : ""} onChange={e => patch({ unknown_year: false, year_from: e.target.value ? Number(e.target.value) : undefined, year_to: e.target.value ? Number(e.target.value) : undefined })}><option value="">全部年份</option>{(facets.years ?? []).filter(f => f.id !== "unknown").map(f => <option key={f.id} value={f.id}>{f.name} · {f.count}</option>)}</select>
      <label>制作机构<select aria-label="筛选制作机构" value={filters.organizations[0] ?? ""} onChange={e => patch({ organizations: e.target.value ? [e.target.value] : [], organization_role: "" })}><option value="">全部片商及工作室</option>{(facets.organizations ?? []).map(f => <option key={f.id} value={f.id}>{f.name} · {f.role === "studio" ? "工作室" : "片商 / 公司"} · {f.count}</option>)}</select></label>
      {(["labels", "platforms"] as const).map(field => <label key={field}>{field === "labels" ? "厂牌" : "发布平台"}<select
        aria-label={field === "labels" ? "筛选厂牌" : "筛选发布平台"} value={filters[field][0] ?? ""}
        onChange={e => patch({ [field]: e.target.value ? [e.target.value] : [] })}>
        <option value="">{field === "labels" ? "全部厂牌" : "全部发布平台"}</option>{(facets[field] ?? []).map(f => <option key={f.id} value={f.id}>{f.name} · {f.count}</option>)}
      </select></label>)}
      <label>时长<select aria-label="筛选时长" value={durationOption(filters)?.id ?? (filters.duration_min !== undefined || filters.duration_max !== undefined ? "custom" : "")}
        onChange={event => patch({ ...EMPTY_DURATION, ...DURATION_OPTIONS.find(option => option.id === event.target.value)?.filters })}>
        <option value="">全部时长</option><option value="custom" hidden>自定义时长</option>{DURATION_OPTIONS.map(option => <option key={option.id} value={option.id}>
          {option.name} · {facets.durations?.find(facet => facet.id === option.id)?.count ?? 0}</option>)}
      </select></label>
      <button className="ghost" aria-expanded={showMore} onClick={() => setShowMore(value => !value)}>{showMore ? "收起更多筛选" : "更多筛选"}</button>
      {CLASSIFICATION_KEYS.filter(field => field !== "types" || showMore).map(field => <label key={field}>{CLASSIFICATION_NAMES[field]}<select
        aria-label={`筛选${CLASSIFICATION_NAMES[field]}`} value=""
        onChange={event => { if (event.target.value) patch({ [field]: [...new Set([...filters[field], event.target.value])] }); }}>
        <option value="">追加{CLASSIFICATION_NAMES[field]}</option>
        {(facets[field] ?? []).map(facet => <option key={facet.id} value={facet.id}>{facet.name} · {facet.count}</option>)}
      </select></label>)}
      <select aria-label="播放状态" value={filters.status} onChange={e => patch({ status: e.target.value as Filters["status"] })}><option value="all">全部状态</option><option value="want">想看</option><option value="downloaded">已下载</option><option value="undownloaded">全部未下载</option><option value="emby">已关联 Emby</option><option value="files">有关联文件</option><option value="metadata">仅资料</option></select>
      <select aria-label="排序" value={filters.sort} onChange={e => patch({ sort: e.target.value as Filters["sort"], seed: e.target.value === "random" ? Math.floor(Math.random() * 1000000) : undefined })}><option value="added">最近入库</option><option value="newest">最新发行</option><option value="oldest">最早发行</option><option value="title">片名</option><option value="code">番号</option><option value="ranked">榜单热度</option><option value="random">随机</option></select>
      <PersonPicker label="追加演员" onSelect={person =>
        patch({ actors: [...new Set([...filters.actors, person.id])] })} />
      <label><input type="checkbox" checked={filters.actors_all} onChange={e => patch({ actors_all: e.target.checked })} />共同出演</label>
      <label><input type="checkbox" checked={filters.tags_all} onChange={e => patch({ tags_all: e.target.checked })} />全部标签</label>
      <select aria-label="排除标签" value="" onChange={e => { if (e.target.value) patch({ exclude_tags: [...new Set([...filters.exclude_tags, e.target.value])] }); }}>
        <option value="">排除某标签</option>{(facets.tags ?? []).map(f => <option key={f.id} value={f.id}>{f.name}</option>)}
      </select>
      <button onClick={() => setFilters({ ...empty, actors: selectedPerson ? [selectedPerson.id] : [] })}>清除条件</button>
    </div>}
    <div className="active-filter-chips">
      {filters.category && filters.category !== "all" && <button onClick={() => patch({ category: "all" })}>{({ Japan: "JAV", China: "国产", Korea: "韩国", Europe: "欧美", Other: "其他" })[filters.category]} ×</button>}
      {filters.collection && <button onClick={() => patch({ collection: "" })}>{filters.collection} ×</button>}
      {filters.actor_name && <button onClick={() => patch({ actor_name: "" })}>{filters.actor_name} ×</button>}
      {filters.studio_name && <button onClick={() => patch({ studio_name: "" })}>{filters.studio_name} ×</button>}
      {filters.series_name && <button onClick={() => patch({ series_name: "" })}>系列：{filters.series_name} ×</button>}
      {(["series", "labels", "platforms"] as const).flatMap(field => filters[field].map(id => <button key={`${field}:${id}`}
        onClick={() => patch({ [field]: filters[field].filter(value => value !== id) })}>{({ series: "系列", labels: "厂牌", platforms: "发布平台" })[field]}：{facets[field]?.find(value => value.id === id)?.name ?? id} ×</button>))}
      {filters.tags.map(id => <button key={"tag:" + id} onClick={() => patch({ tags: filters.tags.filter(t => t !== id) })}>{facets.tags?.find(tag => matchesTag(tag, id))?.name ?? id} ×</button>)}
      {(["genres", "subtitle_languages"] as const).flatMap(field => filters[field].map(id => <button key={field + id}
        onClick={() => patch({ [field]: filters[field].filter(value => value !== id) })}>{field === "genres" ? "题材" : "历史字幕"}：{facets[field]?.find(value => value.id === id)?.name ?? id} ×</button>))}
      {([...CLASSIFICATION_KEYS, "organizations"] as const).flatMap(field => filters[field].map(id =>
        <button key={field + id} onClick={() => patch({ [field]: filters[field].filter(value => value !== id) })}>{facets[field]?.find(value => value.id === id)?.name ?? id} ×</button>))}
      {(filters.year_from || filters.unknown_year) && <button onClick={() => patch({ year_from: undefined, year_to: undefined, unknown_year: false })}>{filters.unknown_year ? "年份待补" : filters.year_from === filters.year_to ? filters.year_from : `${filters.year_from}–${filters.year_to}`} ×</button>}
      {(filters.duration_min !== undefined || filters.duration_max !== undefined || filters.unknown_duration) && <button onClick={() => patch(EMPTY_DURATION)}>{durationLabel(filters)} ×</button>}
      {filters.status !== "all" && <button onClick={() => patch({ status: "all" })}>{({ emby: "已关联 Emby", files: "有关联文件", metadata: "仅资料", want: "想看", downloaded: "已下载", undownloaded: "全部未下载" })[filters.status]} ×</button>}
      {filters.exclude_tags.map(id => <button key={id}
      onClick={() => patch({ exclude_tags: filters.exclude_tags.filter(t => t !== id) })}>排除 {facets.tags?.find(tag => matchesTag(tag, id))?.name ?? id} ×</button>)}{filters.actors.map(id => <button key={id} onClick={() => patch({ actors: filters.actors.filter(a => a !== id) })}>{actorNames?.[id] ?? id} ×</button>)}</div>
    <div className="catalog-controls">
    <CatalogTags tags={facets.tags ?? []} selected={filters.tags} preview={false}
      onToggle={id => {
        const tag = facets.tags?.find(value => value.id === id);
        const selected = tag ? filters.tags.some(value => matchesTag(tag, value)) : filters.tags.includes(id);
        patch({ tags: selected ? filters.tags.filter(value => tag ? !matchesTag(tag, value) : value !== id) : [...filters.tags, id] });
      }} />
    <details className="catalog-management"><summary>片单与资料工具</summary><div className="catalog-tools">
    <CatalogLibraryTools actions={actions} />
    <TagManagement />
    <ClassificationManagement />
    <CatalogOrganizations works={rows} onFilter={(id, role) => patch({ organizations: [id], organization_role: role ?? "" })} />
    <CatalogSeriesEditor works={rows} onFilter={id => patch({ series: [id], collection: "", series_name: "" })} />
    <CatalogPlaylists works={rows} onOpenWork={openWork} filters={filters} onApplyFilters={value => setFilters({ ...empty, ...value })} />
    <details><summary>保存的筛选</summary>{saved.map(item => <button key={item.name} disabled={migratingSaved} onClick={() => setFilters(filtersSchema.parse(item.filters))}>{item.name}</button>)}<input value={saveName} onChange={e => setSaveName(e.target.value)} placeholder="筛选名称" /><button disabled={migratingSaved} onClick={save}>保存到本机</button></details>
    </div></details>
    </div>
    {loading && !workError && !error && <p role="status">正在读取…</p>}
    <div className="work-grid" aria-label="作品列表" aria-busy={loading}>{rows.map(row => <WorkCard key={row.id} work={workSchema.parse({
      ...row, family: "", category: row.category ?? "Other", created_at: "", updated_at: "",
      primary_code: row.code, image_url: row.poster, want_list: actions.prefs?.want_list.includes(row.id) ?? false,
      release_date: row.release_date ?? (row.year ? String(row.year) : null),
    })} onSelect={openWork} busy={actions.busy} onToggleWant={actions.toggleWant}
      onRefresh={actions.refreshWork} onFilterCast={name => patch({ actor_name: name })}
      onFilterStudio={name => patch({ studio_name: name })}
      actions={<button className="ghost" onClick={() => setEditing(row)}>编辑分类</button>} />)}</div>
    {editing && <TaxonomyEditor key={editing.id} work={editing} onClose={() => setEditing(null)} onSaved={() => {
      sessionStorage.removeItem("mdc:catalog-return"); setEditing(null); setRevision(value => value + 1);
    }} />}

    {!loading && !workError && rows.length === 0 && <p>没有符合条件的作品，可移除部分条件后再试。</p>}
    <div className="catalog-load-more">
      <span className="muted" role="status">已显示 {rows.length} / {total} 部作品</span>
      {cursor ? <button disabled={loading} onClick={() => void more()}>{loading ? "正在加载…" : "加载下一页"}</button>
        : !loading && rows.length > 0 && !workError && <span className="muted">已显示全部作品</span>}
    </div>
    </>}
  </section>;
}
