import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { appUrl } from "../api";
import { queryClient, queryKeys } from "../query/client";
import type { Filters } from "./UnifiedCatalogView";
import { organizationKindSchema, organizationRoleSchema, type OrganizationKind, type OrganizationRole } from "../lib/catalogTypes";

export type ManagedPerson = {
  id: string; ref: string; refs: string[]; name: string; aliases: string[];
  biography: string | null; image_url: string | null; x_handle?: string | null; notes: string; favorite: boolean; blacklist: boolean;
  match_names: string[]; groups: string[]; work_ids: string[]; directed_work_ids?: string[]; revision: number;
  collaborators?: { id: string; name: string; count: number }[];
  organizations?: { id: string; name: string; count: number }[];
  series?: { id: string; name: string; count: number }[];
};
export type PersonCard = {
  id: string; name: string; aliases: string[]; image_url: string | null;
  work_count: number; revision: number; same_name: boolean;
};
type PeoplePage = { items: PersonCard[]; next_offset: number | null; total: number; labels: Record<string, string> };
export const PEOPLE_PAGE_SIZE = 48;
type Organization = { id: string; kind: OrganizationKind; name: string; aliases: string[]; description: string | null; parent_id: string | null; revision: number; work_count: number };
type Series = { id: string; name: string; aliases: string[]; description: string | null; organization_id: string | null; revision: number; work_count: number };
export const ORGANIZATION_NAMES: Record<OrganizationKind, string> = { organization: "片商 / 公司", studio: "工作室", label: "厂牌", platform: "发布平台" };
function organizationLabel(item: Organization, items: Organization[]): string {
  const parent = items.find(value => value.id === item.parent_id);
  return `${parent ? parent.name + " › " : ""}${item.name} · ${ORGANIZATION_NAMES[item.kind] ?? "机构"}`;
}
type Playlist = { id: string; name: string; work_ids: string[]; revision: number; filters?: Filters | null };
type Merge = { id: string; target_id: string; members: string[]; undone: boolean };
let fallbackOwner: string | undefined;
function createCatalogOwner(): string {
  if (typeof crypto.randomUUID === "function") return crypto.randomUUID();
  // LAN HTTP pages expose getRandomValues, but not the secure-context UUID API.
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6]! & 0x0f) | 0x40;
  bytes[8] = (bytes[8]! & 0x3f) | 0x80;
  const hex = Array.from(bytes, byte => byte.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}
export function catalogOwner(): string {
  try {
    const stored = localStorage.getItem("mdc:catalog-owner");
    if (stored) return stored;
  } catch { /* Storage is optional. */ }
  fallbackOwner ??= createCatalogOwner();
  try { localStorage.setItem("mdc:catalog-owner", fallbackOwner); }
  catch { /* Keep a stable owner for this page when storage is unavailable. */ }
  return fallbackOwner;
}
export async function catalogRequest<T>(path: string, method = "GET", body?: unknown, signal?: AbortSignal): Promise<T> {
  const response = await fetch(appUrl("/api/catalog/" + path)!, {
    method, signal, headers: { "Content-Type": "application/json", "X-Catalog-Owner": catalogOwner() },
    ...(body === undefined ? {} : { body: JSON.stringify(body) })
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail ?? "操作失败（" + response.status + "）");
  }
  return response.status === 204 ? undefined as T : response.json() as Promise<T>;
}

export function usePeopleCards(q: string, sort: string, followed: boolean, offset: number, enabled: boolean,
  limit = PEOPLE_PAGE_SIZE) {
  const [search, setSearch] = useState(q);
  useEffect(() => {
    const timer = setTimeout(() => setSearch(q), 180);
    return () => clearTimeout(timer);
  }, [q]);
  const params = new URLSearchParams({
    q: search, sort, followed: String(followed), offset: String(offset), limit: String(limit),
  });
  const result = useQuery({
    queryKey: [...queryKeys.personCards, params.toString()],
    queryFn: ({ signal }) => catalogRequest<PeoplePage>("people-cards?" + params, "GET", undefined, signal),
    enabled: enabled && q === search,
  }, queryClient);
  return { page: q === search ? result.data : undefined, error: result.error,
    loading: q !== search || result.isPending || result.isFetching, retry: result.refetch };
}

