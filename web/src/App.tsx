import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";

import { api } from "./api";
import type { OrganizePayload } from "./api";
import { identityAliasesSchema } from "./model";
import type { Asset, BatchPlan, Candidate, IdentityAliases, Library, TaskRun, Work } from "./model";

type View = "inbox" | "works" | "libraries" | "tasks";

export function App() {
  const [view, setView] = useState<View>("inbox");
  const [libraries, setLibraries] = useState<Library[]>([]);
  const [assets, setAssets] = useState<Asset[]>([]);
  const [works, setWorks] = useState<Work[]>([]);
  const [tasks, setTasks] = useState<TaskRun[]>([]);
  const [candidates, setCandidates] = useState<Record<string, Candidate[]>>({});
  const [providerStatus, setProviderStatus] = useState<string[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [message, setMessage] = useState("正在连接 Shadow MDC…");

  const refresh = useCallback(async () => {
    try {
      const [nextLibraries, nextAssets, nextWorks, nextTasks, providers] = await Promise.all([
        api.libraries(),
        api.assets(),
        api.works(),
        api.tasks(),
        api.providers()
      ]);
      setLibraries(nextLibraries);
      setAssets(nextAssets);
      setWorks(nextWorks);
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
            <h1>{view === "inbox" ? "识别收件箱" : view === "works" ? "作品库" : view === "libraries" ? "媒体库" : "运行记录"}</h1>
          </div>
          <button className="ghost" onClick={() => void refresh()}>刷新</button>
        </header>
        <div className="status">{message}</div>
        {view === "inbox" && (
          <Inbox
            assets={inbox}
            candidates={candidates}
            busy={busy}
            identify={identify}
            createManualCandidate={createManualCandidate}
            loadCandidates={loadCandidates}
            accept={accept}
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
          />
        )}
        {view === "libraries" && (
          <Libraries libraries={libraries} busy={busy} run={run} report={setMessage} />
        )}
        {view === "tasks" && <TaskHistory tasks={tasks} />}
      </main>
    </div>
  );
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
  candidates: Record<string, Candidate[]>;
  busy: string | null;
  identify: (asset: Asset, payload: { title?: string; source_url?: string }) => Promise<void>;
  createManualCandidate: (asset: Asset, title: string | undefined) => Promise<void>;
  loadCandidates: (assetId: string) => Promise<void>;
  accept: (candidate: Candidate) => Promise<void>;
}) {
  if (props.assets.length === 0) {
    return <Empty title="没有待确认文件" detail="扫描媒体库后，无法自动确认的影片会出现在这里。" />;
  }
  return <div className="asset-list">{props.assets.map((asset) => (
    <AssetReview
      key={asset.id}
      asset={asset}
      candidates={props.candidates[asset.id] ?? []}
      busy={props.busy}
      identify={props.identify}
      createManualCandidate={props.createManualCandidate}
      loadCandidates={props.loadCandidates}
      accept={props.accept}
    />
  ))}</div>;
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
}) {
  const { works } = props;
  const [code, setCode] = useState("");
  const categories = ["Japan", "China", "Korea", "Europe", "Other"] as const;
  return <>
    <form className="work-lookup" onSubmit={(event) => {
      event.preventDefault();
      if (code.trim()) void props.lookupWork(code.trim());
    }}>
      <div><h2>按番号获取信息</h2><p>无需先有本地文件；多个命中来源会逐字段聚合并保留来源。</p></div>
      <input value={code} onChange={(event) => setCode(event.target.value)} placeholder="例如 SONE-118" />
      <button disabled={!code.trim() || props.busy === "lookup-work"}>查询并建档</button>
    </form>
    {works.length === 0
      ? <Empty title="作品库为空" detail="按番号查询，或扫描媒体库后接受候选。" />
      : <div className="work-sections">{categories.map((category) => {
    const categoryWorks = works.filter((work) => work.category === category);
    if (categoryWorks.length === 0) return null;
    return <section className="work-section" key={category}>
      <div className="work-section-title"><h2>{category}</h2><span>{categoryWorks.length}</span></div>
      <div className="work-grid">{categoryWorks.map((work) => {
    const image = work.artwork.find((item) => typeof item.url === "string");
    const imageUrl = image && typeof image.url === "string" ? image.url : null;
    return (
      <article className="work" key={work.id}>
        <div className="poster" style={imageUrl ? { backgroundImage: `url("${imageUrl}")` } : undefined} />
        <div>
          <span className="pill">{work.category}</span>
          <h2>{work.title}</h2>
          <p>{[work.primary_code, work.studio, work.release_date].filter(Boolean).join(" · ")}</p>
          <div className="tags">{work.actors.slice(0, 4).map((actor) => <span key={actor}>{actor}</span>)}</div>
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
      })}</div>}
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
  const [category, setCategory] = useState("Other");
  function submit(event: FormEvent) {
    event.preventDefault();
    void props.run("create-library", async () => {
      await api.createLibrary({ name, root_path: path, category });
      setName("");
      setPath("");
      setCategory("Other");
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
        <select value={category} onChange={(event) => setCategory(event.target.value)}>
          <option value="Japan">Japan / 日本</option>
          <option value="China">China / 国产</option>
          <option value="Korea">Korea / 韩国</option>
          <option value="Europe">Europe / 欧美</option>
          <option value="Other">Other / 其他</option>
        </select>
        <button disabled={props.busy === "create-library"}>添加媒体库</button>
      </form>
      <p className="library-note">支持已挂载的网络磁盘和 UNC 共享；只读共享可以扫描，整理和写 NFO 需要写权限。</p>
      <div className="library-list">{props.libraries.map((library) => (
        <article key={library.id}>
          <div className="library-head">
            <div><h2>{library.name} · {library.category}</h2><p>{library.root_path}</p></div>
            <button
              disabled={props.busy === `scan-${library.id}`}
              onClick={() => void props.run(`scan-${library.id}`, async () => {
                const result = await api.scan(library.id);
                const errorSummary = result.errors.length > 0
                  ? `；${result.errors.length} 个路径失败：${result.errors.slice(0, 2).join("；")}`
                  : "";
                props.report(
                  `新增 ${result.discovered}，更新 ${result.updated}，自动建档 ${result.cataloged}，` +
                  `过滤 ${result.filtered}，` +
                  `跳过 ${result.skipped}${errorSummary}`
                );
              })}
            >扫描</button>
          </div>
          <LibraryOrganizer library={library} busy={props.busy} run={props.run} report={props.report} />
        </article>
      ))}</div>
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
        <option value="sidecar">当前文件夹，仅写同名 NFO</option>
        <option value="copy">复制到新路径 + NFO</option>
        <option value="move">移动到新路径 + NFO</option>
        <option value="hardlink">硬链接到新路径 + NFO</option>
        <option value="symlink">软链接到新路径 + NFO</option>
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

function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : "发生未知错误";
}
