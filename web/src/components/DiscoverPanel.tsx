import { z } from "zod";
import { useSessionState } from "../hooks/useSessionState";
import { CoverImage } from "./CoverImage";
import { FormEvent, useEffect, useMemo, useRef, useState } from "react";
import { api, appUrl } from "../api";
import { copyToClipboard } from "../lib/clipboard";
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
  onFilterByCode?: (code: string) => void;
  wantListIds?: ReadonlySet<string> | readonly string[];
  onToggleWant?: (workId: string, wanted: boolean) => void | Promise<void>;
};

const rankingState = z.object({
  tab: z.enum(["awards", "actors", "yearly", "browse"]), slug: z.string(),
  list: z.enum(["latest", "rankings_daily", "rankings_weekly", "rankings_monthly"]),
  page: z.number().int().positive(), term: z.string(),
});
type RankTab = Exclude<z.infer<typeof rankingState>["tab"], "browse">;

const LISTS = [
  { value: "latest", label: "最新" },
  { value: "rankings_daily", label: "日榜" },
  { value: "rankings_weekly", label: "周榜" },
  { value: "rankings_monthly", label: "月榜" }
] as const;

function sectionsForTab(sections: JavRankingSection[], tab: RankTab) {
  const kind = { awards: "curated-videos", actors: "curated-actors", yearly: "top250-year" }[tab];
  const choices = sections.filter(section => section.kind === kind);
  if (tab === "yearly") choices.sort((a, b) => (b.year ?? 0) - (a.year ?? 0));
  return choices;
}

function stateLabel(state: DiscoverItem["state"] | string | null | undefined): string {
  if (state === "in_library") return "已有本地媒体";
  if (state === "catalog_only") return "已入库";
  if (state === "not_in_library") return "未入库";
  return "未入库";
}

function isCataloged(state: string | null | undefined, workId?: string | null): boolean {
  return Boolean(workId) || state === "catalog_only" || state === "in_library";
}

