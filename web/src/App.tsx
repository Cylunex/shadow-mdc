import { FormEvent, useCallback, useDeferredValue, useEffect, useMemo, useState } from "react";

import { api, appUrl } from "./api";
import type { NonJavActorEditPayload, OrganizePayload } from "./api";
import { identityAliasesSchema } from "./model";
import type { ActorProfile, Asset, BatchPlan, Candidate, IdentityAliases, Library, NonJavActor, TaskRun, Work } from "./model";

type View = "inbox" | "works" | "actors" | "libraries" | "tasks";
type DisplayCategory = "all" | "Japan" | "China" | "Korea" | "Europe" | "Other";

const INBOX_PAGE_SIZE = 15;
const DIRECTORY_FILE_PAGE_SIZE = 10;
const ACTOR_PAGE_SIZE = 24;
const NON_JAV_ACTOR_PAGE_SIZE = 30;
const WORK_PAGE_SIZE = 48;

const displayCategoryOptions: ReadonlyArray<{ value: DisplayCategory; label: string }> = [
  { value: "all", label: "全部分类" },
  { value: "Japan", label: "JAV" },
  { value: "China", label: "国产" },
  { value: "Korea", label: "韩国" },
  { value: "Europe", label: "欧美" },
  { value: "Other", label: "其他" }
];