export function usePersonLabels(ids: string[]) {
  return useQuery({
    queryKey: [...queryKeys.personCards, "labels", ids],
    queryFn: async ({ signal }) => {
      const labels: Record<string, string> = {};
      for (let start = 0; start < ids.length; start += 100) {
        const params = new URLSearchParams(ids.slice(start, start + 100).map(id => ["id", id]));
        const page = await catalogRequest<PeoplePage>("people-cards?" + params, "GET", undefined, signal);
        Object.assign(labels, page.labels);
      }
      return labels;
    },
    enabled: ids.length > 0,
  }, queryClient).data;
}

export function PersonPicker({ label, exclude, onSelect }: {
  label: string; exclude?: string; onSelect: (person: PersonCard) => void;
}) {
  const [open, setOpen] = useState(false);
  const [search, setSearch] = useState("");
  const [offset, setOffset] = useState(0);
  const { page, error, loading } = usePeopleCards(search, "works", false, offset, open);
  const nextOffset = page?.next_offset;
  return <details onToggle={event => setOpen(event.currentTarget.open)}>
    <summary>{label}</summary>
    {open && <>
      <input aria-label={label + "搜索"} value={search} maxLength={300} placeholder="姓名或别名"
        onChange={event => { setSearch(event.target.value); setOffset(0); }} />
      {loading && <span role="status">正在读取人物…</span>}
      {error && <span role="alert">{error.message}</span>}
      <select aria-label={label} value="" onChange={event => {
        const item = page?.items.find(person => person.id === event.target.value);
        if (item) onSelect(item);
      }}>
        <option value="">选择人物</option>
        {page?.items.filter(person => person.id !== exclude).map(person =>
          <option key={person.id} value={person.id}>{person.name} · {person.work_count} 部
            {person.same_name ? " · " + person.id.slice(-6) : ""}</option>)}
      </select>
      {offset > 0 && <button disabled={loading}
        onClick={() => setOffset(Math.max(0, offset - PEOPLE_PAGE_SIZE))}>上一页</button>}
      {nextOffset != null && <button onClick={() => setOffset(nextOffset)}>下一页</button>}
    </>}
  </details>;
}

