import { useEffect, useMemo, useState, type ReactNode } from "react";

import { api, appUrl } from "../api";
import { StatusBadges } from "../components/StatusBadges";
import type { Work, WorkDetail, WorkRelated } from "../model";

const EDITABLE_LOCK_FIELDS = ["title", "actors", "studio", "series", "tags", "plot"] as const;

function formatRuntime(seconds: number | null | undefined): string | null {
  if (seconds == null || seconds <= 0) return null;
  const mins = Math.round(seconds / 60);
  if (mins < 60) return `${mins} 分钟`;
  const h = Math.floor(mins / 60);
  const m = mins % 60;
  return m ? `${h} 小时 ${m} 分钟` : `${h} 小时`;
}

function sourceOf(work: WorkDetail, field: string): string {
  return work.field_sources[field] ?? "—";
}

function RelatedStrip(props: {
  title: string;
  empty: string;
  works: Work[];
  onOpenWork: (workId: string) => void;
}) {
  return (
    <section className="work-page-section work-related-strip">
      <div className="work-related-head">
        <h2>{props.title}</h2>
        {props.works.length > 0 && <span className="muted">{props.works.length}</span>}
      </div>
      {props.works.length === 0 ? (
        <p className="muted">{props.empty}</p>
      ) : (
        <div className="work-related-rail" role="list">
          {props.works.map((item) => {
            const thumb = item.image_url ? appUrl(item.image_url) : null;
            return (
              <button
                key={item.id}
                type="button"
                className="work-related-card"
                role="listitem"
                onClick={() => props.onOpenWork(item.id)}
                title={item.title}
              >
                <div className="work-related-poster">
                  {thumb ? (
                    <img src={thumb} alt="" loading="lazy" decoding="async" />
                  ) : (
                    <div className="poster-fallback" aria-hidden />
                  )}
                </div>
                <div className="work-related-meta">
                  {item.primary_code && <strong>{item.primary_code}</strong>}
                  <span>{item.title}</span>
                </div>
              </button>
            );
          })}
        </div>
      )}
    </section>
  );
}