export function App() {
  const [view, setView] = useState<View>("inbox");
  const [libraries, setLibraries] = useState<Library[]>([]);
  const [assets, setAssets] = useState<Asset[]>([]);
  const [works, setWorks] = useState<Work[]>([]);
  const [actors, setActors] = useState<ActorProfile[]>([]);
  const [nonJavActors, setNonJavActors] = useState<NonJavActor[]>([]);
  const [tasks, setTasks] = useState<TaskRun[]>([]);
  const [candidates, setCandidates] = useState<Record<string, Candidate[]>>({});
  const [providerStatus, setProviderStatus] = useState<string[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [message, setMessage] = useState("正在连接 Shadow MDC…");

  const refresh = useCallback(async () => {
    try {
      const [nextLibraries, nextAssets, nextWorks, nextActors, nextNonJavActors, nextTasks, providers] = await Promise.all([
        api.libraries(),
        api.assets(),
        api.works(),
        api.actors(),
        api.nonJavActors(),
        api.tasks(),
        api.providers()
      ]);
      setLibraries(nextLibraries);
      setAssets(nextAssets);
      setWorks(nextWorks);
      setActors(nextActors);
      setNonJavActors(nextNonJavActors);
      setTasks(nextTasks);
      setProviderStatus(
        providers.providers.map((provider) => `${provider.name}: ${provider.configured ? "已配置" : "未配置"}`)
      );
      setMessage("数据已同步");
    } catch (error) {
      setMessage(errorMessage(error));
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const inbox = useMemo(() => assets.filter((asset) => asset.state !== "identified"), [assets]);

  async function run(key: string, action: () => Promise<void>) {
    setBusy(key);
    try {
      await action();
      await refresh();
    } catch (error) {
      setMessage(errorMessage(error));
    } finally {
      setBusy(null);
    }
  }

  async function identify(asset: Asset, payload: { title?: string; source_url?: string }) {
    await run(asset.id, async () => {
      const result = await api.identify(asset.id, payload);
      const next = await api.candidates(asset.id);
      setCandidates((current) => ({ ...current, [asset.id]: next }));
      const failures = result.failures.map((failure) => `${failure.provider}: ${failure.reason}`).join("；");
      setMessage(
        result.accepted_work_id
          ? "已按高置信结果自动识别"
          : `得到 ${result.candidate_ids.length} 个候选${failures ? `；${failures}` : ""}`
      );
    });
  }

  async function loadCandidates(assetId: string) {
    await run(`candidate-${assetId}`, async () => {
      const next = await api.candidates(assetId);
      setCandidates((current) => ({ ...current, [assetId]: next }));
    });
  }

  async function createManualCandidate(asset: Asset, title: string | undefined) {
    await run(`manual-${asset.id}`, async () => {
      const candidate = await api.manualCandidate(asset.id, title ? { title } : {});
      const next = await api.candidates(asset.id);
      setCandidates((current) => ({ ...current, [asset.id]: next }));
      setMessage(`已生成本地候选：${candidate.record.title}`);
    });
  }

  async function accept(candidate: Candidate) {
    await run(candidate.id, async () => {
      await api.accept(candidate.id);
      setMessage(`已接受 ${candidate.record.provider} 候选`);
    });
  }

  return (
    <div className="shell">
      <aside>
        <div className="brand">
          <span className="brand-mark">S</span>
          <div><strong>Shadow MDC</strong><small>metadata control</small></div>
        </div>
        <nav>
          <Nav active={view === "inbox"} onClick={() => setView("inbox")} label="待确认" count={inbox.length} />
          <Nav active={view === "works"} onClick={() => setView("works")} label="作品库" count={works.length} />
          <Nav active={view === "actors"} onClick={() => setView("actors")} label="演员库" count={actors.length + nonJavActors.length} />
          <Nav active={view === "libraries"} onClick={() => setView("libraries")} label="媒体库" count={libraries.length} />
          <Nav active={view === "tasks"} onClick={() => setView("tasks")} label="运行记录" count={tasks.length} />
        </nav>
        <div className="provider-list">
          <span>来源状态</span>
          {providerStatus.map((item) => <small key={item}>{item}</small>)}
        </div>
      </aside>
      <main>
        <header>
          <div>
            <p className="eyebrow">LOCAL-FIRST / REVIEW-FIRST</p>
            <h1>{view === "inbox" ? "识别收件箱" : view === "works" ? "作品库" : view === "actors" ? "演员作品库" : view === "libraries" ? "媒体库" : "运行记录"}</h1>
          </div>
          <button className="ghost" onClick={() => void refresh()}>刷新</button>
        </header>
        <div className="status">{message}</div>
        {view === "inbox" && (
          <Inbox
            assets={inbox}
            libraries={libraries}
            nonJavActors={nonJavActors}
            candidates={candidates}
            busy={busy}
            identify={identify}
            createManualCandidate={createManualCandidate}
            loadCandidates={loadCandidates}
            accept={accept}
            assignDirectoryActor={(asset, actor, category, directory) => run(`directory-${asset.id}`, async () => {
              const result = await api.assignDirectoryActor(asset.id, actor, category, directory);
              setMessage(`目录 ${result.directory} 已绑定 ${result.actor}：处理 ${result.cataloged} 个，跳过 ${result.skipped} 个`);
            })}
          />
        )}
        {view === "works" && (
          <Works
            works={works}
            busy={busy}
            refreshMetadata={(work) => run(`work-${work.id}`, async () => {
              const result = await api.refreshWork(work.id);
              const failures = result.failures.map((failure) => failure.provider).join("、");
              setMessage(
                result.accepted_work_id
                  ? `已更新 ${work.primary_code ?? work.title} 的在线元数据`
                  : `未找到可自动接受的在线结果${failures ? `；失败来源：${failures}` : ""}`
              );
            })}
            downloadArtwork={(work) => run(`artwork-${work.id}`, async () => {
              const result = await api.downloadArtwork(work.id);
              setMessage(
                `图片缓存：新下载 ${result.downloaded}，已有 ${result.cached}，失败 ${result.failed}`
              );
            })}
            lookupWork={(code) => run("lookup-work", async () => {
              const result = await api.lookupWork(code);
              const failed = result.failures.map((item) => `${item.provider}/${item.reason}`).join("、");
              setMessage(
                result.work
                  ? `已按番号建立或更新 ${result.work.primary_code ?? result.work.title}，聚合 ${result.matched_records} 个来源`
                  : `没有找到可自动确认的结果${result.matched_records ? `；有 ${result.matched_records} 条低置信候选未采用` : ""}${failed ? `；${failed}` : ""}`
              );
            })}
            translateWorks={() => run("translate-works", async () => {
              const result = await api.translateWorks();
              const error = result.errors.length > 0 ? `；${result.errors[0]}` : "";
              setMessage(
                `标题翻译：成功 ${result.translated}，无需翻译 ${result.skipped}，` +
                `失败 ${result.failed}，剩余 ${result.remaining}${error}`
              );
            })}
          />
        )}
        {view === "actors" && <Actors
          actors={actors}
          nonJavActors={nonJavActors}
          busy={busy}
          saveNonJavActor={(previousName, payload) => run(`actor-${previousName ?? "new"}`, async () => {
            if (previousName) await api.updateNonJavActor(previousName, payload);
            else await api.createNonJavActor(payload);
            setMessage(previousName ? "演员资料已更新" : "演员已加入非 JAV 名单");
          })}
          deleteNonJavActor={(actor) => run(`actor-delete-${actor.name}`, async () => {
            await api.deleteNonJavActor(actor.name);
            setMessage(`已从非 JAV 名单移除 ${actor.name}`);
          })}
          uploadActorImage={(actor, file) => run(`actor-image-${actor.name}`, async () => {
            await api.uploadNonJavActorImage(actor.name, file);
            setMessage(`${actor.name} 的头像已更新`);
          })}
        />}
        {view === "libraries" && (
          <Libraries libraries={libraries} busy={busy} run={run} report={setMessage} />
        )}
        {view === "tasks" && <TaskHistory tasks={tasks} />}
      </main>
    </div>
  );
}

function actorInitials(name: string): string {
  const cleaned = name.normalize("NFKC").trim().replace(/\s+/g, " ");
  if (!cleaned) return "?";
  const letters = [...cleaned].filter((ch) => !/\s|[·・._\-@]/.test(ch));
  if (!letters.length) return "?";
  const cjk = letters.filter((ch) => ch.charCodeAt(0) > 0x2e7f);
  if (cjk.length) return cjk.slice(0, 2).join("");
  const parts = cleaned.replace(/[_-]/g, " ").split(/\s+/).filter(Boolean);
  if (parts.length >= 2) return (parts[0][0] + parts[1][0]).toUpperCase();
  const alnum = letters.filter((ch) => /[0-9A-Za-z]/.test(ch)).join("");
  if (alnum.length >= 2) return alnum.slice(0, 2).toUpperCase();
  return letters[0].toUpperCase();
}

function DisplayFilterBar(props: {
  query: string;
  category: DisplayCategory;
  visible: number;
  total: number;
  placeholder: string;
  setQuery: (value: string) => void;
  setCategory: (value: DisplayCategory) => void;
}) {
  return <div className="display-filter" aria-label="展示筛选">
    <input
      value={props.query}
      onChange={(event) => props.setQuery(event.target.value)}
      placeholder={props.placeholder}
      aria-label="展示搜索"
    />
    <select
      value={props.category}
      onChange={(event) => props.setCategory(event.target.value as DisplayCategory)}
      aria-label="展示分类"
    >
      {displayCategoryOptions.map((option) => (
        <option key={option.value} value={option.value}>{option.label}</option>
      ))}
    </select>
    <span>显示 {props.visible} / {props.total}</span>
    {(props.query || props.category !== "all") && (
      <button
        type="button"
        className="ghost"
        onClick={() => {
          props.setQuery("");
          props.setCategory("all");
        }}
      >清空筛选</button>
    )}
  </div>;
}

function Pagination(props: {
  page: number;
  total: number;
  pageSize: number;
  unit?: string;
  setPage: (page: number) => void;
}) {
  const pageCount = Math.max(1, Math.ceil(props.total / props.pageSize));
  const page = Math.min(Math.max(1, props.page), pageCount);
  if (props.total <= props.pageSize) return null;
  const first = (page - 1) * props.pageSize + 1;
  const last = Math.min(page * props.pageSize, props.total);
  return <div className="pagination" aria-label="分页导航">
    <button className="ghost" disabled={page === 1} onClick={() => props.setPage(1)}>首页</button>
    <button className="secondary" disabled={page === 1} onClick={() => props.setPage(page - 1)}>上一页</button>
    <span>第 {page} / {pageCount} 页 · {first}–{last} / {props.total} {props.unit ?? "个"}</span>
    <button className="secondary" disabled={page === pageCount} onClick={() => props.setPage(page + 1)}>下一页</button>
    <button className="ghost" disabled={page === pageCount} onClick={() => props.setPage(pageCount)}>末页</button>
  </div>;
}

function Actors(props: {
  actors: ActorProfile[];
  nonJavActors: NonJavActor[];
  busy: string | null;
  saveNonJavActor: (previousName: string | null, payload: NonJavActorEditPayload) => Promise<void>;
  deleteNonJavActor: (actor: NonJavActor) => Promise<void>;
  uploadActorImage: (actor: NonJavActor, file: File) => Promise<void>;
}) {
  const nonJavWorkTotal = props.nonJavActors.reduce((sum, actor) => sum + (actor.work_count ?? 0), 0);
  const [source, setSource] = useState<"jav" | "non-jav">(props.actors.length === 0 && props.nonJavActors.length > 0 ? "non-jav" : "jav");
  return <>
    <div className="actor-source-tabs">
      <button className={source === "jav" ? "active" : "ghost"} onClick={() => setSource("jav")}>JAV 演员作品库 · {props.actors.length}</button>
      <button className={source === "non-jav" ? "active" : "ghost"} onClick={() => setSource("non-jav")}>非 JAV 演员 / 作品 · {props.nonJavActors.length} · 作品 {nonJavWorkTotal}</button>
    </div>
    {source === "jav"
      ? <JavActors actors={props.actors} />
      : <NonJavActorsManager
          actors={props.nonJavActors}
          busy={props.busy}
          save={props.saveNonJavActor}
          remove={props.deleteNonJavActor}
          uploadImage={props.uploadActorImage}
        />}
  </>;
}

function JavActors({ actors }: { actors: ActorProfile[] }) {
  const [query, setQuery] = useState("");
  const [category, setCategory] = useState<DisplayCategory>("all");
  const [page, setPage] = useState(1);
  const deferredQuery = useDeferredValue(query);
  const normalizedQuery = deferredQuery.trim().toLocaleLowerCase();
  const visibleActors = useMemo(() => actors.filter((actor) => {
    const categoryMatches = category === "all" || actor.categories.includes(category);
    const text = [
      actor.name,
      ...actor.aliases,
      ...actor.works.flatMap((work) => [work.code ?? "", work.title])
    ].join(" ").toLocaleLowerCase();
    return categoryMatches && (!normalizedQuery || text.includes(normalizedQuery));
  }), [actors, category, normalizedQuery]);
  useEffect(() => setPage(1), [category, deferredQuery]);
  const currentPage = Math.min(page, Math.max(1, Math.ceil(visibleActors.length / ACTOR_PAGE_SIZE)));
  const renderedActors = visibleActors.slice(
    (currentPage - 1) * ACTOR_PAGE_SIZE,
    currentPage * ACTOR_PAGE_SIZE
  );
  if (actors.length === 0) {
    return <Empty title="JAV 演员作品库为空" detail="扫描并接受带番号的作品后会出现在这里。国产/欧美请切换到「非 JAV 演员 / 作品」。" />;
  }
  return <>
    <DisplayFilterBar
      query={query}
      category={category}
      visible={visibleActors.length}
      total={actors.length}
      placeholder="搜索演员、别名、作品或番号"
      setQuery={setQuery}
      setCategory={setCategory}
    />
    {visibleActors.length === 0
      ? <Empty title="没有匹配的演员" detail="可以清空关键词或切换展示分类。" />
      : <><div className="actor-grid">{renderedActors.map((actor) => (
    <article className="actor-card" key={actor.name}>
      <div className="actor-profile">
        <div
          className={`actor-avatar${actor.image_url ? "" : " actor-avatar--empty"}`}
          data-initial={actor.image_url ? undefined : actorInitials(actor.name)}
          style={actor.image_url ? { backgroundImage: `url("${appUrl(actor.image_url)}")` } : undefined}
          role="img"
          aria-label={`${actor.name} 代表图片`}
        />
        <div className="actor-card-title">
          <div><span className="pill">{actor.categories.join(" / ")}</span><h2>{actor.name}</h2></div>
          <b>{actor.work_count}</b>
        </div>
      </div>
      {actor.aliases.length > 0 && <p>别名：{actor.aliases.join("、")}</p>}
      <div className="actor-works">{actor.works.slice(0, 8).map((work) => (
        <div className="actor-work" key={work.id}>
          <div
            className="actor-work-poster"
            style={work.image_url ? { backgroundImage: `url("${appUrl(work.image_url)}")` } : undefined}
          />
          <div><strong>{work.code ?? "无番号"}</strong><span>{work.title}</span></div>
        </div>
      ))}</div>
    </article>
  ))}</div><Pagination page={currentPage} total={visibleActors.length} pageSize={ACTOR_PAGE_SIZE} setPage={setPage} /></>}
  </>;
}

type ActorDraft = {
  previousName: string | null;
  name: string;
  aliases: string;
  groups: string;
  categories: Array<Exclude<DisplayCategory, "all">>;
  biography: string;
  notes: string;
};

const emptyActorDraft: ActorDraft = {
  previousName: null,
  name: "",
  aliases: "",
  groups: "independent",
  categories: ["Other"],
  biography: "",
  notes: ""
};

function NonJavActorsManager(props: {
  actors: NonJavActor[];
  busy: string | null;
  save: (previousName: string | null, payload: NonJavActorEditPayload) => Promise<void>;
  remove: (actor: NonJavActor) => Promise<void>;
  uploadImage: (actor: NonJavActor, file: File) => Promise<void>;
}) {
  const [query, setQuery] = useState("");
  const [category, setCategory] = useState<DisplayCategory>("all");
  const [group, setGroup] = useState<string>("all");
  const [draft, setDraft] = useState<ActorDraft | null>(null);
  const [page, setPage] = useState(1);
  const deferredQuery = useDeferredValue(query);
  const normalizedQuery = deferredQuery.trim().toLocaleLowerCase();
  const groupOptions = useMemo(() => {
    const counts = new Map<string, number>();
    for (const actor of props.actors) {
      for (const item of actor.groups) {
        counts.set(item, (counts.get(item) ?? 0) + 1);
      }
    }
    return [...counts.entries()]
      .sort((left, right) => right[1] - left[1] || left[0].localeCompare(right[0]))
      .map(([value, count]) => ({ value, count, label: groupLabel(value) }));
  }, [props.actors]);
  const visible = useMemo(() => {
    const filtered = props.actors.filter((actor) => {
      const categoryMatches = category === "all" || actor.categories.includes(category);
      const groupMatches = group === "all" || actor.groups.includes(group) || groupAliases(group).some((alias) => actor.groups.includes(alias));
      const text = [
        actor.name,
        ...actor.aliases,
        ...actor.match_names,
        ...actor.groups,
        actor.biography ?? "",
        actor.notes ?? "",
        ...actor.works.flatMap((work) => [work.title, work.code ?? "", work.studio ?? "", work.series ?? ""])
      ].join(" ").toLocaleLowerCase();
      return categoryMatches && groupMatches && (!normalizedQuery || text.includes(normalizedQuery));
    });
    return filtered.sort((left, right) => (right.work_count - left.work_count) || left.name.localeCompare(right.name));
  }, [props.actors, category, group, normalizedQuery]);
  useEffect(() => setPage(1), [category, group, deferredQuery]);
  const currentPage = Math.min(page, Math.max(1, Math.ceil(visible.length / NON_JAV_ACTOR_PAGE_SIZE)));
  const rendered = visible.slice(
    (currentPage - 1) * NON_JAV_ACTOR_PAGE_SIZE,
    currentPage * NON_JAV_ACTOR_PAGE_SIZE
  );
  const withWorks = props.actors.filter((actor) => actor.work_count > 0).length;

  function edit(actor: NonJavActor) {
    setDraft({
      previousName: actor.name,
      name: actor.name,
      aliases: actor.aliases.join(", "),
      groups: actor.groups.join(", "),
      categories: actor.categories,
      biography: actor.biography ?? "",
      notes: actor.notes ?? ""
    });
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!draft?.name.trim()) return;
    await props.save(draft.previousName, {
      name: draft.name.trim(),
      aliases: splitList(draft.aliases),
      groups: splitList(draft.groups),
      categories: draft.categories.length ? draft.categories : ["Other"],
      biography: draft.biography.trim() || null,
      notes: draft.notes.trim() || null
    });
    setDraft(null);
  }

  if (props.actors.length === 0) {
    return <Empty title="非 JAV 演员名单为空" detail="可新增演员，或到媒体库页使用「导入词库」合并其它服务器的 catalog bundle。" />;
  }

  return <>
    <div className="non-jav-heading">
      <div>
        <h2>非 JAV 演员与作品</h2>
        <p>名单来自本地策展；热门演员的作品已写入作品库，可在卡片中直接查看。名称/别名会参与目录识别。也可在「媒体库」页使用「导入词库」从其它服务器合并。</p>
      </div>
      <button onClick={() => setDraft({ ...emptyActorDraft })}>新增演员</button>
    </div>
    <DisplayFilterBar
      query={query}
      category={category}
      visible={visible.length}
      total={props.actors.length}
      placeholder="搜索演员、别名、作品、工作室或分组（探花/博主/麻豆）"
      setQuery={setQuery}
      setCategory={setCategory}
    />
    <div className="group-chip-bar" aria-label="分组筛选">
      <button type="button" className={group === "all" ? "active" : "ghost"} onClick={() => setGroup("all")}>全部分组</button>
      {GROUP_SHORTCUTS.map((item) => (
        <button
          key={item.value}
          type="button"
          className={group === item.value || groupAliases(item.value).includes(group) ? "active" : "ghost"}
          onClick={() => setGroup(item.value)}
        >{item.label}</button>
      ))}
      {groupOptions.filter((item) => !GROUP_SHORTCUT_VALUES.has(item.value)).slice(0, 8).map((item) => (
        <button key={item.value} type="button" className={group === item.value ? "active" : "ghost"} onClick={() => setGroup(item.value)}>
          {item.label} · {item.count}
        </button>
      ))}
      {group !== "all" && <button type="button" className="ghost" onClick={() => setGroup("all")}>清除分组</button>}
    </div>
    <p className="non-jav-stats">已有作品资料的演员 {withWorks} / {props.actors.length} · 当前显示 {visible.length}</p>
    {draft && <form className="actor-editor" onSubmit={(event) => void submit(event)}>
      <div className="actor-editor-title"><h2>{draft.previousName ? `编辑 ${draft.previousName}` : "新增非 JAV 演员"}</h2><button type="button" className="ghost" onClick={() => setDraft(null)}>取消</button></div>
      <label><span>规范名称</span><input required value={draft.name} onChange={(event) => setDraft({ ...draft, name: event.target.value })} /></label>
      <label><span>别名（逗号或换行分隔）</span><input value={draft.aliases} onChange={(event) => setDraft({ ...draft, aliases: event.target.value })} /></label>
      <label><span>分组</span><input value={draft.groups} onChange={(event) => setDraft({ ...draft, groups: event.target.value })} placeholder="madou, tanhua, onlyfans, western…" /></label>
      <fieldset><legend>分类</legend>{displayCategoryOptions.filter((item) => item.value !== "all").map((item) => {
        const value = item.value as Exclude<DisplayCategory, "all">;
        return <label key={value}><input type="checkbox" checked={draft.categories.includes(value)} onChange={(event) => setDraft({ ...draft, categories: event.target.checked ? [...draft.categories, value] : draft.categories.filter((current) => current !== value) })} />{item.label}</label>;
      })}</fieldset>
      <label className="wide"><span>简介</span><textarea value={draft.biography} onChange={(event) => setDraft({ ...draft, biography: event.target.value })} /></label>
      <label className="wide"><span>备注</span><textarea value={draft.notes} onChange={(event) => setDraft({ ...draft, notes: event.target.value })} /></label>
      <button disabled={props.busy?.startsWith("actor-")}>保存资料</button>
    </form>}
    {visible.length === 0
      ? <Empty title="没有匹配的非 JAV 演员" detail="可以清空关键词、分类或分组筛选，或新增一位演员。" />
      : <><div className="actor-grid non-jav-grid">{rendered.map((actor) => <article className="actor-card" key={actor.name}>
          <div className="actor-profile">
            <div className={`actor-avatar${actor.image_url ? "" : " actor-avatar--empty"}`} data-initial={actor.image_url ? undefined : actorInitials(actor.name)} style={actor.image_url ? { backgroundImage: `url("${appUrl(actor.image_url)}")` } : undefined} role="img" aria-label={`${actor.name} 头像`} />
            <div className="actor-card-title">
              <div>
                <span className="pill">{actor.categories.join(" / ")}</span>
                <h2>{actor.name}</h2>
                <small>{actor.groups.map(groupLabel).join(" / ") || "未分组"}</small>
              </div>
              <b title="关联作品数">{actor.work_count}</b>
            </div>
          </div>
          {actor.aliases.length > 0 && <p>别名：{actor.aliases.join("、")}</p>}
          {actor.biography && <p className="actor-biography">{actor.biography}</p>}
          {actor.notes && <p className="actor-notes">备注：{actor.notes}</p>}
          {actor.works.length > 0
            ? <div className="actor-works">{actor.works.slice(0, 6).map((work) => (
              <div className="actor-work" key={work.id}>
                <div
                  className="actor-work-poster"
                  style={work.image_url ? { backgroundImage: `url("${appUrl(work.image_url)}")` } : actor.image_url ? { backgroundImage: `url("${appUrl(actor.image_url)}")` } : undefined}
                />
                <div>
                  <strong>{work.code ?? work.studio ?? work.series ?? "无番号"}</strong>
                  <span>{work.title}</span>
                </div>
              </div>
            ))}</div>
            : <p className="actor-works-empty">暂无作品资料 · 扫描媒体并识别后会自动关联</p>}
          <div className="actor-actions">
            <button className="secondary" onClick={() => edit(actor)}>编辑</button>
            <label className="upload-button">上传头像<input type="file" accept="image/jpeg,image/png,image/webp,image/gif" onChange={(event) => { const file = event.target.files?.[0]; if (file) void props.uploadImage(actor, file); event.target.value = ""; }} /></label>
            <button className="ghost danger" disabled={props.busy === `actor-delete-${actor.name}`} onClick={() => void props.remove(actor)}>删除</button>
          </div>
        </article>)}</div><Pagination page={currentPage} total={visible.length} pageSize={NON_JAV_ACTOR_PAGE_SIZE} setPage={setPage} /></>}
  </>;
}

const GROUP_SHORTCUTS: ReadonlyArray<{ value: string; label: string }> = [
  { value: "tanhua", label: "探花" },
  { value: "blogger", label: "博主" },
  { value: "madou", label: "麻豆" },
  { value: "onlyfans", label: "OnlyFans" },
  { value: "western", label: "欧美" },
  { value: "swag", label: "SWAG" },
  { value: "korean", label: "韩国" }
];
const GROUP_SHORTCUT_VALUES = new Set(GROUP_SHORTCUTS.map((item) => item.value));

function groupAliases(value: string): string[] {
  if (value === "tanhua") return ["tanhua", "91-tanhua", "x-tanhua", "tandian", "yuepao"];
  if (value === "blogger") return ["blogger", "twitter", "x", "x-xingba", "xingba"];
  if (value === "madou") return ["madou", "91-studio", "tianmei", "jelly", "xingkong"];
  return [value];
}

function groupLabel(value: string): string {
  const hit = GROUP_SHORTCUTS.find((item) => item.value === value || groupAliases(item.value).includes(value));
  if (hit) {
    if (hit.value === value) return hit.label;
    return `${hit.label}/${value}`;
  }
  return value;
}

function TaskHistory({ tasks }: { tasks: TaskRun[] }) {
  if (tasks.length === 0) {
    return <Empty title="还没有运行记录" detail="扫描、番号查询、图片缓存和批量整理完成后会记录在这里。" />;
  }
  return <div className="task-list">{tasks.map((task) => (
    <article key={task.id}>
      <div>
        <span className={`task-state ${task.status}`}>{task.status}</span>
        <h2>{task.kind}</h2>
        <p>{task.scope}</p>
      </div>
      <code>{JSON.stringify(task.summary)}</code>
      <time>{new Date(task.created_at).toLocaleString()}</time>
    </article>
  ))}</div>;
}

function Nav(props: { active: boolean; onClick: () => void; label: string; count: number }) {
  return (
    <button className={props.active ? "nav active" : "nav"} onClick={props.onClick}>
      <span>{props.label}</span><b>{props.count}</b>
    </button>
  );
}

function Inbox(props: {
  assets: Asset[];
  libraries: Library[];
  nonJavActors: NonJavActor[];
  candidates: Record<string, Candidate[]>;
  busy: string | null;
  identify: (asset: Asset, payload: { title?: string; source_url?: string }) => Promise<void>;
  createManualCandidate: (asset: Asset, title: string | undefined) => Promise<void>;
  loadCandidates: (assetId: string) => Promise<void>;
  accept: (candidate: Candidate) => Promise<void>;
  assignDirectoryActor: (
    asset: Asset,
    actor: string,
    category: Exclude<DisplayCategory, "all">,
    directory: string
  ) => Promise<void>;
}) {
  const [query, setQuery] = useState("");
  const [category, setCategory] = useState<DisplayCategory>("all");
  const [page, setPage] = useState(1);
  const deferredQuery = useDeferredValue(query);
  const normalizedQuery = deferredQuery.trim().toLocaleLowerCase();
  const visibleAssets = useMemo(() => props.assets.filter((asset) => {
    const categoryMatches = category === "all" || asset.hints.category === category;
    const text = [
      asset.path,
      asset.hints.code ?? "",
      asset.hints.title ?? "",
      asset.hints.studio ?? "",
      asset.hints.series ?? "",
      ...asset.hints.actors
    ].join(" ").toLocaleLowerCase();
    return categoryMatches && (!normalizedQuery || text.includes(normalizedQuery));
  }), [props.assets, category, normalizedQuery]);
  useEffect(() => setPage(1), [category, deferredQuery]);
  const directoryGroups = useMemo(() => groupAssetsByDirectory(visibleAssets), [visibleAssets]);
  const currentPage = Math.min(page, Math.max(1, Math.ceil(directoryGroups.length / INBOX_PAGE_SIZE)));
  const renderedGroups = directoryGroups.slice(
    (currentPage - 1) * INBOX_PAGE_SIZE,
    currentPage * INBOX_PAGE_SIZE
  );
  if (props.assets.length === 0) {
    return <Empty title="没有待确认文件" detail="扫描媒体库后，无法自动确认的影片会出现在这里。" />;
  }
  return <>
    <DisplayFilterBar
      query={query}
      category={category}
      visible={visibleAssets.length}
      total={props.assets.length}
      placeholder="搜索文件名、番号、标题、演员或目录"
      setQuery={setQuery}
      setCategory={setCategory}
    />
    {visibleAssets.length === 0
      ? <Empty title="没有匹配的待确认文件" detail="可以清空关键词或切换展示分类。" />
      : <><datalist id="non-jav-actor-options">{props.nonJavActors.map((actor) => <option key={actor.name} value={actor.name} />)}</datalist><div className="directory-list">{renderedGroups.map((group) => (
    <AssetDirectoryGroup
      key={group.directory}
      group={group}
      libraryRoot={props.libraries.find((library) => library.id === group.assets[0]?.library_id)?.root_path}
      candidates={props.candidates}
      busy={props.busy}
      identify={props.identify}
      createManualCandidate={props.createManualCandidate}
      loadCandidates={props.loadCandidates}
      accept={props.accept}
      assignDirectoryActor={props.assignDirectoryActor}
    />
  ))}</div><Pagination page={currentPage} total={directoryGroups.length} pageSize={INBOX_PAGE_SIZE} unit="个目录" setPage={setPage} /></>}
  </>;
}

type AssetDirectoryGroupData = {
  directory: string;
  name: string;
  assets: Asset[];
};

function AssetDirectoryGroup(props: {
  group: AssetDirectoryGroupData;
  libraryRoot?: string;
  candidates: Record<string, Candidate[]>;
  busy: string | null;
  identify: (asset: Asset, payload: { title?: string; source_url?: string }) => Promise<void>;
  createManualCandidate: (asset: Asset, title: string | undefined) => Promise<void>;
  loadCandidates: (assetId: string) => Promise<void>;
  accept: (candidate: Candidate) => Promise<void>;
  assignDirectoryActor: (
    asset: Asset,
    actor: string,
    category: Exclude<DisplayCategory, "all">,
    directory: string
  ) => Promise<void>;
}) {
  const [filePage, setFilePage] = useState(1);
  const actorNames = [...new Set(props.group.assets.flatMap((asset) => asset.hints.actors))];
  const noCodeAssets = props.group.assets.filter((asset) => !asset.hints.code);
  const codeCount = props.group.assets.length - noCodeAssets.length;
  const categories = [...new Set(props.group.assets.map((asset) => asset.hints.category))];
  const onlyCategory = categories.length === 1 ? categories[0] : undefined;
  const [directoryActor, setDirectoryActor] = useState(actorNames.length === 1 ? actorNames[0] ?? "" : "");
  const [directoryCategory, setDirectoryCategory] = useState<Exclude<DisplayCategory, "all">>(
    onlyCategory && onlyCategory !== "Japan" ? onlyCategory : "Other"
  );
  const directoryOptions = directoryAncestors(
    props.group.assets[0]?.path ?? "",
    props.libraryRoot
  );
  const [selectedDirectory, setSelectedDirectory] = useState(props.group.directory);
  const currentFilePage = Math.min(
    filePage,
    Math.max(1, Math.ceil(props.group.assets.length / DIRECTORY_FILE_PAGE_SIZE))
  );
  const renderedAssets = props.group.assets.slice(
    (currentFilePage - 1) * DIRECTORY_FILE_PAGE_SIZE,
    currentFilePage * DIRECTORY_FILE_PAGE_SIZE
  );
  const representative = noCodeAssets[0];

  return <section className="directory-group">
    <div className="directory-head">
      <div className="directory-title">
        <span className="directory-icon">⌁</span>
        <div><h2>{props.group.name}</h2><p title={props.group.directory}>{props.group.directory}</p></div>
      </div>
      <div className="directory-stats">
        <span><b>{props.group.assets.length}</b> 个文件</span>
        {codeCount > 0 && <span><b>{codeCount}</b> 个有番号</span>}
        {noCodeAssets.length > 0 && <span><b>{noCodeAssets.length}</b> 个无番号</span>}
        {actorNames.map((actor) => <span className="actor-clue" key={actor}>人物 · {actor}</span>)}
      </div>
    </div>
    {representative && <div className="directory-confirm">
      <div><strong>确认多级目录的演员</strong><small>选择演员所在层级后，会处理该目录及全部子目录；子目录名会进入作品标题和标签。</small></div>
      <input list="non-jav-actor-options" value={directoryActor} onChange={(event) => setDirectoryActor(event.target.value)} placeholder="选择或输入演员" />
      <select value={selectedDirectory} onChange={(event) => setSelectedDirectory(event.target.value)}>
        {directoryOptions.map((option, index) => (
          <option key={option} value={option}>{index === 0 ? "当前目录" : `向上 ${index} 级`} · {fileName(option)}</option>
        ))}
      </select>
      <select value={directoryCategory} onChange={(event) => setDirectoryCategory(event.target.value as Exclude<DisplayCategory, "all">)}>
        {displayCategoryOptions.filter((item) => item.value !== "all" && item.value !== "Japan").map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}
      </select>
      <button className="secondary" disabled={!directoryActor.trim() || props.busy === `directory-${representative.id}`} onClick={() => void props.assignDirectoryActor(representative, directoryActor.trim(), directoryCategory, selectedDirectory)}>应用到所选目录及子目录</button>
    </div>}
    <div className="directory-assets">{renderedAssets.map((asset) => <AssetReview
      key={asset.id}
      asset={asset}
      candidates={props.candidates[asset.id] ?? []}
      busy={props.busy}
      identify={props.identify}
      createManualCandidate={props.createManualCandidate}
      loadCandidates={props.loadCandidates}
      accept={props.accept}
    />)}</div>
    <Pagination
      page={currentFilePage}
      total={props.group.assets.length}
      pageSize={DIRECTORY_FILE_PAGE_SIZE}
      unit="个文件"
      setPage={setFilePage}
    />
  </section>;
}

function AssetReview(props: {
  asset: Asset;
  candidates: Candidate[];
  busy: string | null;
  identify: (asset: Asset, payload: { title?: string; source_url?: string }) => Promise<void>;
  createManualCandidate: (asset: Asset, title: string | undefined) => Promise<void>;
  loadCandidates: (assetId: string) => Promise<void>;
  accept: (candidate: Candidate) => Promise<void>;
}) {
  const [manual, setManual] = useState("");
  const isUrl = /^https?:\/\//i.test(manual);
  return (
    <article className="asset">
      <div className="asset-head">
        <div>
          <span className={`pill ${props.asset.hints.family}`}>{props.asset.hints.family}</span>
          <h2>{fileName(props.asset.path)}</h2>
          <p>{props.asset.hints.code ?? props.asset.hints.title ?? "未提取身份"}</p>
          <div className="hint-line">
            {mediaSummary(props.asset) && <span>质量 · {mediaSummary(props.asset)}</span>}
            {props.asset.hints.media_locator && <span>STRM · {props.asset.hints.media_locator}</span>}
            {props.asset.hints.studio && <span>片商 · {props.asset.hints.studio}</span>}
            {props.asset.hints.series && <span>系列 · {props.asset.hints.series}</span>}
            {props.asset.hints.actors.map((actor) => <span key={actor}>人物 · {actor}</span>)}
          </div>
        </div>
        <button disabled={props.busy === props.asset.id} onClick={() => void props.identify(props.asset, {})}>
          自动识别
        </button>
      </div>
      <div className="manual">
        <input
          value={manual}
          onChange={(event) => setManual(event.target.value)}
          placeholder="输入标题或粘贴详情页 URL"
        />
        <button
          className="secondary"
          disabled={!manual.trim() || props.busy === props.asset.id}
          onClick={() => void props.identify(props.asset, isUrl ? { source_url: manual } : { title: manual })}
        >
          指定查询
        </button>
        <button
          className="secondary"
          disabled={props.busy === `manual-${props.asset.id}`}
          onClick={() => void props.createManualCandidate(props.asset, manual.trim() || undefined)}
        >
          本地候选
        </button>
        <button className="ghost" onClick={() => void props.loadCandidates(props.asset.id)}>候选</button>
      </div>
      {props.candidates.length > 0 && <div className="candidate-grid">
        {props.candidates.map((candidate) => (
          <div className="candidate" key={candidate.id}>
            <div>
              <span>{candidate.record.provider}</span>
              <b>{Math.round(candidate.score * 100)}%</b>
            </div>
            <h3>{candidate.record.title}</h3>
            <p>{[candidate.record.code, candidate.record.studio, candidate.record.release_date].filter(Boolean).join(" · ")}</p>
            <button disabled={props.busy === candidate.id} onClick={() => void props.accept(candidate)}>接受</button>
          </div>
        ))}
      </div>}
    </article>
  );
}

function Works(props: {
  works: Work[];
  busy: string | null;
  refreshMetadata: (work: Work) => Promise<void>;
  downloadArtwork: (work: Work) => Promise<void>;
  lookupWork: (code: string) => Promise<void>;
  translateWorks: () => Promise<void>;
}) {
  const { works } = props;
  const [code, setCode] = useState("");
  const [query, setQuery] = useState("");
  const [category, setCategory] = useState<DisplayCategory>("all");
  const [page, setPage] = useState(1);
  const categories = ["Japan", "China", "Korea", "Europe", "Other"] as const;
  const deferredQuery = useDeferredValue(query);
  const normalizedQuery = deferredQuery.trim().toLocaleLowerCase();
  const visibleWorks = useMemo(() => works.filter((work) => {
    const categoryMatches = category === "all" || work.category === category;
    const text = [
      work.title,
      work.original_title ?? "",
      work.primary_code ?? "",
      work.studio ?? "",
      work.series ?? "",
      ...work.actors,
      ...work.tags
    ].join(" ").toLocaleLowerCase();
    return categoryMatches && (!normalizedQuery || text.includes(normalizedQuery));
  }), [works, category, normalizedQuery]);
  useEffect(() => setPage(1), [category, deferredQuery]);
  const currentPage = Math.min(page, Math.max(1, Math.ceil(visibleWorks.length / WORK_PAGE_SIZE)));
  const renderedWorks = visibleWorks.slice(
    (currentPage - 1) * WORK_PAGE_SIZE,
    currentPage * WORK_PAGE_SIZE
  );
  return <>
    <form className="work-lookup" onSubmit={(event) => {
      event.preventDefault();
      if (code.trim()) void props.lookupWork(code.trim());
    }}>
      <div><h2>按番号获取信息</h2><p>无需先有本地文件；多个命中来源会逐字段聚合并保留来源。</p></div>
      <input value={code} onChange={(event) => setCode(event.target.value)} placeholder="例如 SONE-118" />
      <button disabled={!code.trim() || props.busy === "lookup-work"}>查询并建档</button>
      <button
        type="button"
        className="secondary"
        disabled={props.busy === "translate-works"}
        onClick={() => void props.translateWorks()}
      >补翻译标题</button>
    </form>
    {works.length > 0 && <DisplayFilterBar
      query={query}
      category={category}
      visible={visibleWorks.length}
      total={works.length}
      placeholder="搜索标题、番号、演员、片商或标签"
      setQuery={setQuery}
      setCategory={setCategory}
    />}
    {works.length === 0
      ? <Empty title="作品库为空" detail="可按番号查询 JAV，或依赖非 JAV 种子作品 / 扫描媒体库后接受候选。打开演员库可查看已写入的非 JAV 作品。" />
      : visibleWorks.length === 0
        ? <Empty title="没有匹配的作品" detail="可以清空关键词或切换展示分类。" />
        : <div className="work-sections">{categories.map((sectionCategory) => {
    const categoryWorks = renderedWorks.filter((work) => work.category === sectionCategory);
    if (categoryWorks.length === 0) return null;
    return <section className="work-section" key={sectionCategory}>
      <div className="work-section-title"><h2>{sectionCategory}</h2><span>{categoryWorks.length}</span></div>
      <div className="work-grid">{categoryWorks.map((work) => {
    return (
      <article className="work" key={work.id}>
        <div
          className="poster"
          style={work.image_url ? { backgroundImage: `url("${appUrl(work.image_url)}")` } : undefined}
          role="img"
          aria-label={`${work.primary_code ?? work.title} 海报`}
        />
        <div>
          <span className="pill">{work.category}</span>
          <h2>{work.title}</h2>
          {work.original_title && work.original_title !== work.title && (
            <p className="original-title">原文：{work.original_title}</p>
          )}
          <p>{[work.primary_code, work.studio, work.release_date].filter(Boolean).join(" · ")}</p>
          <div className="tags">{work.actor_entities.slice(0, 4).map((actor) => (
            <span key={actor.id}>{actor.name}</span>
          ))}</div>
          {work.primary_code && (
            <button
              className="secondary work-refresh"
              disabled={props.busy === `work-${work.id}`}
              onClick={() => void props.refreshMetadata(work)}
            >刷新元数据</button>
          )}
          {work.artwork.length > 0 && (
            <button
              className="ghost work-refresh"
              disabled={props.busy === `artwork-${work.id}`}
              onClick={() => void props.downloadArtwork(work)}
            >缓存图片</button>
          )}
        </div>
      </article>
    );
      })}</div>
    </section>;
      })}</div>}<Pagination page={currentPage} total={visibleWorks.length} pageSize={WORK_PAGE_SIZE} setPage={setPage} />
  </>;
}

function Libraries(props: {
  libraries: Library[];
  busy: string | null;
  run: (key: string, action: () => Promise<void>) => Promise<void>;
  report: (message: string) => void;
}) {
  const [name, setName] = useState("");
  const [path, setPath] = useState("");
  const [recognitionScope, setRecognitionScope] = useState<"all" | "jav_only">("all");
  const [displayQuery, setDisplayQuery] = useState("");
  const [displayScope, setDisplayScope] = useState<"any" | "all" | "jav_only">("any");
  const normalizedDisplayQuery = displayQuery.trim().toLocaleLowerCase();
  const visibleLibraries = props.libraries.filter((library) => {
    const scopeMatches = displayScope === "any" || library.recognition_scope === displayScope;
    const text = `${library.name} ${library.root_path}`.toLocaleLowerCase();
    return scopeMatches && (!normalizedDisplayQuery || text.includes(normalizedDisplayQuery));
  });
  function submit(event: FormEvent) {
    event.preventDefault();
    void props.run("create-library", async () => {
      await api.createLibrary({ name, root_path: path, recognition_scope: recognitionScope });
      setName("");
      setPath("");
    });
  }
  return (
    <>
      <form className="library-form" onSubmit={submit}>
        <input value={name} onChange={(event) => setName(event.target.value)} placeholder="媒体库名称" required />
        <input
          value={path}
          onChange={(event) => setPath(event.target.value)}
          placeholder="本地路径、Z:\\媒体 或 \\\\server\\share"
          required
        />
        <select
          value={recognitionScope}
          onChange={(event) => setRecognitionScope(event.target.value as "all" | "jav_only")}
          aria-label="新媒体库处理范围"
        >
          <option value="all">处理全部内容</option>
          <option value="jav_only">仅处理 JAV</option>
        </select>
        <button disabled={props.busy === "create-library"}>添加媒体库</button>
      </form>
      <p className="library-note">分类按每个文件自动识别：番号优先，其次目录关键词和中日韩/英文脚本。支持网络磁盘和 UNC 共享；写入需要共享目录有写权限。</p>
      {props.libraries.length > 0 && <div className="display-filter" aria-label="媒体库展示筛选">
        <input
          value={displayQuery}
          onChange={(event) => setDisplayQuery(event.target.value)}
          placeholder="搜索媒体库名称或路径"
          aria-label="媒体库展示搜索"
        />
        <select
          value={displayScope}
          onChange={(event) => setDisplayScope(event.target.value as "any" | "all" | "jav_only")}
          aria-label="媒体库处理范围筛选"
        >
          <option value="any">全部处理范围</option>
          <option value="all">全部内容模式</option>
          <option value="jav_only">仅 JAV 模式</option>
        </select>
        <span>显示 {visibleLibraries.length} / {props.libraries.length}</span>
        {(displayQuery || displayScope !== "any") && <button
          type="button"
          className="ghost"
          onClick={() => {
            setDisplayQuery("");
            setDisplayScope("any");
          }}
        >清空筛选</button>}
      </div>}
      {props.libraries.length > 0 && visibleLibraries.length === 0
        ? <Empty title="没有匹配的媒体库" detail="可以清空关键词或切换处理范围筛选。" />
        : <div className="library-list">{visibleLibraries.map((library) => (
        <article key={library.id}>
          <div className="library-head">
            <div>
              <h2>{library.name} · 自动分类</h2>
              <p>{library.root_path}</p>
              <label>
                <span>处理范围</span>
                <select
                  value={library.recognition_scope}
                  disabled={props.busy === `scope-${library.id}`}
                  onChange={(event) => void props.run(`scope-${library.id}`, async () => {
                    const recognition_scope = event.target.value as "all" | "jav_only";
                    await api.updateLibrary(library.id, { recognition_scope });
                    props.report(recognition_scope === "jav_only" ? "该媒体库已切换为仅识别 JAV" : "该媒体库已恢复识别全部内容");
                  })}
                >
                  <option value="all">全部内容</option>
                  <option value="jav_only">仅 JAV</option>
                </select>
              </label>
            </div>
            <button
              disabled={props.busy === `scan-${library.id}`}
              onClick={() => void props.run(`scan-${library.id}`, async () => {
                const result = await api.scan(library.id);
                const errorSummary = result.errors.length > 0
                  ? `；${result.errors.length} 个路径失败：${result.errors.slice(0, 2).join("；")}`
                  : "";
                props.report(
                  `新增 ${result.discovered}，更新 ${result.updated}，已识别 ${result.identified}，` +
                  `待确认 ${result.queued}，` +
                  `过滤 ${result.filtered}，` +
                  `跳过 ${result.skipped}${errorSummary}`
                );
              })}
            >扫描</button>
            <button
              className="secondary"
              disabled={props.busy === `identify-${library.id}`}
              onClick={() => void props.run(`identify-${library.id}`, async () => {
                const result = await api.identifyLibrary(library.id);
                props.report(
                  `批量处理：番号 ${result.code_queries}、标题 ${result.title_queries}，` +
                  `在线识别 ${result.online_identified}、作品库复用 ${result.catalog_reused}、` +
                  `本地优化 ${result.local_optimized}，` +
                  `待确认 ${result.unresolved}，来源失败 ${result.provider_failures} 次，` +
                  `范围外跳过 ${result.scope_skipped}，剩余 ${result.remaining_identities} 组`
                );
              })}
            >识别并优化媒体</button>
            <button
              className="secondary"
              disabled={props.busy === `screenshots-${library.id}`}
              onClick={() => void props.run(`screenshots-${library.id}`, async () => {
                const result = await api.generateScreenshots(library.id);
                const errorSummary = result.errors.length > 0
                  ? `；失败示例：${result.errors.slice(0, 2).join("；")}`
                  : "";
                props.report(
                  `非 JAV 截图：生成 ${result.generated}/${result.attempted}，` +
                  `STRM 免截图 ${result.skipped_strm}，已缓存 ${result.skipped_cached}，` +
                  `严格校验未通过 ${result.skipped_untrusted}，` +
                  `失败 ${result.failed}${errorSummary}`
                );
              })}
            >生成非 JAV 截图</button>
          </div>
          <LibraryOrganizer library={library} busy={props.busy} run={props.run} report={props.report} />
        </article>
      ))}</div>}
      <CatalogImportEditor report={props.report} busy={props.busy} run={props.run} />
      <ProviderDiagnostics />
      <FilterWordsEditor />
      <AliasEditor />
    </>
  );
}

function LibraryOrganizer(props: {
  library: Library;
  busy: string | null;
  run: (key: string, action: () => Promise<void>) => Promise<void>;
  report: (message: string) => void;
}) {
  const [mode, setMode] = useState<OrganizePayload["mode"]>("sidecar");
  const [targetRoot, setTargetRoot] = useState("");
  const [template, setTemplate] = useState(props.library.organize_template);
  const [nfoPolicy, setNfoPolicy] = useState<"error" | "skip" | "replace">("replace");
  const [plan, setPlan] = useState<BatchPlan | null>(null);
  const key = `organize-${props.library.id}`;
  const payload: OrganizePayload = {
    mode,
    target_root: mode === "sidecar" ? null : targetRoot,
    template: mode === "sidecar" ? null : template
  };

  function preview() {
    setPlan(null);
    void props.run(`${key}-plan`, async () => {
      const next = await api.planLibrary(props.library.id, payload);
      setPlan(next);
      props.report(`整理预览完成：${next.asset_count} 个文件，${next.conflict_count} 个冲突`);
    });
  }

  function apply() {
    if (!plan || !window.confirm(`确认执行 ${plan.asset_count} 个文件的整理计划？`)) return;
    void props.run(`${key}-apply`, async () => {
      const result = await api.applyLibraryPlan(props.library.id, {
        ...payload,
        token: plan.token,
        nfo_policy: nfoPolicy
      });
      setPlan(null);
      props.report(`整理完成：成功 ${result.succeeded}，失败 ${result.failed}`);
    });
  }

  return <section className="organizer">
    <div className="organizer-grid">
      <label><span>输出模式</span><select value={mode} onChange={(event) => {
        setMode(event.target.value as OrganizePayload["mode"]);
        setPlan(null);
      }}>
        <option value="sidecar">当前文件夹 + movie.nfo + 图片</option>
        <option value="copy">复制到新路径 + NFO + 图片</option>
        <option value="move">移动到新路径 + NFO + 图片 + 清理空目录</option>
        <option value="hardlink">硬链接到新路径 + NFO + 图片</option>
        <option value="symlink">软链接到新路径 + NFO + 图片</option>
      </select></label>
      {mode !== "sidecar" && <>
        <label><span>目标根目录</span><input value={targetRoot} onChange={(event) => {
          setTargetRoot(event.target.value);
          setPlan(null);
        }} placeholder="D:\\Media 或 \\\\server\\share\\Media" /></label>
        <label className="template-field"><span>目录模板</span><input value={template} onChange={(event) => {
          setTemplate(event.target.value);
          setPlan(null);
        }} /></label>
      </>}
      <label><span>已有 NFO</span><select value={nfoPolicy} onChange={(event) =>
        setNfoPolicy(event.target.value as "error" | "skip" | "replace")
      }>
        <option value="replace">覆盖</option>
        <option value="skip">跳过</option>
        <option value="error">视为冲突</option>
      </select></label>
      <div className="organizer-actions">
        <button className="secondary" disabled={props.busy !== null} onClick={preview}>预览计划</button>
        <button disabled={props.busy !== null || plan === null} onClick={apply}>确认执行</button>
      </div>
    </div>
    {plan && <div className="plan-preview">
      <strong>{plan.asset_count} 个文件 / {plan.operation_count} 项操作 / {plan.conflict_count} 个现存目标</strong>
      {plan.samples.flatMap((item) => item.operations).slice(0, 8).map((operation, index) =>
        <p key={`${operation.destination}-${index}`} className={operation.conflict ? "conflict" : ""}>
          {operation.kind} → {operation.destination}{operation.conflict ? "（已存在）" : ""}
        </p>
      )}
      {plan.truncated && <small>这里只显示前 50 个文件的部分操作，执行仍覆盖完整计划。</small>}
    </div>}
  </section>;
}


function CatalogImportEditor(props: {
  busy: string | null;
  run: (key: string, action: () => Promise<void>) => Promise<void>;
  report: (message: string) => void;
}) {
  const [path, setPath] = useState("");
  const [dryRun, setDryRun] = useState(false);
  const [actorsOnly, setActorsOnly] = useState(false);
  const [worksOnly, setWorksOnly] = useState(false);
  const [status, setStatus] = useState("从其它服务器合并词库与策展作品；不会覆盖媒体库、扫描状态或整库数据库。");

  function summarize(result: Awaited<ReturnType<typeof api.importCatalogFromPath>>) {
    return (
      `${result.dry_run ? "预览" : "已合并"} ${result.bundle_kind}：` +
      `演员 +${result.actors_added}/更新 ${result.actors_updated}，` +
      `头像 ${result.actor_images_copied}，` +
      `作品 +${result.works_created}/更新 ${result.works_updated}，` +
      `海报文件 ${result.artwork_copied}，` +
      `正式作品 ${result.formal_works_imported}` +
      (result.notes.length ? `；${result.notes.join("；")}` : "")
    );
  }

  return (
    <section className="alias-editor">
      <div>
        <h2>导入词库</h2>
        <p>{status}</p>
      </div>
      <label><span>服务器本地路径（目录 / .zip / .tar.gz）</span>
        <input value={path} onChange={(event) => setPath(event.target.value)} placeholder="/data/import/catalog-bundle" />
      </label>
      <div className="group-chip-bar" aria-label="导入选项">
        <label><input type="checkbox" checked={dryRun} onChange={(event) => setDryRun(event.target.checked)} />仅预览</label>
        <label><input type="checkbox" checked={actorsOnly} onChange={(event) => { setActorsOnly(event.target.checked); if (event.target.checked) setWorksOnly(false); }} />仅演员</label>
        <label><input type="checkbox" checked={worksOnly} onChange={(event) => { setWorksOnly(event.target.checked); if (event.target.checked) setActorsOnly(false); }} />仅作品</label>
      </div>
      <div className="organizer-actions">
        <button
          disabled={props.busy !== null || !path.trim()}
          onClick={() => void props.run("catalog-import-path", async () => {
            const result = await api.importCatalogFromPath({
              path: path.trim(),
              dry_run: dryRun,
              actors_only: actorsOnly,
              works_only: worksOnly
            });
            const message = summarize(result);
            setStatus(message);
            props.report(message);
          })}
        >从路径合并导入</button>
        <label className="ghost" style={{ display: "inline-flex", alignItems: "center", gap: 8 }}>
          上传压缩包
          <input
            type="file"
            accept=".zip,.tar.gz,.tgz,application/zip,application/gzip"
            disabled={props.busy !== null}
            onChange={(event) => {
              const file = event.target.files?.[0];
              event.target.value = "";
              if (!file) return;
              void props.run("catalog-import-upload", async () => {
                const result = await api.importCatalogUpload(file, {
                  dry_run: dryRun,
                  actors_only: actorsOnly,
                  works_only: worksOnly
                });
                const message = summarize(result);
                setStatus(message);
                props.report(message);
              });
            }}
          />
        </label>
      </div>
    </section>
  );
}

function ProviderDiagnostics() {
  const [code, setCode] = useState("SONE-118");
  const [status, setStatus] = useState("输入一个已知番号，实际请求所有可用来源。这里只检测，不修改作品库。");
  const [running, setRunning] = useState(false);

  async function diagnose() {
    setRunning(true);
    try {
      const result = await api.diagnoseProviders(code);
      const items = result.diagnostics.map((item) => {
        const state = item.status === "success"
          ? `精确命中 ${item.accepted} 条（返回 ${item.records} 条）`
          : item.status === "failed"
            ? `失败 / ${item.reason ?? "未知原因"}`
            : item.status === "no_result"
              ? "可访问，但无结果"
              : item.status === "candidates"
                ? `返回 ${item.records} 条，但没有精确匹配`
              : item.status === "not_configured"
                ? "未配置"
                : "不适用于番号";
        return `${item.provider}: ${state}`;
      });
      setStatus(`${result.proxy_configured ? "已配置代理" : "未配置代理"}，重试 ${result.retries} 次；${items.join("；")}`);
    } catch (error) {
      setStatus(errorMessage(error));
    } finally {
      setRunning(false);
    }
  }

  return <section className="provider-diagnostics">
    <div><h2>在线来源诊断</h2><p>{status}</p></div>
    <input value={code} onChange={(event) => setCode(event.target.value)} aria-label="诊断番号" />
    <button disabled={running || !code.trim()} onClick={() => void diagnose()}>测试来源</button>
  </section>;
}

function FilterWordsEditor() {
  const [value, setValue] = useState("");
  const [status, setStatus] = useState("正在读取规则…");
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    void api.filterWords()
      .then((rules) => {
        setValue(rules.words.join("\n"));
        setStatus("每行一个普通关键词；匹配相对路径，不支持正则，空行会被忽略。");
      })
      .catch((error: unknown) => setStatus(errorMessage(error)));
  }, []);

  async function save() {
    setSaving(true);
    try {
      const words = value.split(/\r?\n/).map((word) => word.trim()).filter(Boolean);
      const saved = await api.saveFilterWords({ words });
      setValue(saved.words.join("\n"));
      setStatus(`已保存 ${saved.words.length} 条规则，下一次扫描时生效。`);
    } catch (error) {
      setStatus(errorMessage(error));
    } finally {
      setSaving(false);
    }
  }

  return (
    <section className="alias-editor filter-editor">
      <div>
        <h2>垃圾文件过滤词</h2>
        <p>{status}</p>
      </div>
      <textarea
        value={value}
        onChange={(event) => setValue(event.target.value)}
        spellCheck={false}
        aria-label="垃圾文件过滤词"
      />
      <button disabled={saving} onClick={() => void save()}>保存规则</button>
    </section>
  );
}

function AliasEditor() {
  const [value, setValue] = useState("");
  const [status, setStatus] = useState("正在读取规则…");
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    void api.identityAliases()
      .then((rules) => {
        setValue(JSON.stringify(rules, null, 2));
        setStatus("文件名和最近三级目录会使用这些别名作为片商、系列和人物线索。");
      })
      .catch((error: unknown) => setStatus(errorMessage(error)));
  }, []);

  async function save() {
    setSaving(true);
    try {
      const parsed: unknown = JSON.parse(value);
      const rules: IdentityAliases = identityAliasesSchema.parse(parsed);
      const saved = await api.saveIdentityAliases(rules);
      setValue(JSON.stringify(saved, null, 2));
      setStatus("别名规则已保存，下一次扫描时生效。");
    } catch (error) {
      setStatus(errorMessage(error));
    } finally {
      setSaving(false);
    }
  }

  return (
    <section className="alias-editor">
      <div>
        <h2>无番号别名规则</h2>
        <p>{status}</p>
      </div>
      <textarea
        value={value}
        onChange={(event) => setValue(event.target.value)}
        spellCheck={false}
        aria-label="无番号别名规则 JSON"
      />
      <button disabled={saving || !value.trim()} onClick={() => void save()}>保存规则</button>
    </section>
  );
}