const split = (value: string) => [...new Set(value.split(/[、,\n]/).map(item => item.trim()).filter(Boolean))];
export function CatalogPeopleEditor({ person, onSaved }: {
  person: ManagedPerson | null; onSaved: () => void;
}) {
  const [editing, setEditing] = useState<ManagedPerson | null>(null);
  const [aliasesText, setAliasesText] = useState("");
  const [groupsText, setGroupsText] = useState("");
  const [matchesText, setMatchesText] = useState("");
  function openEditor(value: ManagedPerson) {
    setEditing(value); setAliasesText(value.aliases.join("、"));
    setGroupsText(value.groups.join("、")); setMatchesText(value.match_names.join("、"));
  }
  const [workQuery, setWorkQuery] = useState("");
  const [workOptions, setWorkOptions] = useState<{ id: string; title: string }[]>([]);
  const [creditWork, setCreditWork] = useState("");
  const [creditRole, setCreditRole] = useState("actor");
  const [target, setTarget] = useState<PersonCard | null>(null);
  const [showMerges, setShowMerges] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const mergesQuery = useQuery({
    queryKey: [...queryKeys.personCards, "merges"],
    queryFn: ({ signal }) => catalogRequest<Merge[]>("people-merges", "GET", undefined, signal),
    enabled: showMerges,
  }, queryClient);
  const merges = mergesQuery.data ?? [];
  const mergeNames = usePersonLabels(showMerges ? [...new Set(merges.map(item => item.target_id))] : []);
  async function run(operation: () => Promise<void | boolean>) {
    setBusy(true); setError("");
    try {
      if (await operation() === false) return;
      setTarget(null);
      await queryClient.cancelQueries({ queryKey: queryKeys.personCards });
      void queryClient.invalidateQueries({ queryKey: queryKeys.personCards });
      onSaved();
    }
    catch (e) { setError(String(e)); } finally { setBusy(false); }
  }
  function create() {
    openEditor({ id: "", ref: "", refs: [], name: "", aliases: [], biography: "", image_url: "",
      notes: "", favorite: false, blacklist: false, groups: [], match_names: [], work_ids: [], revision: 1 });
  }
  return <section>
    {error && <p role="alert">{error}</p>}
    <button onClick={create}>新增演员档案</button>
    {person && <>
      <button disabled={busy} onClick={() => void run(async () => {
        await catalogRequest("people/" + person.id + "/preference", "PUT", {
          favorite: !person.favorite, blacklist: person.blacklist
        });
      })}>{person.favorite ? "取消关注" : "关注"}</button>
      <button onClick={() => openEditor(person)}>编辑 {person.name} 的资料</button>
      <label>上传头像<input type="file" accept="image/png,image/jpeg,image/webp" disabled={busy}
        onChange={event => { const file = event.target.files?.[0]; if (!file) return; void run(async () => {
          const response = await fetch(appUrl("/api/catalog/people/" + person.id + "/image?revision=" + person.revision)!, { method: "POST", body: file });
          if (!response.ok) throw new Error("头像上传失败，请刷新资料后重试");
        }); }} /></label>

      <details><summary>维护参演或执导关联</summary>
        <input value={workQuery} onChange={e => setWorkQuery(e.target.value)} placeholder="搜索要关联的作品" />
        <button disabled={busy} onClick={() => {
          setBusy(true);
          catalogRequest<{ items: { id: string; title: string }[] }>("works/query", "POST", { q: workQuery })
            .then(page => setWorkOptions(page.items)).catch(e => setError(String(e))).finally(() => setBusy(false));
        }}>查找作品</button>
        <select value={creditWork} onChange={e => setCreditWork(e.target.value)}><option value="">选择作品</option>
          {workOptions.map(work => <option key={work.id} value={work.id}>{work.title}</option>)}
        </select>
        <select value={creditRole} onChange={e => setCreditRole(e.target.value)}>
          <option value="actor">出演</option><option value="director">执导</option>
        </select>
        <button disabled={busy || !creditWork} onClick={() => void run(async () => {
          await catalogRequest("people/" + person.id + "/works", "PUT", {
            work_id: creditWork, role: creditRole, included: true, revision: person.revision
          });
        })}>保存关联</button>
        <button disabled={busy || !creditWork} onClick={() => void run(async () => {
          await catalogRequest("people/" + person.id + "/works", "PUT", {
            work_id: creditWork, role: creditRole, included: false, revision: person.revision
          });
        })}>移除此关联</button>
      </details>
      <PersonPicker label="合并到" exclude={person.id} onSelect={setTarget} />
      {target && <span>{target.name} · {target.id.slice(-6)}</span>}
      <button disabled={busy || !target} onClick={() => void run(async () => {
        if (!target || !window.confirm("确认将“" + person.name + "”合并到“" + target.name + "”？原档案和旧引用会保留，可从记录撤销。")) return false;
        await catalogRequest("people-merge", "POST", { target_id: target.id, source_id: person.id,
          target_revision: target.revision, source_revision: person.revision });
      })}>合并档案</button>
    </>}
    <details onToggle={event => setShowMerges(event.currentTarget.open)}><summary>人物合并记录</summary>
      {showMerges && mergesQuery.error && <p role="alert">{mergesQuery.error.message}</p>}
      {showMerges && merges.map(item => <div key={item.id}>
      <span>{mergeNames?.[item.target_id] ?? "人物档案"} · {item.undone ? "已撤销" : "已合并"}</span>
      {!item.undone && <button disabled={busy} onClick={() => void run(async () => {
        await catalogRequest("people-merges/" + item.id + "/undo", "POST");
      })}>撤销合并</button>}
    </div>)}</details>
    {editing && <div className="genre-sheet-backdrop"><form className="genre-sheet" role="dialog" aria-modal="true" aria-label="编辑演员资料"
      onSubmit={event => { event.preventDefault(); void run(async () => {
        await catalogRequest(editing.id ? "people/" + editing.id : "people", editing.id ? "PUT" : "POST", {
          name: editing.name, aliases: split(aliasesText), biography: editing.biography,
          image_url: editing.image_url || null, notes: editing.notes, groups: split(groupsText),
          match_names: split(matchesText), revision: editing.revision
        });
        setEditing(null);
      }); }}>
      <h2>演员资料</h2>{error && <p role="alert">{error}</p>}
      <label>显示名<input required value={editing.name} onChange={e => setEditing({ ...editing, name: e.target.value })} /></label>
      <label>别名<input value={aliasesText} onChange={e => setAliasesText(e.target.value)} /></label>
      <label>简介<textarea value={editing.biography ?? ""} onChange={e => setEditing({ ...editing, biography: e.target.value })} /></label>
      <label>头像地址<input value={editing.image_url ?? ""} onChange={e => setEditing({ ...editing, image_url: e.target.value })} /></label>
      <label>分组<input value={groupsText} onChange={e => setGroupsText(e.target.value)} /></label>
      <label>目录识别词<input value={matchesText} onChange={e => setMatchesText(e.target.value)} /></label>
      <label>私有备注<textarea value={editing.notes} onChange={e => setEditing({ ...editing, notes: e.target.value })} /></label>
      <button disabled={busy} type="submit">保存</button><button type="button" onClick={() => setEditing(null)}>取消</button>
    </form></div>}
  </section>;
}