export function WorkDetailView(props: {
  workId: string;
  busy: string | null;
  onBack: () => void;
  onOpenWork: (workId: string) => void;
  onSave: (workId: string, payload: {
    title?: string;
    actors?: string[];
    studio?: string | null;
    series?: string | null;
    tags?: string[];
    plot?: string | null;
  }) => Promise<void>;
  onLocks: (workId: string, locks: string[]) => Promise<void>;
  onPreferPoster: (workId: string, index: number) => Promise<void>;
  onDeleteMagnet: (workId: string, magnetId: string) => Promise<void>;
  onRefresh: (workId: string) => void;
  onDownload: (workId: string) => void;
  onSelectGenreTag?: (tag: string) => void;
  onOpenActor?: (name: string) => void;
  report?: (message: string) => void;
}) {
  const [detail, setDetail] = useState<WorkDetail | null>(null);
  const [related, setRelated] = useState<WorkRelated>({ by_actor: [], by_tag: [] });
  const [loadError, setLoadError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [editOpen, setEditOpen] = useState(false);

  const reload = async (workId: string) => {
    setLoading(true);
    setLoadError(null);
    try {
      const [next, relatedNext] = await Promise.all([
        api.workDetail(workId),
        api.workRelated(workId, 18).catch(() => ({ by_actor: [], by_tag: [] } as WorkRelated))
      ]);
      setDetail(next);
      setRelated(relatedNext);
    } catch (error) {
      setDetail(null);
      setRelated({ by_actor: [], by_tag: [] });
      setLoadError(error instanceof Error ? error.message : String(error));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void reload(props.workId);
    // scroll detail page to top when switching works
    window.scrollTo(0, 0);
  }, [props.workId]);

  const work = detail;
  const [title, setTitle] = useState("");
  const [actors, setActors] = useState("");
  const [studio, setStudio] = useState("");
  const [series, setSeries] = useState("");
  const [tags, setTags] = useState("");
  const [plot, setPlot] = useState("");
  const [locks, setLocks] = useState<string[]>([]);

  useEffect(() => {
    if (!work) return;
    setTitle(work.title);
    setActors(work.actors.join(", "));
    setStudio(work.studio ?? "");
    setSeries(work.series ?? "");
    setTags(work.tags.join(", "));
    setPlot(work.plot ?? "");
    setLocks(work.field_locks ?? []);
  }, [work]);

  const posterUrl = useMemo(() => {
    if (!work) return null;
    return work.image_url ? appUrl(work.image_url) : null;
  }, [work]);

  const fanartUrl = useMemo(() => {
    if (!work?.fanart_url) return null;
    return appUrl(work.fanart_url);
  }, [work]);

  const runtimeLabel = formatRuntime(work?.runtime_seconds);

  const metaRows = useMemo(() => {
    if (!work) return [] as Array<{ label: string; value: ReactNode }>;
    const rows: Array<{ label: string; value: ReactNode }> = [];
    if (work.studio) rows.push({ label: "片商", value: work.studio });
    if (work.label) rows.push({ label: "厂牌", value: work.label });
    if (work.series) rows.push({ label: "系列", value: work.series });
    if (work.release_date) rows.push({ label: "发行", value: work.release_date });
    if (runtimeLabel) rows.push({ label: "时长", value: runtimeLabel });
    if (work.directors.length > 0) {
      rows.push({ label: "导演", value: work.directors.join("、") });
    }
    if (work.actors.length > 0) {
      rows.push({
        label: "演员",
        value: (
          <div className="work-page-actor-chips">
            {work.actors.map((name) => (
              props.onOpenActor ? (
                <button
                  key={name}
                  type="button"
                  className="display-tag-chip"
                  onClick={() => props.onOpenActor?.(name)}
                  title={`查看演员：${name}`}
                >{name}</button>
              ) : (
                <span key={name} className="display-tag-chip static">{name}</span>
              )
            ))}
          </div>
        )
      });
    }
    if (work.category) rows.push({ label: "分类", value: work.category });
    return rows;
  }, [work, runtimeLabel, props.onOpenActor]);

  if (loading && !work) {
    return (
      <section className="work-page">
        <div className="work-page-toolbar">
          <button type="button" className="ghost" onClick={props.onBack}>← 返回影片列表</button>
        </div>
        <p className="muted">正在加载作品详情…</p>
      </section>
    );
  }

  if (!work) {
    return (
      <section className="work-page">
        <div className="work-page-toolbar">
          <button type="button" className="ghost" onClick={props.onBack}>← 返回影片列表</button>
        </div>
        <div className="empty">
          <div>!</div>
          <h2>无法打开作品</h2>
          <p>{loadError ?? "作品不存在或已被删除"}</p>
        </div>
      </section>
    );
  }

  return (
    <section className="work-page">
      <div className="work-page-toolbar">
        <button type="button" className="ghost work-page-back" onClick={props.onBack}>
          ← 返回影片列表
        </button>
        <div className="work-page-toolbar-actions">
          <button
            type="button"
            className="secondary"
            disabled={props.busy === `work-${work.id}`}
            onClick={() => props.onRefresh(work.id)}
          >刷新元数据</button>
          <button
            type="button"
            className="ghost"
            disabled={props.busy === `artwork-${work.id}`}
            onClick={() => props.onDownload(work.id)}
          >缓存图片</button>
          <button
            type="button"
            className="ghost"
            onClick={() => {
              void (async () => {
                try {
                  const result = await api.generateWorkSamples(work.id);
                  await reload(work.id);
                  props.report?.(
                    `样本：网页 ${result.web_downloaded}，本地补帧 ${result.local_generated}，合计 ${result.sample_count}`
                  );
                } catch (error) {
                  props.report?.(error instanceof Error ? error.message : String(error));
                }
              })();
            }}
          >生成样本帧</button>
          <button
            type="button"
            className={editOpen ? "secondary" : "ghost"}
            onClick={() => setEditOpen((current) => !current)}
          >{editOpen ? "收起编辑" : "编辑"}</button>
        </div>
      </div>

      <div className="work-page-scroll">
        <header className="work-page-hero">
          <div className="work-page-poster-wrap">
            {posterUrl ? (
              <img className="work-page-poster" src={posterUrl} alt="" />
            ) : (
              <div className="work-page-poster fallback" aria-hidden />
            )}
            {fanartUrl && fanartUrl !== posterUrl && (
              <img className="work-page-fanart" src={fanartUrl} alt="" />
            )}
          </div>
          <div className="work-page-hero-meta">
            <p className="eyebrow">WORK DETAIL</p>
            {work.primary_code && <p className="work-page-code">{work.primary_code}</p>}
            <h1>{work.title}</h1>
            {work.original_title && work.original_title !== work.title && (
              <p className="original-title">原文：{work.original_title}</p>
            )}
            <div className="work-page-badge-row">
              <StatusBadges
                wantList={work.want_list}
                hasLocalMedia={work.has_local_media}
                inCatalog
              />
              {(work.rating_value != null || (work.javranking && work.javranking.score != null)) && (
                <div className="score-badge-row">
                  {work.rating_value != null && (
                    <span className="score-badge" title={work.rating_source ? `来源 ${work.rating_source}` : undefined}>
                      ★ {work.rating_value}
                      {work.rating_max != null ? `/${work.rating_max}` : ""}
                      {work.rating_count != null ? ` · ${work.rating_count}` : ""}
                    </span>
                  )}
                  {work.javranking?.score != null && (
                    <span className="score-badge secondary" title="JavRanking score">
                      JR {work.javranking.score}
                      {work.javranking.rank != null ? ` · #${work.javranking.rank}` : ""}
                    </span>
                  )}
                </div>
              )}
              <span className="pill">{work.category}</span>
            </div>

            {work.javranking && (
              <section className="javranking-honors">
                <div className="javranking-honors-head">
                  <h3>JavRanking 上榜</h3>
                  {work.javranking.compact_badge && (
                    <a
                      className="javranking-badge compact"
                      href={work.javranking.detail_url}
                      target="_blank"
                      rel="noopener noreferrer"
                    >{work.javranking.compact_badge}</a>
                  )}
                </div>
                <div className="tags javranking-badges">
                  {(work.javranking.honors ?? []).map((honor) => (
                    <a
                      key={`${honor.slug}-${honor.position}`}
                      className="javranking-badge"
                      href={honor.url || work.javranking?.detail_url || "#"}
                      target="_blank"
                      rel="noopener noreferrer"
                      title={honor.source ? `${honor.label} · ${honor.source}` : honor.label}
                    >{honor.label}</a>
                  ))}
                  {(work.javranking.honors ?? []).length === 0 && (
                    <p className="muted">已收录于 JavRanking，暂无分榜记录</p>
                  )}
                </div>
              </section>
            )}

            <dl className="work-page-meta-table">
              {metaRows.map((row) => (
                <div key={row.label} className="work-page-meta-row">
                  <dt>{row.label}</dt>
                  <dd>{row.value}</dd>
                </div>
              ))}
            </dl>

            {(work.display_tags ?? []).length > 0 && (
              <div className="tags display-tags" aria-label="作品标签">
                {(work.display_tags ?? []).map((tag) => (
                  props.onSelectGenreTag ? (
                    <button
                      key={tag}
                      type="button"
                      className="display-tag-chip"
                      onClick={() => props.onSelectGenreTag?.(tag)}
                      title={`按「${tag}」筛选影片`}
                    >{tag}</button>
                  ) : (
                    <span key={tag}>{tag}</span>
                  )
                ))}
              </div>
            )}
          </div>
        </header>

        <div className="work-page-body">
          <section className="work-page-section plot-bilingual">
            <h2>剧情</h2>
            <div className="work-page-plot">
              {work.plot
                ? <p>{work.plot}</p>
                : <p className="muted">暂无剧情译文</p>}
              {work.original_plot && work.original_plot !== work.plot && (
                <details>
                  <summary>原文 · original plot <small>来源 {sourceOf(work, "original_plot")}</small></summary>
                  <p className="plot-original">{work.original_plot}</p>
                </details>
              )}
            </div>
          </section>

          {(work.sample_urls ?? []).length > 0 && (
            <section className="work-page-section sample-gallery">
              <h2>样本 / 预览 ({(work.sample_urls ?? []).length})</h2>
              <div className="sample-grid work-page-sample-grid">
                {(work.sample_urls ?? []).map((url, index) => (
                  <a
                    key={`${url}-${index}`}
                    href={appUrl(url) ?? url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="sample-thumb"
                  >
                    <img src={appUrl(url) ?? url} alt="" loading="lazy" decoding="async" />
                  </a>
                ))}
              </div>
            </section>
          )}

          <section className="work-page-section magnet-panel">
            <div className="magnet-panel-head">
              <h2>资源 / 磁力 ({(work.magnets ?? []).length})</h2>
              <button
                type="button"
                className="secondary"
                onClick={() => {
                  void (async () => {
                    const url = await api.workEmbyLink(work.id);
                    if (url) window.open(url, "_blank", "noopener,noreferrer");
                    else window.alert("尚未配置 Emby/Jellyfin 地址");
                  })();
                }}
              >打开 Emby</button>
            </div>
            <p className="muted">一对多保存在本地，仅供复制/保存；应用不下载。可在「榜单 → 多源搜索」勾选后保存到作品。</p>
            {(work.magnets ?? []).length === 0
              ? <p className="muted">暂无已保存磁力。可在「榜单 → 多源番号搜索」勾选后保存到作品。</p>
              : (
                <div className="magnet-list">
                  {(work.magnets ?? []).map((magnet) => (
                    <div className="magnet-row" key={magnet.id}>
                      <span>
                        {(magnet.name || magnet.info_hash.slice(0, 12))
                          + (magnet.has_subtitle ? " · 字幕" : "")
                          + (magnet.hd ? " · HD" : "")}
                      </span>
                      <small className="muted">{magnet.provider}</small>
                      <button
                        type="button"
                        className="ghost"
                        onClick={() => void navigator.clipboard.writeText(magnet.uri)}
                      >复制</button>
                      <button
                        type="button"
                        className="ghost"
                        disabled={props.busy === `magnet-del-${magnet.id}`}
                        onClick={() => {
                          void (async () => {
                            await props.onDeleteMagnet(work.id, magnet.id);
                            await reload(work.id);
                          })();
                        }}
                      >移除</button>
                    </div>
                  ))}
                </div>
              )}
          </section>

          <RelatedStrip
            title="同演员作品"
            empty="库中暂无其他同演员作品"
            works={related.by_actor}
            onOpenWork={props.onOpenWork}
          />
          <RelatedStrip
            title="同类型作品"
            empty="库中暂无其他同类型作品"
            works={related.by_tag}
            onOpenWork={props.onOpenWork}
          />

          {(work.reviews ?? []).length > 0 && (
            <section className="work-page-section review-highlights">
              <h2>短评 / 摘录</h2>
              <ul className="review-list">
                {(work.reviews ?? []).map((item, index) => (
                  <li key={`${String(item.provider)}-${index}`}>
                    <p>{String(item.text ?? "")}</p>
                    <small className="muted">
                      {[item.provider, item.author, item.score != null ? `★ ${item.score}` : undefined]
                        .filter((value): value is string | number => value != null && value !== "")
                        .map(String)
                        .join(" · ")}
                    </small>
                  </li>
                ))}
              </ul>
            </section>
          )}

          {editOpen && (
            <section className="work-page-section work-page-edit">
              <h2>编辑字段</h2>
              <label>
                <span>标题 <small>来源 {sourceOf(work, "title")}</small></span>
                <input value={title} onChange={(event) => setTitle(event.target.value)} />
              </label>
              <label>
                <span>演员 <small>来源 {sourceOf(work, "actors")}</small></span>
                <input value={actors} onChange={(event) => setActors(event.target.value)} placeholder="逗号分隔" />
              </label>
              <label>
                <span>片商 <small>来源 {sourceOf(work, "studio")}</small></span>
                <input value={studio} onChange={(event) => setStudio(event.target.value)} />
              </label>
              <label>
                <span>系列 <small>来源 {sourceOf(work, "series")}</small></span>
                <input value={series} onChange={(event) => setSeries(event.target.value)} />
              </label>
              <label>
                <span>标签 <small>来源 {sourceOf(work, "tags")}</small></span>
                <input value={tags} onChange={(event) => setTags(event.target.value)} placeholder="逗号分隔" />
              </label>
              <label>
                <span>剧情 · 译文 <small>来源 {sourceOf(work, "plot")}</small></span>
                <textarea value={plot} onChange={(event) => setPlot(event.target.value)} rows={5} />
              </label>
              <div className="lock-grid">
                <strong>字段锁</strong>
                <small>锁定后刷新/再刮削不会覆盖该字段</small>
                {EDITABLE_LOCK_FIELDS.map((field) => (
                  <label key={field} className="check-line">
                    <input
                      type="checkbox"
                      checked={locks.includes(field)}
                      onChange={(event) => {
                        setLocks((current) => event.target.checked
                          ? [...current, field]
                          : current.filter((item) => item !== field));
                      }}
                    />
                    {field}
                  </label>
                ))}
              </div>
              <div className="work-detail-actions">
                <button
                  type="button"
                  disabled={props.busy === `edit-${work.id}`}
                  onClick={() => {
                    void (async () => {
                      await props.onSave(work.id, {
                        title,
                        actors: actors.split(/[,，]/).map((item) => item.trim()).filter(Boolean),
                        studio: studio.trim() || null,
                        series: series.trim() || null,
                        tags: tags.split(/[,，]/).map((item) => item.trim()).filter(Boolean),
                        plot: plot.trim() || null
                      });
                      await reload(work.id);
                    })();
                  }}
                >保存到 Work</button>
                <button
                  type="button"
                  className="secondary"
                  disabled={props.busy === `locks-${work.id}`}
                  onClick={() => {
                    void (async () => {
                      await props.onLocks(work.id, locks);
                      await reload(work.id);
                    })();
                  }}
                >保存锁</button>
              </div>

              <h3>缓存海报</h3>
              <div className="poster-pick">
                {work.artwork.map((item, index) => {
                  const local = typeof item.local_path === "string" ? item.local_path : null;
                  const preferred = item.preferred === true;
                  return (
                    <button
                      key={`${index}-${String(item.url ?? local ?? index)}`}
                      type="button"
                      className={preferred ? "secondary" : "ghost"}
                      disabled={!local || props.busy === `poster-${work.id}`}
                      onClick={() => {
                        void (async () => {
                          await props.onPreferPoster(work.id, index);
                          await reload(work.id);
                        })();
                      }}
                    >{preferred ? "当前海报" : local ? `选用 #${index + 1}` : `无缓存 #${index + 1}`}</button>
                  );
                })}
                {work.artwork.length === 0 && <p className="muted">暂无图片条目</p>}
              </div>
            </section>
          )}

          <section className="work-page-section">
            <h2>关联资产 ({work.assets.length})</h2>
            {work.assets.length === 0
              ? <p className="muted">暂无本地媒体文件</p>
              : (
                <ul className="asset-list">
                  {work.assets.map((asset) => (
                    <li key={asset.id}><code>{asset.path}</code><span>{asset.state}</span></li>
                  ))}
                </ul>
              )}
          </section>

          <section className="work-page-section">
            <h2>合集</h2>
            <div className="tags">
              {(work.collections ?? []).map((item) => (
                <span key={item.id}>{item.kind} · {item.name}</span>
              ))}
              {(work.collections ?? []).length === 0 && <p className="muted">尚未关联系列/片商/平台合集</p>}
            </div>
          </section>

          <section className="work-page-section">
            <h2>身份</h2>
            <ul className="asset-list">
              {work.identities.map((identity) => (
                <li key={`${identity.provider}-${identity.kind}-${identity.value}`}>
                  <code>{identity.provider}/{identity.kind}</code><span>{identity.value}</span>
                </li>
              ))}
            </ul>
          </section>
        </div>
      </div>
    </section>
  );
}
