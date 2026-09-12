import { FormEvent, useState } from "react";
import { api } from "../api";
import type { DiscoverItem, MagnetLink, MultiSiteSearch } from "../model";

type Props = {
  busy: string | null;
  report: (message: string) => void;
  onSeeded: () => Promise<void>;
};

const LISTS: ReadonlyArray<{ value: string; label: string }> = [
  { value: "latest", label: "最新" },
  { value: "rankings_daily", label: "日榜" },
  { value: "rankings_weekly", label: "周榜" },
  { value: "rankings_monthly", label: "月榜" }
];

function stateLabel(state: DiscoverItem["state"]): string {
  if (state === "in_library") return "已有本地媒体";
  if (state === "catalog_only") return "仅元数据种子";
  return "未入库";
}

export function DiscoverPanel({ busy, report, onSeeded }: Props) {
  const [mode, setMode] = useState<"browse" | "multi">("browse");
  const [list, setList] = useState("latest");
  const [page, setPage] = useState(1);
  const [query, setQuery] = useState("");
  const [items, setItems] = useState<DiscoverItem[]>([]);
  const [multi, setMulti] = useState<MultiSiteSearch | null>(null);
  const [selectedMagnets, setSelectedMagnets] = useState<Record<string, MagnetLink>>({});

  async function loadBrowse(nextList = list, nextPage = page) {
    const result = await api.discoverBrowse({ provider: "javdb", list: nextList, page: nextPage });
    setItems(result.items);
    setMulti(null);
    report(`发现浏览 ${result.items.length} 条（不写入作品库）`);
  }

  async function loadSearch(event: FormEvent) {
    event.preventDefault();
    if (!query.trim()) return;
    if (mode === "multi") {
      const result = await api.discoverMultiSearch(query.trim(), true);
      setMulti(result);
      setItems([]);
      setSelectedMagnets({});
      report(`多源番号搜索 ${result.hits.length} 条命中`);
      return;
    }
    const result = await api.discoverSearch(query.trim(), { provider: "javdb", page: 1 });
    setItems(result.items);
    setMulti(null);
    report(`发现搜索 ${result.items.length} 条（不写入作品库）`);
  }

  async function seedItem(item: DiscoverItem) {
    const result = await api.discoverSeed({
      provider: item.provider,
      external_id: item.external_id,
      source_url: item.source_url,
      code: item.code ?? undefined
    });
    report(
      result.created
        ? `已从发现页建档元数据：${result.title}（仍无本地文件）`
        : `已关联现有作品：${result.title}`
    );
    await onSeeded();
  }

  async function copyText(value: string, label: string) {
    await navigator.clipboard.writeText(value);
    report(`已复制${label}`);
  }

  function toggleMagnet(magnet: MagnetLink) {
    setSelectedMagnets((current) => {
      const next = { ...current };
      if (next[magnet.info_hash]) delete next[magnet.info_hash];
      else next[magnet.info_hash] = magnet;
      return next;
    });
  }

  async function saveSelectedToWork(workId: string | null | undefined, provider: string) {
    const magnets = Object.values(selectedMagnets);
    if (!workId) {
      report("请先「建档元数据」再保存磁力");
      return;
    }
    if (magnets.length === 0) {
      report("未选择磁力");
      return;
    }
    await api.saveWorkMagnets(workId, magnets, provider);
    report(`已保存 ${magnets.length} 条磁力到作品（仅本地记录，不下载）`);
    setSelectedMagnets({});
    await onSeeded();
  }

  return (
    <div className="discover-panel">
      <div className="section-hero compact">
        <div>
          <p className="eyebrow">RANKINGS</p>
          <h1>榜单</h1>
          <p className="muted">日/周/月与最新目录；未入库角标可一点建档。浏览本身不写作品库。</p>
        </div>
      </div>
      <div className="discover-banner">
        <strong>发现 ≠ 作品库</strong>
        <p>
          这里浏览/搜索远程目录与榜单，不会因为打开列表就写入作品。只有你主动「建档元数据」或保存磁力时，才会写入本地索引。
          磁力仅供复制与本地保存，不下载、不提交网盘离线。
        </p>
      </div>

      <div className="discover-toolbar">
        <button type="button" className={mode === "browse" ? "active" : "ghost"} onClick={() => setMode("browse")}>
          榜单浏览
        </button>
        <button type="button" className={mode === "multi" ? "active" : "ghost"} onClick={() => setMode("multi")}>
          多源番号搜索
        </button>
      </div>

      {mode === "browse" && (
        <div className="discover-toolbar">
          <button
            type="button"
            className="danger-solid"
            disabled={busy === "discover"}
            onClick={() => {
              void (async () => {
                try {
                  await loadBrowse(list, page);
                  report("榜单已刷新");
                } catch (error) {
                  report(error instanceof Error ? error.message : String(error));
                }
              })();
            }}
          >更新榜单</button>
          {LISTS.map((entry) => (
            <button
              key={entry.value}
              type="button"
              className={list === entry.value ? "active" : "ghost"}
              disabled={busy === "discover"}
              onClick={() => {
                setList(entry.value);
                setPage(1);
                void (async () => {
                  try {
                    await loadBrowse(entry.value, 1);
                  } catch (error) {
                    report(error instanceof Error ? error.message : String(error));
                  }
                })();
              }}
            >
              {entry.label}
            </button>
          ))}
          <button
            type="button"
            className="secondary"
            disabled={busy === "discover"}
            onClick={() => {
              void (async () => {
                try {
                  await loadBrowse(list, page);
                } catch (error) {
                  report(error instanceof Error ? error.message : String(error));
                }
              })();
            }}
          >
            刷新列表
          </button>
          <button
            type="button"
            className="ghost"
            disabled={page <= 1}
            onClick={() => {
              const next = Math.max(1, page - 1);
              setPage(next);
              void loadBrowse(list, next).catch((error) => report(String(error)));
            }}
          >
            上一页
          </button>
          <button
            type="button"
            className="ghost"
            onClick={() => {
              const next = page + 1;
              setPage(next);
              void loadBrowse(list, next).catch((error) => report(String(error)));
            }}
          >
            下一页
          </button>
        </div>
      )}

      <form className="discover-search" onSubmit={(event) => void loadSearch(event).catch((error) => report(String(error)))}>
        <input
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder={mode === "multi" ? "多源搜索番号，例如 SONE-118" : "在发现源中搜索"}
        />
        <button type="submit" disabled={!query.trim() || busy === "discover"}>
          {mode === "multi" ? "多源搜索" : "搜索"}
        </button>
      </form>

      {multi && (
        <div className="discover-multi">
          <p className="muted">
            查询 {multi.query}
            {multi.code ? ` · 规范化 ${multi.code}` : ""} · {multi.hits.length} 个来源命中
          </p>
          {multi.failures.length > 0 && <p className="muted">部分来源失败：{multi.failures.join("；")}</p>}
          {multi.hits.map((hit) => (
            <article key={`${hit.provider}-${hit.item.external_id}`} className="discover-hit">
              <header>
                <span className="pill">{hit.provider}</span>
                <strong>{hit.item.title}</strong>
                <span className="muted">{stateLabel(hit.item.state)}</span>
              </header>
              <p>{[hit.item.code, hit.item.release_date].filter(Boolean).join(" · ")}</p>
              <div className="discover-actions">
                {hit.item.code && (
                  <button type="button" className="ghost" onClick={() => void copyText(hit.item.code!, "番号")}>
                    复制番号
                  </button>
                )}
                <button type="button" className="secondary" onClick={() => void seedItem(hit.item)}>
                  建档元数据
                </button>
                {hit.item.work_id && Object.keys(selectedMagnets).length > 0 && (
                  <button
                    type="button"
                    onClick={() => void saveSelectedToWork(hit.item.work_id, hit.provider)}
                  >
                    保存所选磁力到作品
                  </button>
                )}
              </div>
              {hit.magnets_error && <p className="muted">磁力读取失败：{hit.magnets_error}</p>}
              {hit.magnets.length > 0 && (
                <div className="magnet-list">
                  <h3>磁力（仅展示/保存）</h3>
                  {hit.magnets.map((magnet) => (
                    <label key={magnet.info_hash} className="magnet-row">
                      <input
                        type="checkbox"
                        checked={Boolean(selectedMagnets[magnet.info_hash])}
                        onChange={() => toggleMagnet(magnet)}
                      />
                      <span>
                        {(magnet.name || magnet.info_hash.slice(0, 12)) +
                          (magnet.has_subtitle ? " · 字幕" : "") +
                          (magnet.hd ? " · HD" : "")}
                      </span>
                      <button type="button" className="ghost" onClick={() => void copyText(magnet.uri, "磁力")}>
                        复制
                      </button>
                    </label>
                  ))}
                </div>
              )}
            </article>
          ))}
        </div>
      )}

      {!multi && (
        <div className="discover-grid">
          {items.length === 0 ? (
            <p className="empty-detail">选择榜单或搜索后显示远程结果。这些条目不属于作品库。</p>
          ) : (
            items.map((item) => (
              <article key={`${item.provider}-${item.external_id}`} className="discover-card">
                <div
                  className="poster"
                  style={item.thumb_url ? { backgroundImage: `url("${item.thumb_url}")` } : undefined}
                />
                <div>
                  <span className="pill">{stateLabel(item.state)}</span>
                  <h2>{item.title}</h2>
                  <p>{[item.code, item.release_date].filter(Boolean).join(" · ")}</p>
                  <div className="discover-actions">
                    {item.code && (
                      <button type="button" className="ghost" onClick={() => void copyText(item.code!, "番号")}>
                        复制番号
                      </button>
                    )}
                    <button type="button" className="secondary" onClick={() => void seedItem(item)}>
                      建档元数据
                    </button>
                    <a className="ghost" href={item.source_url} target="_blank" rel="noreferrer">
                      打开详情
                    </a>
                  </div>
                </div>
              </article>
            ))
          )}
        </div>
      )}
    </div>
  );
}