export function CatalogOrganizations({ works, onFilter }: {
  works: { id: string; title: string }[];
  onFilter: (id: string, role?: OrganizationRole) => void;
}) {
  const [open, setOpen] = useState(false);
  const [organizations, setOrganizations] = useState<Organization[]>([]);
  const [selected, setSelected] = useState<Organization | null>(null);
  const [aliasesText, setAliasesText] = useState("");
  useEffect(() => { setAliasesText(selected?.aliases.join("、") ?? ""); }, [selected?.id]);
  const [workId, setWorkId] = useState("");
  const [role, setRole] = useState<OrganizationRole>("studio");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const roles: Record<OrganizationRole, string> = { studio: "制作", distributor: "发行", label: "厂牌", platform: "发布平台", credited: "原始署名" };
  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();
    setError("");
    catalogRequest<Organization[]>("organizations", "GET", undefined, controller.signal)
      .then(items => { if (!controller.signal.aborted) setOrganizations(items); })
      .catch(e => { if (!controller.signal.aborted) setError(String(e)); });
    return () => controller.abort();
  }, [open]);
  async function save() {
    if (!selected) return;
    setBusy(true); setError("");
    try {
      const result = await catalogRequest<Organization>(
        selected.id ? "organizations/" + selected.id : "organizations",
        selected.id ? "PUT" : "POST", { ...selected, aliases: split(aliasesText) });
      setSelected(result); setOrganizations(await catalogRequest<Organization[]>("organizations"));
    } catch (e) { setError(String(e)); } finally { setBusy(false); }
  }
  return <details onToggle={event => setOpen(event.currentTarget.open)}><summary>片商与所属机构</summary>
    <p className="muted">工作室可隶属片商，厂牌是品牌，平台是发布渠道；所属关系按实际资料维护。</p>
    {error && <p role="alert">{error}</p>}
    <button onClick={() => { setSelected({ id: "", kind: "organization", name: "", aliases: [],
      description: "", parent_id: null, revision: 0, work_count: 0 }); setAliasesText(""); }}>新增片商或所属机构</button>
    <select value={selected?.id ?? ""} onChange={event => setSelected(organizations.find(item => item.id === event.target.value) ?? null)}>
      <option value="">选择片商或所属机构</option>{organizations.map(item =>
        <option key={item.id} value={item.id}>{organizationLabel(item, organizations)} · {item.work_count} 部</option>)}
    </select>
    {selected && <article><h2>{selected.name}</h2>
      {!selected.id && <label>类型<select value={selected.kind} onChange={e => setSelected({ ...selected, kind: organizationKindSchema.parse(e.target.value) })}>
        {Object.entries(ORGANIZATION_NAMES).map(([key, name]) => <option key={key} value={key}>{name}</option>)}
      </select></label>}
      <label>名称<input value={selected.name} onChange={e => setSelected({ ...selected, name: e.target.value })} /></label>
      <label>别名<input value={aliasesText} onChange={e => setAliasesText(e.target.value)} /></label>
      <label>简介<textarea value={selected.description ?? ""} onChange={e => setSelected({ ...selected, description: e.target.value })} /></label>
      <label>所属机构<select value={selected.parent_id ?? ""} onChange={e => setSelected({ ...selected, parent_id: e.target.value || null })}>
        <option value="">独立 / 归属未知</option>{organizations.filter(item => item.id !== selected.id && ["organization", "studio"].includes(item.kind)).map(item =>
          <option key={item.id} value={item.id}>{organizationLabel(item, organizations)}</option>)}
      </select></label>
      <button disabled={busy} onClick={() => void save()}>保存机构资料</button>
      <button disabled={!selected.id} onClick={() => onFilter(selected.id)}>全部作品</button>
      {Object.entries(roles).map(([key, name]) => <button key={key} disabled={!selected.id} onClick={() => onFilter(selected.id, organizationRoleSchema.parse(key))}>{name}作品</button>)}
      <details><summary>维护作品署名</summary>
        <select value={workId} onChange={e => setWorkId(e.target.value)}><option value="">从当前结果选择作品</option>{works.map(work =>
          <option value={work.id} key={work.id}>{work.title}</option>)}</select>
        <select value={role} onChange={e => setRole(organizationRoleSchema.parse(e.target.value))}>{Object.entries(roles).map(([key, name]) => <option value={key} key={key}>{name}</option>)}</select>
        <button disabled={busy || !workId || !selected.id} onClick={() => {
          setBusy(true);
          catalogRequest("organizations/" + selected.id + "/works", "PUT", { work_id: workId, role })
            .then(() => catalogRequest<Organization[]>("organizations")).then(setOrganizations)
            .catch(e => setError(String(e))).finally(() => setBusy(false));
        }}>保存署名</button>
      </details>
    </article>}
  </details>;
}

