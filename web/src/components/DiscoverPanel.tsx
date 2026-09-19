import { FormEvent, useEffect, useMemo, useState } from "react";
import { api } from "../api";
import type {
  DiscoverItem,
  JavRankingList,
  JavRankingSection,
  MagnetLink,
  MultiSiteSearch
} from "../model";

type Props = {
  busy: string | null;
  report: (message: string) => void;
  onSeeded: () => Promise<void>;
  onOpenWork?: (workId: string) => void;
};

type RankTab = "awards" | "yearly" | "browse";

const LISTS: ReadonlyArray<{ value: string; label: string }> = [
  { value: "latest", label: "最新" },
  { value: "rankings_daily", label: "日榜" },
  { value: "rankings_weekly", label: "周榜" },
  { value: "rankings_monthly", label: "月榜" }
];

function stateLabel(state: DiscoverItem["state"] | string | null | undefined): string {
  if (state === "in_library") return "已有本地媒体";
  if (state === "catalog_only") return "已入库";
  if (state === "not_in_library") return "未入库";
  return "未入库";
}

function isCataloged(state: string | null | undefined, workId?: string | null): boolean {
  return Boolean(workId) || state === "catalog_only" || state === "in_library";
}

export function DiscoverPanel({ busy, report, onSeeded, onOpenWork }: Props) {
  const [tab, setTab] = useState<RankTab>("awards");
  const [list, setList] = useState("latest");
  const [page, setPage] = useState(1);
  const [query, setQuery] = useState("");
  const [items, setItems] = useState<DiscoverItem[]>([]);
  const [multi, setMulti] = useState<MultiSiteSearch | null>(null);
  const [selectedMagnets, setSelectedMagnets] = useState<Record<string, MagnetLink>>({});
  const [loading, setLoading] = useState(false);
  const [jrSections, setJrSections] = useState<JavRankingSection[]>([]);
  const [jrSlug, setJrSlug] = useState<string>("most-awarded-videos");
  const [jrList, setJrList] = useState<JavRankingList | null>(null);
  const [jrSeeding, setJrSeeding] = useState(false);
  const [jrLoaded, setJrLoaded] = useState(false);
  const blocked = loading || busy === "discover" || jrSeeding;

  const jrGroups = useMemo(() => {
    const curatedVideos = jrSections.filter((item) => item.kind === "curated-videos");
    const curatedActors = jrSections.filter((item) => item.kind === "curated-actors");
    const top250 = [...jrSections.filter((item) => item.kind === "top250-year")].sort(
      (a, b) => (b.year ?? 0) - (a.year ?? 0)
    );
    return { curatedVideos, curatedActors, top250 };
  }, [jrSections]);

  const latestYearSlug = jrGroups.top250[0]?.slug ?? null;

  async function withLoading(action: () => Promise<void>) {
    setLoading(true);
    try {
      await action();
    } catch (error) {
      report(error instanceof Error ? error.message : String(error));
    } finally {
      setLoading(false);
    }
  }

  async function loadBrowse(nextList = list, nextPage = page) {
    const result = await api.discoverBrowse({ provider: "javdb", list: nextList, page: nextPage });
    setItems(result.items);
    setMulti(null);
    report(`发现浏览 ${result.items.length} 条（不写入作品库）`);
  }

  async function loadJavRankingSections(forceRefresh = false, preferredSlug?: string) {
    const result = await api.javrankingSections(forceRefresh);
    setJrSections(result.sections);
    setJrLoaded(true);
    const top250 = result.sections
      .filter((item) => item.kind === "top250-year")
      .sort((a, b) => (b.year ?? 0) - (a.year ?? 0));
    const top250Ready = top250.filter((item) => (item.item_count ?? 0) > 0);
    const preferred =
      (preferredSlug && result.sections.find((item) => item.slug === preferredSlug)?.slug) ||
      result.sections.find((item) => item.slug === jrSlug)?.slug ||
      (preferredSlug?.startsWith("javdb-top250") ? top250Ready[0]?.slug : undefined) ||
      result.sections.find((item) => item.slug === "most-awarded-videos")?.slug ||
      top250Ready[0]?.slug ||
      result.sections[0]?.slug;
    if (preferred) {
      setJrSlug(preferred);
      const detail = await api.javrankingList(preferred, forceRefresh);
      setJrList(detail);
      report(`榜单分区 ${result.sections.length} · 当前 ${detail.items.length} 条（只读）`);
    } else {
      setJrList(null);
      report("暂无可用榜单分区");
    }
  }

  async function loadJavRankingList(slug: string, forceRefresh = false) {
    setJrSlug(slug);
    const detail = await api.javrankingList(slug, forceRefresh);
    setJrList(detail);
    report(`「${detail.section.title}」${detail.items.length} 条（只读）`);
  }

  useEffect(() => {
    void withLoading(() => loadJavRankingSections(false, "most-awarded-videos"));
    // Mount: default open 神作.
  }, []);

  async function ensureJrThen(action: () => Promise<void>) {
    if (!jrLoaded) {
      await loadJavRankingSections(false);
    }
    await action();
  }

  async function loadSearch(event: FormEvent) {
    event.preventDefault();
    if (!query.trim()) return;
    await withLoading(async () => {
      setTab("browse");
      const result = await api.discoverMultiSearch(query.trim(), true);
      setMulti(result);
      setItems([]);
      setSelectedMagnets({});
      report(`多源番号搜索 ${result.hits.length} 条命中`);
    });
  }

  async function seedItem(item: DiscoverItem) {
    await withLoading(async () => {
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
    });
  }

  async function seedJavRankingMissing() {
    if (!jrList) return;
    const section = jrList.section;
    setJrSeeding(true);
    try {
      const payload =
        section.kind === "curated-videos"
          ? { list_slug: section.slug, limit: 20, dry_run: false }
          : section.kind === "top250-year"
            ? { ranking_slug: section.slug, limit: 20, dry_run: false }
            : null;
      if (!payload) {
        report("演员战力榜为只读展示，不支持补入库");
        return;
      }
      const result = await api.javrankingSeed(payload);
      report(
        `补入库完成：新建 ${result.seeded_count} · 跳过 ${result.skipped_count} · 失败 ${result.failure_count}`
      );
      await loadJavRankingList(section.slug, true);
      await onSeeded();
    } catch (error) {
      report(error instanceof Error ? error.message : String(error));
    } finally {
      setJrSeeding(false);
    }
  }

  async function copyText(value: string, label: string) {
    try {
      await navigator.clipboard.writeText(value);
      report(`已复制${label}`);
    } catch {
      report(`复制${label}失败`);
    }
  }

  function toggleMagnet(magnet: MagnetLink) {
    setSelectedMagnets((prev) => {
      const next = { ...prev };
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
    await withLoading(async () => {
      await api.saveWorkMagnets(workId, magnets, provider);
      report(`已保存 ${magnets.length} 条磁力到作品（仅本地记录，不下载）`);
      setSelectedMagnets({});
      await onSeeded();
    });
  }

  const showAwards = tab === "awards";
  const showYearly = tab === "yearly";
  const showBrowse = tab === "browse";

  return (
    <div className="panel discover-panel">
      <div className="panel-header">
        <div>
          <h1>榜单</h1>
          <p className="muted">神作·战力、年榜 TOP250（2008+）、发现浏览；浏览本身不写作品库。</p>
        </div>
      </div>
      <div className="discover-banner">
        <strong>发现 ≠ 作品库</strong>
        <p>
          这里浏览/搜索远程目录与榜单，不会因为打开列表就写入作品。只有你主动「建档元数据」或「补入库」时，才会写入本地索引。
          磁力仅供复制与本地保存，不下载、不提交网盘离线。
        </p>
      </div>

      <div className="discover-toolbar rank-tabs">
        <button
          type="button"
          className={showAwards ? "active" : "ghost"}
          disabled={blocked}
          onClick={() => {
            setTab("awards");
            void withLoading(() =>
              ensureJrThen(async () => {
                const slug =
                  jrGroups.curatedVideos[0]?.slug ||
                  jrSections.find((s) => s.slug === "most-awarded-videos")?.slug ||
                  "most-awarded-videos";
                await loadJavRankingList(slug);
              })
            );
          }}
        >
          神作·战力
          {jrGroups.curatedVideos[0] ? ` · ${jrGroups.curatedVideos[0].item_count}` : ""}
        </button>
        <button
          type="button"
          className={showYearly ? "active" : "ghost"}
          disabled={blocked}
          onClick={() => {
            setTab("yearly");
            void withLoading(() =>
              ensureJrThen(async () => {
                const slug = latestYearSlug || jrGroups.top250[0]?.slug;
                if (slug) await loadJavRankingList(slug);
              })
            );
          }}
        >
          年榜 TOP250
          {jrGroups.top250.length ? ` · ${jrGroups.top250.length}年` : ""}
        </button>
        <button
          type="button"
          className={showBrowse ? "active" : "ghost"}
          disabled={blocked}
          onClick={() => {
            setTab("browse");
            void withLoading(() => loadBrowse(list, page));
          }}
        >
          发现浏览
        </button>
      </div>

      {(showAwards || showYearly) && (
        <div className="javranking-panel">
          <div className="discover-toolbar">
            <button
              type="button"
              className="secondary"
              disabled={blocked}
              onClick={() => void withLoading(() => loadJavRankingSections(true, jrSlug))}
            >
              刷新榜单缓存
            </button>
            {(jrList?.section.kind === "curated-videos" || jrList?.section.kind === "top250-year") && (
              <button type="button" className="danger-solid" disabled={blocked} onClick={() => void seedJavRankingMissing()}>
                补入库缺失
              </button>
            )}
            {jrList && (
              <span className="muted">
                {jrList.section.title} · {jrList.items.length} 条
                {jrList.revision ? ` · rev ${jrList.revision.slice(0, 8)}` : ""}
              </span>
            )}
          </div>

          {showAwards && (
            <>
              <div className="javranking-section-block">
                <h3>神作 TOP100</h3>
                <div className="discover-toolbar wrap">
                  {jrGroups.curatedVideos.length === 0 && !loading && (
                    <p className="muted">暂无神作榜数据</p>
                  )}
                  {jrGroups.curatedVideos.map((section) => (
                    <button
                      key={section.id}
                      type="button"
                      className={jrSlug === section.slug ? "active" : "ghost"}
                      disabled={blocked}
                      onClick={() => void withLoading(() => loadJavRankingList(section.slug))}
                    >
                      {section.title} · {section.item_count}
                    </button>
                  ))}
                </div>
              </div>
              <div className="javranking-section-block">
                <h3>演员战力</h3>
                <div className="discover-toolbar wrap">
                  {jrGroups.curatedActors.map((section) => (
                    <button
                      key={section.id}
                      type="button"
                      className={jrSlug === section.slug ? "active" : "ghost"}
                      disabled={blocked}
                      onClick={() => void withLoading(() => loadJavRankingList(section.slug))}
                    >
                      {section.title} · {section.item_count}
                    </button>
                  ))}
                </div>
              </div>
            </>
          )}

          {showYearly && (
            <div className="javranking-section-block">
              <h3>年榜 TOP250 · {jrGroups.top250.length} 个年份</h3>
              <div className="year-chip-bar sticky-year-bar">
                {jrGroups.top250.length === 0 && !loading && (
                  <p className="muted">暂无年榜；请在 box 运行 export_javdb_yearly_top250.py 后 rsync 到 NAS</p>
                )}
                {[...jrGroups.top250]
                  .sort((a, b) => (a.year ?? 0) - (b.year ?? 0))
                  .map((section) => (
                  <button
                    key={section.id}
                    type="button"
                    className={jrSlug === section.slug ? "active" : "ghost"}
                    disabled={blocked || (section.item_count ?? 0) === 0}
                    title={section.title}
                    onClick={() => void withLoading(() => loadJavRankingList(section.slug))}
                  >
                    {section.year ?? section.title}
                    <span className="chip-count">{section.item_count || "—"}</span>
                  </button>
                ))}
              </div>
            </div>
          )}

          <div className={`discover-grid ${showYearly ? "dense-rank-grid" : ""}`}>
            {loading && (!jrList || jrList.items.length === 0) ? (
              <p className="empty-detail">正在拉取榜单…</p>
            ) : !jrList || jrList.items.length === 0 ? (
              <p className="empty-detail">选择上方分区查看只读榜单。</p>
            ) : (
              jrList.items.map((item) => {
                const linked = Boolean(item.work_id);
                const cataloged = isCataloged(item.state, item.work_id);
                return (
                <article
                  key={`${jrList.section.slug}-${item.position}-${item.code ?? item.name ?? item.title}`}
                  className={`discover-card dense-rank-card${linked ? " is-linked" : ""}`}
                  role={linked ? "button" : undefined}
                  tabIndex={linked ? 0 : undefined}
                  onClick={() => {
                    if (linked && item.work_id && onOpenWork) onOpenWork(item.work_id);
                  }}
                  onKeyDown={(event) => {
                    if (!linked || !item.work_id || !onOpenWork) return;
                    if (event.key === "Enter" || event.key === " ") {
                      event.preventDefault();
                      onOpenWork(item.work_id);
                    }
                  }}
                >
                  <div
                    className="poster"
                    style={item.thumb_url ? { backgroundImage: `url("${item.thumb_url}")` } : undefined}
                  >
                    <span className="rank-badge overlay-rank">#{item.position}</span>
                    {cataloged && <span className="in-library-badge">已入库</span>}
                  </div>
                  <div>
                    {item.state && (
                      <span className={`pill${cataloged ? " pill-in-library" : ""}`}>
                        {stateLabel(item.state)}
                      </span>
                    )}
                    <h2>{item.code ? `${item.code}` : item.title}</h2>
                    <p className="rank-card-title">{item.code ? item.title : null}</p>
                    <p>
                      {[
                        item.score != null ? `战力 ${item.score}` : null,
                        item.appearances != null ? `上榜作品 ${item.appearances}` : null
                      ]
                        .filter(Boolean)
                        .join(" · ")}
                    </p>
                    <div className="discover-actions" onClick={(event) => event.stopPropagation()}>
                      {linked && item.work_id && onOpenWork && (
                        <button
                          type="button"
                          className="secondary"
                          onClick={() => onOpenWork(item.work_id!)}
                        >
                          打开作品
                        </button>
                      )}
                      {item.code && (
                        <button type="button" className="ghost" onClick={() => void copyText(item.code!, "番号")}>
                          复制番号
                        </button>
                      )}
                      {item.url && (
                        <a className="ghost" href={item.url} target="_blank" rel="noreferrer">
                          外部详情
                        </a>
                      )}
                    </div>
                  </div>
                </article>
                );
              })
            )}
          </div>
        </div>
      )}

      {showBrowse && (
        <>
          <div className="discover-toolbar">
            <button
              type="button"
              className="danger-solid"
              disabled={blocked}
              onClick={() => {
                void withLoading(async () => {
                  await loadBrowse(list, page);
                  report("榜单已刷新");
                });
              }}
            >
              更新榜单
            </button>
            {LISTS.map((entry) => (
              <button
                key={entry.value}
                type="button"
                className={list === entry.value ? "active" : "ghost"}
                disabled={blocked}
                onClick={() => {
                  setList(entry.value);
                  setPage(1);
                  void withLoading(() => loadBrowse(entry.value, 1));
                }}
              >
                {entry.label}
              </button>
            ))}
            <button
              type="button"
              className="secondary"
              disabled={blocked}
              onClick={() => {
                void withLoading(() => loadBrowse(list, page));
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
                void withLoading(() => loadBrowse(list, next));
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
                void withLoading(() => loadBrowse(list, next));
              }}
            >
              下一页
            </button>
          </div>

          <form className="discover-search" onSubmit={(event) => void loadSearch(event)}>
            <input
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="多源搜索番号，例如 SONE-118"
            />
            <button type="submit" disabled={!query.trim() || blocked}>
              多源搜索
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
                <p className="empty-detail">{loading ? "正在拉取榜单…" : "选择榜单或搜索后显示远程结果。这些条目不属于作品库。"}</p>
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
        </>
      )}
    </div>
  );
}
