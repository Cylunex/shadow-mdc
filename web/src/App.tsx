import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";

import { api } from "./api";
import type { Asset, Candidate, Library, Work } from "./model";

type View = "inbox" | "works" | "libraries";

export function App() {
  const [view, setView] = useState<View>("inbox");
  const [libraries, setLibraries] = useState<Library[]>([]);
  const [assets, setAssets] = useState<Asset[]>([]);
  const [works, setWorks] = useState<Work[]>([]);
  const [candidates, setCandidates] = useState<Record<string, Candidate[]>>({});
  const [providerStatus, setProviderStatus] = useState<string[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [message, setMessage] = useState("正在连接 Shadow MDC…");

  const refresh = useCallback(async () => {
    try {
      const [nextLibraries, nextAssets, nextWorks, providers] = await Promise.all([
        api.libraries(),
        api.assets(),
        api.works(),
        api.providers()
      ]);
      setLibraries(nextLibraries);
      setAssets(nextAssets);
      setWorks(nextWorks);
      setProviderStatus(
        providers.providers.map((provider) => `${provider.name}: ${provider.configured ? "可用" : "未配置"}`)
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
            <h1>{view === "inbox" ? "识别收件箱" : view === "works" ? "作品库" : "媒体库"}</h1>
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
            loadCandidates={loadCandidates}
            accept={accept}
          />
        )}
        {view === "works" && <Works works={works} />}
        {view === "libraries" && (
          <Libraries libraries={libraries} busy={busy} run={run} />
        )}
      </main>
    </div>
  );
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

function Works({ works }: { works: Work[] }) {
  if (works.length === 0) {
    return <Empty title="作品库为空" detail="接受候选后，逻辑作品会显示在这里。" />;
  }
  return <div className="work-grid">{works.map((work) => {
    const image = work.artwork.find((item) => typeof item.url === "string");
    const imageUrl = image && typeof image.url === "string" ? image.url : null;
    return (
      <article className="work" key={work.id}>
        <div className="poster" style={imageUrl ? { backgroundImage: `url("${imageUrl}")` } : undefined} />
        <div>
          <span className="pill">{work.family}</span>
          <h2>{work.title}</h2>
          <p>{[work.primary_code, work.studio, work.release_date].filter(Boolean).join(" · ")}</p>
          <div className="tags">{work.actors.slice(0, 4).map((actor) => <span key={actor}>{actor}</span>)}</div>
        </div>
      </article>
    );
  })}</div>;
}

function Libraries(props: {
  libraries: Library[];
  busy: string | null;
  run: (key: string, action: () => Promise<void>) => Promise<void>;
}) {
  const [name, setName] = useState("");
  const [path, setPath] = useState("");
  function submit(event: FormEvent) {
    event.preventDefault();
    void props.run("create-library", async () => {
      await api.createLibrary({ name, root_path: path });
      setName("");
      setPath("");
    });
  }
  return (
    <>
      <form className="library-form" onSubmit={submit}>
        <input value={name} onChange={(event) => setName(event.target.value)} placeholder="媒体库名称" required />
        <input value={path} onChange={(event) => setPath(event.target.value)} placeholder="绝对目录路径" required />
        <button disabled={props.busy === "create-library"}>添加媒体库</button>
      </form>
      <div className="library-list">{props.libraries.map((library) => (
        <article key={library.id}>
          <div><h2>{library.name}</h2><p>{library.root_path}</p></div>
          <button
            disabled={props.busy === `scan-${library.id}`}
            onClick={() => void props.run(`scan-${library.id}`, async () => { await api.scan(library.id); })}
          >扫描</button>
        </article>
      ))}</div>
    </>
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