export function CatalogSeriesEditor({ works, onFilter }: {
  works: { id: string; title: string }[]; onFilter: (id: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [items, setItems] = useState<Series[]>([]);
  const [organizations, setOrganizations] = useState<Organization[]>([]);
  const [selected, setSelected] = useState<Series | null>(null);
  const [aliases, setAliases] = useState("");
  const [workId, setWorkId] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();
    Promise.all([
      catalogRequest<Series[]>("series", "GET", undefined, controller.signal),
      catalogRequest<Organization[]>("organizations", "GET", undefined, controller.signal),
    ]).then(([series, owners]) => { if (!controller.signal.aborted) { setItems(series); setOrganizations(owners); } })
      .catch(e => { if (!controller.signal.aborted) setError(String(e)); });
    return () => controller.abort();
  }, [open]);
  async function save() {
    if (!selected) return;
    setBusy(true); setError("");
    try {
      const result = await catalogRequest<Series>(selected.id ? `series/${selected.id}` : "series",
        selected.id ? "PUT" : "POST", { ...selected, aliases: split(aliases) });
      setSelected(result); setAliases(result.aliases.join("、"));
      setItems(await catalogRequest<Series[]>("series"));
    } catch (e) { setError(String(e)); } finally { setBusy(false); }
  }
  return <details onToggle={event => setOpen(event.currentTarget.open)}><summary>作品系列</summary>
    <p className="muted">系列组织一组作品，可归属片商、工作室或厂牌；系列成员与制作、发行署名分别维护。</p>
    {error && <p role="alert">{error}</p>}
    <button onClick={() => { setSelected({ id: "", name: "", aliases: [], description: "", organization_id: null, revision: 0, work_count: 0 }); setAliases(""); }}>新增系列</button>
    <select aria-label="选择系列档案" value={selected?.id ?? ""} onChange={e => {
      const value = items.find(item => item.id === e.target.value) ?? null;
      setSelected(value); setAliases(value?.aliases.join("、") ?? "");
    }}><option value="">选择系列</option>{items.map(item => <option key={item.id} value={item.id}>{item.name} · {item.work_count} 部</option>)}</select>
    {selected && <article>
      <label>系列名称<input value={selected.name} onChange={e => setSelected({ ...selected, name: e.target.value })} /></label>
      <label>系列别名<input value={aliases} onChange={e => setAliases(e.target.value)} /></label>
      <label>系列简介<textarea value={selected.description ?? ""} onChange={e => setSelected({ ...selected, description: e.target.value })} /></label>
      <label>归属机构<select value={selected.organization_id ?? ""} onChange={e => setSelected({ ...selected, organization_id: e.target.value || null })}>
        <option value="">归属未知</option>{organizations.map(item => <option key={item.id} value={item.id}>{organizationLabel(item, organizations)}</option>)}
      </select></label>
      <button disabled={busy || !selected.name.trim()} onClick={() => void save()}>保存系列资料</button>
      <button disabled={!selected.id} onClick={() => onFilter(selected.id)}>查看系列作品</button>
      <label>加入系列<select value={workId} onChange={e => setWorkId(e.target.value)}><option value="">从当前结果选择作品</option>{works.map(work => <option key={work.id} value={work.id}>{work.title}</option>)}</select></label>
      <button disabled={busy || !workId || !selected.id} onClick={() => {
        setBusy(true); setError("");
        catalogRequest(`series/${selected.id}/works`, "PUT", { work_id: workId })
          .then(() => catalogRequest<Series[]>("series")).then(setItems)
          .catch(e => setError(String(e))).finally(() => setBusy(false));
      }}>保存系列成员</button>
    </article>}
  </details>;
}

