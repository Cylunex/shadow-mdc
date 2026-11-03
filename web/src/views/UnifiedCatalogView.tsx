import { useEffect, useRef, useState } from "react";
import { appUrl } from "../api";
import { readWorksFiltersFromSearch } from "../lib/worksFilters";
import { CatalogPeopleEditor, CatalogOrganizations, CatalogPlaylists, catalogRequest, type ManagedPerson } from "./CatalogManagement";
import { CoverImage } from "../components/CoverImage";

type Card = { id: string; title: string; year: number | null; actors: string[]; tags: string[]; poster: string; studio: string | null };
type Person = ManagedPerson;
type Facet = { id: string; name: string; count: number; role: string };
type Filters = { q: string; actors: string[]; actors_all: boolean; actor_name?: string; studio_name?: string; tags: string[]; tags_all: boolean; exclude_tags: string[]; genres: string[]; forms: string[]; regions: string[]; organizations: string[]; organization_role?: string; year_from?: number; year_to?: number; unknown_year: boolean; sort: string; status: string };
export type CatalogFeedStart = { filters: Filters; items: Card[]; nextCursor: string | null; index?: number };
const empty: Filters = { q: "", actors: [], actors_all: false, tags: [], tags_all: false, exclude_tags: [], genres: [], forms: [], regions: [], organizations: [], unknown_year: false, sort: "added", status: "all" };
async function request<T>(path: string, body?: unknown, signal?: AbortSignal): Promise<T> {
  const response = await fetch(appUrl("/api/catalog/" + path)!, { signal, ...(body ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {}) });
  if (!response.ok) throw new Error("读取失败（" + response.status + "）");
  return response.json() as Promise<T>;
}
export function UnifiedCatalogView({ mode, onOpenWork, onOpenActor, initialPersonRef, onStartFeed }: { mode: "works" | "actors" | "categories"; initialPersonRef?: string; onStartFeed?: (state: CatalogFeedStart) => void; onOpenWork: (id: string) => void; onOpenActor: (id: string) => void }) {
  const [filters, setFilters] = useState<Filters>(() => {
    try {
      const params = new URLSearchParams(window.location.search);
      const legacy = readWorksFiltersFromSearch();
      if (["q", "tag", "year", "studio", "cast"].some(key => params.has(key))) {
        return { ...empty, q: legacy.q, tags: legacy.tags, tags_all: true,
          actor_name: legacy.cast, studio_name: legacy.studio,
          year_from: legacy.year ? Number(legacy.year) : undefined,
          year_to: legacy.year ? Number(legacy.year) : undefined };
      }
      return { ...empty, ...JSON.parse(params.get("catalog") ?? sessionStorage.getItem("mdc:catalog-filter") ?? "{}") };
    } catch { return empty; }
  });
  const generation = useRef(0);
  const [rows, setRows] = useState<Card[]>([]);
  const [people, setPeople] = useState<Person[]>([]);
  const [facets, setFacets] = useState<Record<string, Facet[]>>({});
  const [cursor, setCursor] = useState<string | null>(null);
  const [total, setTotal] = useState(0);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [showFilters, setShowFilters] = useState(() => typeof window !== "undefined" && window.innerWidth >= 800);
  const [actorSort, setActorSort] = useState("works");
  const [followedOnly, setFollowedOnly] = useState(false);
  const [actorSearch, setActorSearch] = useState("");
  const [selectedPerson, setSelectedPerson] = useState<Person | null>(null);
  const [saved, setSaved] = useState<{ name: string; filters: Filters }[]>(() => {
    try { return JSON.parse(localStorage.getItem("mdc:saved-filters") ?? "[]"); } catch { return []; }
  });
  const [editing, setEditing] = useState<Card | null>(null);
  const [taxonomy, setTaxonomy] = useState({ genres: "", forms: "", regions: "", release_year: "" });
  const [revision, setRevision] = useState(0);
  async function edit(row: Card) {
    try {
      const value = await request<{ genres: string[]; forms: string[]; regions: string[]; release_year: number | null }>("works/" + row.id + "/taxonomy");
      setTaxonomy({ genres: value.genres.join("、"), forms: value.forms.join("、"), regions: value.regions.join("、"), release_year: String(value.release_year ?? "") });
      setEditing(row);
    } catch (e) { setError(String(e)); }
  }
  async function saveTaxonomy() {
    if (!editing) return;
    try {
      const response = await fetch(appUrl("/api/catalog/works/" + editing.id + "/taxonomy")!, {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ genres: taxonomy.genres.split(/[、,]/).map(s => s.trim()).filter(Boolean),
          forms: taxonomy.forms.split(/[、,]/).map(s => s.trim()).filter(Boolean),
          regions: taxonomy.regions.split(/[、,]/).map(s => s.trim()).filter(Boolean),
          release_year: taxonomy.release_year ? Number(taxonomy.release_year) : null })
      });
      if (!response.ok) throw new Error("分类保存失败（" + response.status + "）");
      sessionStorage.removeItem("mdc:catalog-return");
      setEditing(null); setRevision(value => value + 1);
    } catch (e) { setError(String(e)); }
  }
  const [saveName, setSaveName] = useState("");
  const patch = (value: Partial<Filters>) => setFilters(previous => ({ ...previous, ...value }));
  useEffect(() => {
    if (!initialPersonRef) return;
    let cancelled = false;
    catalogRequest<Person>("people/" + encodeURIComponent(initialPersonRef)).then(person => {
      if (cancelled) return;
      setSelectedPerson(person);
      setFilters(previous => ({ ...previous, actors: [person.id], actor_name: "" }));
    }).catch(e => { if (!cancelled) setError(String(e)); });
    return () => { cancelled = true; };
  }, [initialPersonRef, revision]);
  useEffect(() => { try {
      sessionStorage.setItem("mdc:catalog-filter", JSON.stringify(filters));
      const url = new URL(window.location.href);
      for (const key of ["q", "tag", "year", "studio", "cast"]) url.searchParams.delete(key);
      url.searchParams.set("catalog", JSON.stringify(filters));
      history.replaceState(history.state, "", url);
    } catch { /* Storage is optional. */ } }, [filters]);
  useEffect(() => {
    const controller = new AbortController();
    request<Person[]>("people", undefined, controller.signal).then(setPeople).catch(e => { if (!controller.signal.aborted) setError(String(e)); });
    return () => controller.abort();
  }, [revision]);
  useEffect(() => {
    const controller = new AbortController();
    generation.current += 1;
    setLoading(true); setError("");
    let snapshot: { filters: Filters; rows: Card[]; cursor: string | null; total: number; scroll: number; at: number } | null = null;
    try { snapshot = JSON.parse(sessionStorage.getItem("mdc:catalog-return") ?? "null"); } catch { /* Optional cache. */ }
    if (snapshot && Date.now() - snapshot.at < 300000 && JSON.stringify(snapshot.filters) === JSON.stringify(filters)) {
      setRows(snapshot.rows); setCursor(snapshot.cursor); setTotal(snapshot.total); setLoading(false);
      request<Record<string, Facet[]>>("facets", filters, controller.signal).then(setFacets)
        .catch(e => { if (!controller.signal.aborted) setError(String(e)); });
      const position = snapshot.scroll;
      const frame = requestAnimationFrame(() => window.scrollTo(0, position));
      return () => { controller.abort(); cancelAnimationFrame(frame); };
    }
    const timer = setTimeout(() => {
      Promise.all([
        request<{ items: Card[]; next_cursor: string | null; total: number }>("works/query", filters, controller.signal),
        request<Record<string, Facet[]>>("facets", filters, controller.signal)
      ]).then(([page, values]) => {
        if (controller.signal.aborted) return;
        setRows(page.items); setCursor(page.next_cursor); setTotal(page.total); setFacets(values);
      }).catch(e => { if (!controller.signal.aborted) setError(String(e)); })
        .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    }, 180);
    return () => { clearTimeout(timer); controller.abort(); };
  }, [filters, revision]);
  async function more() {
    if (!cursor || loading) return;
    const active = generation.current;
    setLoading(true);
    try {
      const page = await request<{ items: Card[]; next_cursor: string | null }>("works/query", { ...filters, cursor });
      if (active !== generation.current) return;
      setRows(previous => [...new Map([...previous, ...page.items].map(row => [row.id, row])).values()]);
      setCursor(page.next_cursor);
    } catch (e) { if (active === generation.current) setError(String(e)); }
    finally { if (active === generation.current) setLoading(false); }
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
    <h1>{mode === "actors" ? "演员库" : mode === "categories" ? "分类浏览" : "作品库"}</h1>
    <p className="muted">统一作品目录 · {total} 部作品</p>
    {onStartFeed && <button disabled={rows.length === 0 || loading} onClick={() => {
      try { sessionStorage.setItem("mdc:catalog-return", JSON.stringify({
        filters, rows, cursor, total, scroll: window.scrollY, at: Date.now()
      })); } catch { /* Storage is optional. */ }
      onStartFeed({ filters, items: rows, nextCursor: cursor, index: 0 });
    }}>按当前筛选刷片</button>}

    {error && <p role="alert">{error}</p>}
    {mode === "actors" && <>
      <select aria-label="演员排序" value={actorSort} onChange={e => setActorSort(e.target.value)}>
        <option value="works">库内作品数</option><option value="name">姓名</option>
      </select>
      <label><input type="checkbox" checked={followedOnly} onChange={e => setFollowedOnly(e.target.checked)} />已关注</label>
      <input aria-label="搜索演员与别名" placeholder="演员、别名" value={actorSearch} onChange={e => setActorSearch(e.target.value)} />
      {!initialPersonRef && <div className="actor-grid">{people.filter(p => (!followedOnly || p.favorite) &&
        [p.name, ...p.aliases].join(" ").toLowerCase().includes(actorSearch.toLowerCase()))
        .sort((a, b) => actorSort === "name" ? a.name.localeCompare(b.name, "zh") : b.work_ids.length - a.work_ids.length)
        .map(p =>
        <button key={p.id} className="actor-card" onClick={() => {
          patch({ actors: [p.id] });
          catalogRequest<Person>("people/" + p.id).then(setSelectedPerson).catch(e => setError(String(e)));
        }}>
          <CoverImage src={appUrl(p.image_url) ?? ""} /><strong>{p.name}</strong>
          {people.some(other => other.id !== p.id && other.name === p.name) && <span>档案 {p.id.slice(-6)}</span>}
          <span>{p.aliases.slice(0, 2).join(" / ")} · {p.work_ids.length} 部</span>
        </button>)}</div>}
      {selectedPerson && <article><h2>{selectedPerson.name}</h2><p>{selectedPerson.biography}</p>
        {selectedPerson.x_handle && <a href={"https://x.com/" + encodeURIComponent(selectedPerson.x_handle)}
          target="_blank" rel="noreferrer">X · {selectedPerson.x_handle}</a>}
        {!selectedPerson.ref.startsWith("person:") && <button onClick={() => onOpenActor(selectedPerson.ref)}>原有关注与订阅</button>}
        <p>常合作演员</p>{selectedPerson.collaborators?.map(other => <button key={other.id}
          onClick={() => patch({ actors: [selectedPerson.id, other.id], actors_all: true })}>{other.name} · {other.count}</button>)}
        <p>合作机构</p>{selectedPerson.organizations?.map(other => <button key={other.id}
          onClick={() => patch({ actors: [selectedPerson.id], organizations: [other.id] })}>{other.name} · {other.count}</button>)}
      </article>}
      <CatalogPeopleEditor person={selectedPerson} people={people} onSaved={() => {
        if (selectedPerson) catalogRequest<Person>("people/" + selectedPerson.id)
          .then(person => { setSelectedPerson(person); patch({ actors: [person.id] }); })
          .catch(e => setError(String(e)));
        setRevision(value => value + 1);
      }} />
    </>}
    <div className="catalog-search">
      <input value={filters.q} aria-label="检索作品" placeholder="片名、番号、演员、片商" onChange={e => patch({ q: e.target.value })} />
      <button className="ghost" aria-expanded={showFilters} onClick={() => setShowFilters(value => !value)}>{showFilters ? "收起筛选" : "分类与筛选"}</button>
    </div>
    {showFilters && <div className="works-extra-filters">
      <select aria-label="年代" value={filters.unknown_year ? "unknown" : filters.year_from && filters.year_to === filters.year_from + 9 ? String(filters.year_from) : ""} onChange={e => patch(e.target.value === "unknown" ? { unknown_year: true, year_from: undefined, year_to: undefined } : { unknown_year: false, year_from: e.target.value ? Number(e.target.value) : undefined, year_to: e.target.value ? Number(e.target.value) + 9 : undefined })}>
        <option value="">全部年代</option>{(facets.decades ?? []).map(f => <option key={f.id} value={f.id}>{f.id === "unknown" ? "年份待补" : f.name + " 年代"} · {f.count}</option>)}
      </select>
      <select aria-label="发行年份" value={filters.year_from === filters.year_to ? String(filters.year_from ?? "") : ""} onChange={e => patch({ unknown_year: false, year_from: e.target.value ? Number(e.target.value) : undefined, year_to: e.target.value ? Number(e.target.value) : undefined })}><option value="">全部年份</option>{(facets.years ?? []).filter(f => f.id !== "unknown").map(f => <option key={f.id} value={f.id}>{f.name} · {f.count}</option>)}</select>
      <select aria-label="机构和系列" value={filters.organizations[0] ?? ""} onChange={e => patch({ organizations: e.target.value ? [e.target.value] : [], organization_role: "" })}><option value="">全部片商 / 工作室 / 厂牌 / 系列</option>{(facets.organizations ?? []).map(f => <option key={f.id} value={f.id}>{f.name} · {({ studio: "工作室", label: "厂牌", platform: "平台", series: "系列" } as Record<string, string>)[f.role] ?? "署名机构"} · {f.count}</option>)}</select>
      {(["genres", "forms", "regions"] as const).map(field => <select key={field}
        aria-label={field} value={filters[field][0] ?? ""}
        onChange={e => patch({ [field]: e.target.value ? [e.target.value] : [] })}>
        <option value="">{field === "genres" ? "全部题材" : field === "forms" ? "全部作品形式" : "全部制作地区"}</option>
        {(facets[field] ?? []).map(f => <option key={f.id} value={f.id}>{f.name} · {f.count}</option>)}
      </select>)}
      <select aria-label="播放状态" value={filters.status} onChange={e => patch({ status: e.target.value })}><option value="all">全部状态</option><option value="emby">已关联 Emby</option><option value="files">有关联文件</option><option value="metadata">仅资料</option></select>
      <select aria-label="排序" value={filters.sort} onChange={e => patch({ sort: e.target.value })}><option value="added">最近入库</option><option value="newest">最新发行</option><option value="oldest">最早发行</option><option value="title">片名</option></select>
      <select aria-label="追加演员" value="" onChange={e => { if (e.target.value) patch({ actors: [...new Set([...filters.actors, e.target.value])] }); }}><option value="">添加演员条件</option>{people.map(p => <option key={p.id} value={p.id}>{p.name} · {p.work_ids.length}</option>)}</select>
      <label><input type="checkbox" checked={filters.actors_all} onChange={e => patch({ actors_all: e.target.checked })} />共同出演</label>
      <label><input type="checkbox" checked={filters.tags_all} onChange={e => patch({ tags_all: e.target.checked })} />全部标签</label>
      <select aria-label="排除标签" value="" onChange={e => { if (e.target.value) patch({ exclude_tags: [...new Set([...filters.exclude_tags, e.target.value])] }); }}>
        <option value="">排除某标签</option>{(facets.tags ?? []).map(f => <option key={f.id} value={f.id}>{f.name}</option>)}
      </select>
      <button onClick={() => { setFilters(empty); setSelectedPerson(null); }}>清除条件</button>
    </div>}
    <div className="active-filter-chips">
      {filters.tags.map(name => <button key={"tag:" + name} onClick={() => patch({ tags: filters.tags.filter(t => t !== name) })}>{name} ×</button>)}
      {(["genres", "forms", "regions", "organizations"] as const).flatMap(field => filters[field].map(id =>
        <button key={field + id} onClick={() => patch({ [field]: filters[field].filter(value => value !== id) })}>{facets[field]?.find(value => value.id === id)?.name ?? id} ×</button>))}
      {(filters.year_from || filters.unknown_year) && <button onClick={() => patch({ year_from: undefined, year_to: undefined, unknown_year: false })}>{filters.unknown_year ? "年份待补" : filters.year_from === filters.year_to ? filters.year_from : `${filters.year_from}–${filters.year_to}`} ×</button>}
      {filters.status !== "all" && <button onClick={() => patch({ status: "all" })}>{({ emby: "已关联 Emby", files: "有关联文件", metadata: "仅资料" })[filters.status]} ×</button>}
      {filters.exclude_tags.map(name => <button key={name}
      onClick={() => patch({ exclude_tags: filters.exclude_tags.filter(t => t !== name) })}>排除 {name} ×</button>)}{filters.actors.map(id => <button key={id} onClick={() => patch({ actors: filters.actors.filter(a => a !== id) })}>{people.find(p => p.id === id)?.name ?? id} ×</button>)}</div>
    <div className="catalog-tools">
    <details open={mode === "categories"}><summary>标签 · {facets.tags?.length ?? 0}</summary><div className="tag-capsules">{(facets.tags ?? []).map(f => <button key={f.id} className={filters.tags.includes(f.id) ? "active" : "ghost"} onClick={() => patch({ tags: filters.tags.includes(f.id) ? filters.tags.filter(t => t !== f.id) : [...filters.tags, f.id] })}>{f.name} · {f.count}</button>)}</div></details>
    <CatalogOrganizations works={rows} onFilter={(id, role) => patch({ organizations: [id], organization_role: role ?? "" })} />
    <CatalogPlaylists works={rows} onOpenWork={openWork} />
    <details><summary>保存的筛选</summary>{saved.map(item => <button key={item.name} onClick={() => setFilters(item.filters)}>{item.name}</button>)}<input value={saveName} onChange={e => setSaveName(e.target.value)} placeholder="筛选名称" /><button onClick={save}>保存到本机</button></details>
    </div>
    {loading && <p role="status">正在读取…</p>}
    <div className="work-grid">{rows.map(row => <article className="work-card" key={row.id}>
      <button onClick={() => openWork(row.id)}><CoverImage src={appUrl(row.poster) ?? ""} /><strong>{row.title}</strong></button>
      <span>{row.year ?? "年份待补"} · {row.studio}</span><span>{row.actors.slice(0, 2).join(" / ")}</span>
      <button className="ghost" onClick={() => void edit(row)}>编辑分类</button>
    </article>)}</div>
    {editing && <div className="genre-sheet-backdrop"><form className="genre-sheet" role="dialog" aria-modal="true" aria-label="编辑作品分类" onSubmit={e => { e.preventDefault(); void saveTaxonomy(); }}>
      <h2>{editing.title}</h2>{(["genres", "forms", "regions", "release_year"] as const).map(field =>
        <label key={field}>{({ genres: "题材", forms: "形式", regions: "制作地区", release_year: "发行年" })[field]}
          <input value={taxonomy[field]} onChange={e => setTaxonomy(previous => ({ ...previous, [field]: e.target.value }))} />
        </label>)}
      <p>多值用顿号分隔。手工分类独立保存，来源刷新不会覆盖。</p>
      <button type="submit">保存</button><button type="button" onClick={() => setEditing(null)}>取消</button>
    </form></div>}

    {!loading && rows.length === 0 && <p>没有符合条件的作品，可移除部分条件后再试。</p>}
    {cursor && <button disabled={loading} onClick={() => void more()}>加载下一页</button>}
  </section>;
}