function Empty({ title, detail }: { title: string; detail: string }) {
  return <div className="empty"><div>◇</div><h2>{title}</h2><p>{detail}</p></div>;
}

function fileName(path: string) {
  return path.split(/[\\/]/).at(-1) ?? path;
}

function directoryAncestors(mediaPath: string, rootPath?: string): string[] {
  const result: string[] = [];
  const normalizedRoot = rootPath?.replace(/[\\/]+$/, "").toLocaleLowerCase();
  let current = parentDirectory(mediaPath);
  while (current) {
    if (normalizedRoot && !current.toLocaleLowerCase().startsWith(normalizedRoot)) break;
    result.push(current);
    if (normalizedRoot && current.toLocaleLowerCase() === normalizedRoot) break;
    const parent = parentDirectory(current);
    if (!parent || parent === current) break;
    current = parent;
  }
  return result;
}

function parentDirectory(path: string): string | null {
  const trimmed = path.replace(/[\\/]+$/, "");
  const separator = Math.max(trimmed.lastIndexOf("\\"), trimmed.lastIndexOf("/"));
  if (separator <= 2) return null;
  return trimmed.slice(0, separator);
}

function groupAssetsByDirectory(assets: Asset[]): AssetDirectoryGroupData[] {
  const groups = new Map<string, Asset[]>();
  for (const asset of assets) {
    const separator = Math.max(asset.path.lastIndexOf("\\"), asset.path.lastIndexOf("/"));
    const directory = separator >= 0 ? asset.path.slice(0, separator) : asset.path;
    const current = groups.get(directory);
    if (current) current.push(asset);
    else groups.set(directory, [asset]);
  }
  return [...groups.entries()]
    .map(([directory, groupedAssets]) => ({
      directory,
      name: fileName(directory),
      assets: groupedAssets.sort((left, right) => fileName(left.path).localeCompare(fileName(right.path)))
    }))
    .sort((left, right) => left.directory.localeCompare(right.directory));
}

function splitList(value: string) {
  return [...new Set(value.split(/[,，;；\n]/).map((item) => item.trim()).filter(Boolean))];
}

function mediaSummary(asset: Asset) {
  const info = asset.media_info;
  return [
    info.quality_label,
    info.video_codec?.toUpperCase(),
    info.hdr_format,
    info.audio_codec?.toUpperCase()
  ].filter(Boolean).join(" · ");
}

function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : "发生未知错误";
}