export function CatalogPlaylists({ works, onOpenWork, filters, onApplyFilters }: {
  works: { id: string; title: string }[]; onOpenWork: (id: string) => void;
  filters?: Filters; onApplyFilters?: (value: Filters) => void;
}) {
  const [open, setOpen] = useState(false);
  const [items, setItems] = useState<Playlist[]>([]);
  const [selected, setSelected] = useState<Playlist | null>(null);
  const [name, setName] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [workTitles, setWorkTitles] = useState<Record<string, string>>({});
  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();
    catalogRequest<Playlist[]>("playlists", "GET", undefined, controller.signal)
      .then(rows => { if (!controller.signal.aborted) setItems(rows); })
      .catch(e => { if (!controller.signal.aborted) setError(String(e)); });
    return () => controller.abort();
  }, [open]);
  useEffect(() => {
    if (!selected?.id || selected.filters) return;
    catalogRequest<{ id: string; title: string }[]>("playlists/" + selected.id + "/works")
      .then(rows => setWorkTitles(Object.fromEntries(rows.map(row => [row.id, row.title]))))
      .catch(e => setError(String(e)));
  }, [selected?.id]);
  async function save(playlist: Playlist) {
    setBusy(true); setError("");
    try {
      const result = await catalogRequest<Playlist>("playlists" + (playlist.id ? "/" + playlist.id : ""),
        playlist.id ? "PUT" : "POST", { ...playlist, work_ids: playlist.filters ? [] : playlist.work_ids });
      setItems(await catalogRequest<Playlist[]>("playlists")); setSelected(result); setName("");
    } catch (e) { setError(String(e)); } finally { setBusy(false); }
  }
  function move(index: number, delta: number) {
    if (!selected || index + delta < 0 || index + delta >= selected.work_ids.length) return;
    const ids = [...selected.work_ids];
    [ids[index], ids[index + delta]] = [ids[index + delta]!, ids[index]!];
    setSelected({ ...selected, work_ids: ids });
  }
  return <details onToggle={event => setOpen(event.currentTarget.open)}><summary>我的片单</summary>{error && <p role="alert">{error}</p>}
    <label>片单标识<input aria-label="片单标识" readOnly value={catalogOwner()} /></label>
    <p>在其他客户端填写同一片单标识，即可使用同一片单空间。</p>
    <input aria-label="新片单名称" value={name} onChange={e => setName(e.target.value)} placeholder="新片单名称" />
    <button disabled={busy || !name.trim()} onClick={() => void save({ id: "", name: name.trim(), work_ids: [], revision: 0 })}>创建片单</button>
    {filters && <button disabled={busy || !name.trim()} onClick={() =>
      void save({ id: "", name: name.trim(), work_ids: [], revision: 0, filters })}>用当前筛选创建动态片单</button>}
    <select value={selected?.id ?? ""} onChange={e => setSelected(items.find(item => item.id === e.target.value) ?? null)}>
      <option value="">选择片单</option>{items.map(item => <option key={item.id} value={item.id}>{item.name} {item.filters ? "· 动态" : ""} · {item.work_ids.length} 部 · {item.id.slice(-6)}</option>)}
    </select>
    {selected && <article>
      <input aria-label="片单名称" value={selected.name} onChange={e => setSelected({ ...selected, name: e.target.value })} />
      {selected.filters ? <>
        <p>符合规则的新入库作品会自动加入。手机可在同一片单空间打开。</p>
        <button onClick={() => { if (selected.filters) onApplyFilters?.(selected.filters); }}>应用片单筛选</button>
        <button disabled={busy || !filters} onClick={() => setSelected({ ...selected, filters })}>替换为当前筛选</button>
        <button disabled={busy} onClick={() => void save(selected)}>保存动态片单</button>
      </> : <>
      <select value="" onChange={e => { if (e.target.value) setSelected({ ...selected, work_ids: [...new Set([...selected.work_ids, e.target.value])] }); }}>
        <option value="">添加当前结果中的作品</option>{works.map(work => <option key={work.id} value={work.id}>{work.title}</option>)}
      </select>
      <button onClick={() => setSelected({ ...selected, work_ids: [...new Set([...selected.work_ids, ...works.map(work => work.id)])] })}>加入当前已加载作品</button>
      <ol>{selected.work_ids.map((id, index) => <li key={id}>
        <button onClick={() => onOpenWork(id)}>{works.find(work => work.id === id)?.title ?? workTitles[id] ?? "作品详情"}</button>
        <button aria-label="上移" onClick={() => move(index, -1)} disabled={index === 0}>↑</button>
        <button aria-label="下移" onClick={() => move(index, 1)} disabled={index === selected.work_ids.length - 1}>↓</button>
        <button onClick={() => setSelected({ ...selected, work_ids: selected.work_ids.filter(work => work !== id) })}>移出片单</button>
      </li>)}</ol>
      <button disabled={busy} onClick={() => void save(selected)}>保存片单及顺序</button>
      </>}
      <button disabled={busy} onClick={() => {
        if (!window.confirm("确认删除片单“" + selected.name + "”？共 " + selected.work_ids.length + " 个条目。")) return;
        setBusy(true);
        catalogRequest("playlists/" + selected.id, "DELETE").then(() => {
          setItems(previous => previous.filter(item => item.id !== selected.id)); setSelected(null);
        }).catch(e => setError(String(e))).finally(() => setBusy(false));
      }}>删除片单</button>
    </article>}
  </details>;
}


export function CatalogWorkCredits({ workId, fallback, onOpen }: {
  workId: string; fallback: string[]; onOpen?: (id: string) => void;
}) {
  const [people, setPeople] = useState<ManagedPerson[] | null>(null);
  useEffect(() => {
    let cancelled = false;
    setPeople(null);
    catalogRequest<ManagedPerson[]>("people?work_id=" + encodeURIComponent(workId))
      .then(items => { if (!cancelled) setPeople(items); }).catch(() => { /* Original credits remain visible. */ });
    return () => { cancelled = true; };
  }, [workId]);
  if (people === null) return <div className="work-page-actor-chips">{fallback.map(name =>
    <button type="button" className="display-tag-chip" key={name} onClick={() => onOpen?.(name)}>{name}</button>)}</div>;
  return <div className="work-page-actor-chips">{people.length === 0 ? "—" : people.map(person =>
    <button type="button" className="display-tag-chip" key={person.id} onClick={() => onOpen?.(person.id)}>
      {person.name}{person.directed_work_ids?.includes(workId) ? " · 导演" : ""}
    </button>)}</div>;
}