export function DiscoverPanel({ busy, report, onSeeded, onOpenWork, onFilterByCode, wantListIds, onToggleWant }: Props) {
  const wantSet = useMemo(() => {
    if (!wantListIds) return new Set<string>();
    return wantListIds instanceof Set ? wantListIds : new Set(wantListIds);
  }, [wantListIds]);
  const [browsing, setBrowsing] = useSessionState<z.infer<typeof rankingState>>("mdc:rankings", rankingState, {
    tab: "awards", slug: "most-awarded-videos", list: "latest", page: 1, term: "",
  });
  const { tab, slug: jrSlug, list, page, term } = browsing;
  const [query, setQuery] = useState(term);
  const [items, setItems] = useState<DiscoverItem[]>([]);
  const [multi, setMulti] = useState<MultiSiteSearch | null>(null);
  const [selectedMagnets, setSelectedMagnets] = useState<Record<string, MagnetLink>>({});
  const [loading, setLoading] = useState(false);
  const [jrSections, setJrSections] = useState<JavRankingSection[]>([]);
  const [jrList, setJrList] = useState<JavRankingList | null>(null);
  const [jrSeeding, setJrSeeding] = useState(false);
  const requestRef = useRef<AbortController | null>(null);
  const blocked = loading || busy === "discover" || jrSeeding;

  useEffect(() => () => requestRef.current?.abort(), []);

  const choices = useMemo(() => tab === "browse" ? [] : sectionsForTab(jrSections, tab), [jrSections, tab]);

  async function withLoading(action: (signal: AbortSignal) => Promise<void>) {
    requestRef.current?.abort();
    const controller = new AbortController();
    requestRef.current = controller;
    setLoading(true);
    try {
      await action(controller.signal);
    } catch (error) {
      if (!controller.signal.aborted) {
        report(error instanceof Error ? error.message : String(error));
      }
    } finally {
      if (requestRef.current === controller) {
        setLoading(false);
      }
    }
  }

  async function loadBrowse(nextList: typeof list, nextPage: number, signal: AbortSignal) {
    setBrowsing(previous => ({ ...previous, tab: "browse", list: nextList, page: nextPage, term: "" }));
    setQuery("");
    setMulti(null);
    setItems([]);
    const result = await api.discoverBrowse({ provider: "javdb", list: nextList, page: nextPage }, signal);
    if (signal.aborted) return;
    setItems(result.items);
    report(`发现浏览 ${result.items.length} 条`);
  }

  async function loadRanking(nextTab: RankTab, preferred: string, signal: AbortSignal, refresh = false) {
    setBrowsing(previous => ({ ...previous, tab: nextTab, slug: preferred }));
    setJrList(null);
    let sections = jrSections;
    if (!sections.length || refresh) {
      const result = await api.javrankingSections(refresh, signal);
      if (signal.aborted) return;
      sections = result.sections;
      setJrSections(sections);
    }
    const options = sectionsForTab(sections, nextTab);
    const available = nextTab === "yearly" ? options.filter(section => section.item_count > 0) : options;
    const selected = available.find(section => section.slug === preferred) ?? available[0];
    if (!selected) return;
    setBrowsing(previous => ({ ...previous, slug: selected.slug }));
    const detail = await api.javrankingList(selected.slug, refresh, signal);
    if (signal.aborted) return;
    setJrList(detail);
    setJrSections(current => current.map(section => section.slug === selected.slug ? detail.section : section));
    report(`「${detail.section.title}」${detail.items.length} 条`);
  }

  async function searchCode(value: string, signal: AbortSignal) {
    setBrowsing(previous => ({ ...previous, tab: "browse", term: value }));
    setMulti(null);
    setItems([]);
    setSelectedMagnets({});
    const result = await api.discoverMultiSearch(value, true, signal);
    if (signal.aborted) return;
    setMulti(result);
    report(`多源番号搜索 ${result.hits.length} 条命中`);
  }

  useEffect(() => {
    void withLoading(signal => tab !== "browse" ? loadRanking(tab, jrSlug, signal)
      : term ? searchCode(term, signal) : loadBrowse(list, page, signal));
  }, []);

  function loadSearch(event: FormEvent) {
    event.preventDefault();
    if (query.trim()) void withLoading(signal => searchCode(query.trim(), signal));
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
      if (tab !== "browse") await withLoading(signal => loadRanking(tab, section.slug, signal, true));
      await onSeeded();
    } catch (error) {
      report(error instanceof Error ? error.message : String(error));
    } finally {
      setJrSeeding(false);
    }
  }

  async function copyText(value: string, label: string) {
    const ok = await copyToClipboard(value);
    report(ok ? `已复制${label}` : `复制${label}失败`);
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

  const showYearly = tab === "yearly";
  const showBrowse = tab === "browse";

  return (
    <div className="panel discover-panel">
      <div className="panel-header">
        <div>
          <h1>榜单</h1>
          <p className="muted">点击已入库的作品可打开详情。</p>
        </div>
      </div>
      <div className="discover-toolbar rank-tabs">
        {([["awards", "神作 TOP100"], ["actors", "演员战力"], ["yearly", "年榜 TOP250"]] as const).map(([value, label]) =>
          <button key={value} type="button" className={tab === value ? "active" : "ghost"} aria-pressed={tab === value}
            disabled={blocked} onClick={() => void withLoading(signal => loadRanking(value, jrSlug, signal))}>{label}</button>)}
        <button type="button" className={showBrowse ? "active" : "ghost"} aria-pressed={showBrowse}
          disabled={blocked} onClick={() => void withLoading(signal => loadBrowse(list, page, signal))}>
          发现浏览
        </button>
      </div>

      {!showBrowse && (
        <div className="javranking-panel">
          <div className="discover-toolbar">
            <button
              type="button"
              className="secondary"
              disabled={blocked}
              onClick={() => void withLoading((signal) => loadRanking(tab, jrSlug, signal, true))}
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

          {!showYearly && choices.length > 1 && <div className="discover-toolbar wrap" aria-label="榜单分区">
            {choices.map(section => <button key={section.id} type="button"
              className={jrSlug === section.slug ? "active" : "ghost"} aria-pressed={jrSlug === section.slug}
              disabled={blocked} onClick={() => void withLoading(signal => loadRanking(tab, section.slug, signal))}>
              {section.title} · {section.item_count}
            </button>)}
          </div>}

          {showYearly && (
            <div className="javranking-section-block">
              <h3>年榜 TOP250 · {choices.length} 个年份</h3>
              <div className="year-chip-bar sticky-year-bar">
                {choices.length === 0 && !loading && (
                  <p className="muted">暂无年榜数据</p>
                )}
                {choices.map((section) => (
                  <button
                    key={section.id}
                    type="button"
                    className={jrSlug === section.slug ? "active" : "ghost"}
                    disabled={blocked || (section.item_count ?? 0) === 0}
                    title={section.title}
                    onClick={() => void withLoading((signal) => loadRanking(tab, section.slug, signal))}
                  >
                    {section.year ?? section.title}
                    <span className="chip-count">{section.item_count || "—"}</span>
                  </button>
                ))}
              </div>
            </div>
          )}

          <div className="discover-grid dense-rank-grid">
            {loading && (!jrList || jrList.items.length === 0) ? (
              <div className="skeleton-grid" aria-busy="true" aria-label="榜单加载中">
                {Array.from({ length: 6 }, (_, index) => (
                  <div className="skeleton-card" key={index}>
                    <div className="skeleton-poster" />
                    <div className="skeleton-lines"><span /><span /><span /></div>
                  </div>
                ))}
              </div>
            ) : !jrList || jrList.items.length === 0 ? (
              <div className="empty"><div>◆</div><h2>暂无榜单</h2><p>选择上方分区查看只读榜单。</p></div>
            ) : (
              jrList.items.map((item) => {
                const linked = Boolean(item.work_id);
                const cataloged = isCataloged(item.state, item.work_id);
                return (
                <article
                  key={`${jrList.section.slug}-${item.position}-${item.code ?? item.name ?? item.title}`}
                  className={`discover-card dense-rank-card${linked || (item.code && onFilterByCode) ? " is-linked" : ""}`}
                  role={linked || (item.code && onFilterByCode) ? "button" : undefined}
                  tabIndex={linked || (item.code && onFilterByCode) ? 0 : undefined}
                  onClick={() => {
                    if (linked && item.work_id && onOpenWork) onOpenWork(item.work_id);
                    else if (!linked && item.code && onFilterByCode) onFilterByCode(item.code);
                  }}
                  onKeyDown={(event) => {
                    if (event.target !== event.currentTarget || (event.key !== "Enter" && event.key !== " ")) return;
                    event.preventDefault();
                    if (linked && item.work_id && onOpenWork) onOpenWork(item.work_id);
                    else if (!linked && item.code && onFilterByCode) onFilterByCode(item.code);
                  }}
                >
                  <div className="poster">
                    {item.thumb_url ? (
                      <CoverImage src={appUrl(item.thumb_url) ?? item.thumb_url} />
                    ) : (
                      <div className="poster-fallback" aria-hidden />
                    )}
                    <span className="rank-badge overlay-rank">#{item.position}</span>
                    {cataloged && <span className="in-library-badge">已入库</span>}
                    {linked && item.work_id && wantSet.has(item.work_id) && (
                      <span className="want-badge">想看</span>
                    )}
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
                      {linked && item.work_id && onToggleWant && (
                        <button
                          type="button"
                          className={wantSet.has(item.work_id) ? "active" : "ghost"}
                          disabled={blocked}
                          onClick={() => {
                            const id = item.work_id!;
                            const next = !wantSet.has(id);
                            void onToggleWant(id, next);
                          }}
                        >
                          {wantSet.has(item.work_id) ? "取消想看" : "想看"}
                        </button>
                      )}
                      {item.code && onFilterByCode && (
                        <button type="button" className="secondary" onClick={() => onFilterByCode(item.code!)}>
                          片库筛选
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
            {LISTS.map((entry) => (
              <button
                key={entry.value}
                type="button"
                className={list === entry.value ? "active" : "ghost"}
                disabled={blocked}
                onClick={() => {
                  void withLoading((signal) => loadBrowse(entry.value, 1, signal));
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
                void withLoading(signal => term ? searchCode(term, signal) : loadBrowse(list, page, signal));
              }}
            >
              刷新结果
            </button>
            <button
              type="button"
              className="ghost"
              disabled={blocked || page <= 1 || Boolean(term)}
              onClick={() => {
                const next = Math.max(1, page - 1);
                void withLoading((signal) => loadBrowse(list, next, signal));
              }}
            >
              上一页
            </button>
            {!term && <span className="muted">第 {page} 页</span>}
            <button
              type="button"
              className="ghost"
              disabled={blocked || Boolean(term) || items.length === 0}
              onClick={() => {
                const next = page + 1;
                void withLoading((signal) => loadBrowse(list, next, signal));
              }}
            >
              下一页
            </button>
          </div>

          <form className="discover-search" onSubmit={(event) => void loadSearch(event)}>
            <input
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              aria-label="搜索番号" placeholder="输入番号"
            />
            {term && <button type="button" className="ghost" disabled={blocked}
              onClick={() => void withLoading(signal => loadBrowse(list, page, signal))}>返回列表</button>}
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
                    {hit.item.code && onFilterByCode && (
                      <button type="button" className="secondary" onClick={() => onFilterByCode(hit.item.code!)}>
                        片库筛选
                      </button>
                    )}
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
                              (magnet.hd ? " · HD" : "") +
                              (magnet.size_bytes
                                ? ` · ${(magnet.size_bytes / (1024 ** 3)).toFixed(2)} GiB`
                                : "")}
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
                <p className="empty-detail">{loading ? "正在拉取榜单…" : "当前页没有结果。"}</p>
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
                        {item.code && onFilterByCode && (
                          <button type="button" className="secondary" onClick={() => onFilterByCode(item.code!)}>
                            片库筛选
                          </button>
                        )}
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
