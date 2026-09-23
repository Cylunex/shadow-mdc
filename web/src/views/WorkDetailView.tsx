import { useEffect, useMemo, useState, type ReactNode } from "react";

import { api, appUrl } from "../api";
import { StatusBadges } from "../components/StatusBadges";
import type { PanOfflineTask, Work, WorkDetail, WorkRelated } from "../model";

const EDITABLE_LOCK_FIELDS = ["title", "actors", "studio", "series", "tags", "plot"] as const;

type LangMode = "translated" | "original";

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

function LangToggle(props: {
  mode: LangMode;
  onChange: (mode: LangMode) => void;
  hasOriginal: boolean;
  label?: string;
}) {
  if (!props.hasOriginal) return null;
  return (
    <div className="lang-toggle" role="group" aria-label={props.label ?? "语言切换"}>
      <button
        type="button"
        className={props.mode === "translated" ? "lang-toggle-btn active" : "lang-toggle-btn"}
        onClick={() => props.onChange("translated")}
      >译文</button>
      <button
        type="button"
        className={props.mode === "original" ? "lang-toggle-btn active" : "lang-toggle-btn"}
        onClick={() => props.onChange("original")}
      >原文</button>
    </div>
  );
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

function MetaRow(props: { label: string; children: ReactNode; empty?: boolean }) {
  return (
    <div className="work-page-meta-row">
      <dt>{props.label}</dt>
      <dd className={props.empty ? "meta-empty" : undefined}>{props.children}</dd>
    </div>
  );
}


function visibleIdentities(items: WorkDetail["identities"]) {
  const covered = new Set(
    items
      .filter((item) => item.kind !== "source_url" && item.source_url)
      .map((item) => `${item.provider}|${item.source_url}`)
  );
  return items.filter((item) => {
    if (item.kind !== "source_url") return true;
    const key = `${item.provider}|${item.source_url ?? item.value}`;
    return !covered.has(key);
  });
}

async function copyText(value: string, label: string, report?: (message: string) => void) {
  try {
    await navigator.clipboard.writeText(value);
    report?.(`已复制${label}`);
  } catch {
    report?.(`复制${label}失败`);
  }
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
  const [offlineTasks, setOfflineTasks] = useState<PanOfflineTask[]>([]);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [editOpen, setEditOpen] = useState(false);
  const [titleMode, setTitleMode] = useState<LangMode>("translated");
  const [plotMode, setPlotMode] = useState<LangMode>("translated");

  const reload = async (workId: string) => {
    setLoading(true);
    setLoadError(null);
    try {
      const [next, relatedNext, offlineNext] = await Promise.all([
        api.workDetail(workId),
        api.workRelated(workId, 18).catch(() => ({ by_actor: [], by_tag: [] } as WorkRelated)),
        api.workOfflineTasks(workId).catch(() => [] as PanOfflineTask[])
      ]);
      setDetail(next);
      setRelated(relatedNext);
      setOfflineTasks(offlineNext);
    } catch (error) {
      setDetail(null);
      setRelated({ by_actor: [], by_tag: [] });
      setOfflineTasks([]);
      setLoadError(error instanceof Error ? error.message : String(error));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void reload(props.workId);
    setTitleMode("translated");
    setPlotMode("translated");
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

  const hasOriginalTitle = Boolean(
    work?.original_title && work.original_title.trim() && work.original_title !== work.title
  );
  const hasOriginalPlot = Boolean(
    work?.original_plot && work.original_plot.trim() && work.original_plot !== (work.plot ?? "")
  );

  const displayTitle = useMemo(() => {
    if (!work) return "";
    if (titleMode === "original" && hasOriginalTitle) return work.original_title ?? work.title;
    return work.title;
  }, [work, titleMode, hasOriginalTitle]);

  const displayPlot = useMemo(() => {
    if (!work) return null;
    if (plotMode === "original" && hasOriginalPlot) return work.original_plot ?? null;
    return work.plot ?? null;
  }, [work, plotMode, hasOriginalPlot]);

  const ratingLabel = useMemo(() => {
    if (!work) return null;
    const parts: string[] = [];
    if (work.rating_value != null) {
      parts.push(
        `★ ${work.rating_value}${work.rating_max != null ? `/${work.rating_max}` : ""}` +
          (work.rating_count != null ? ` · ${work.rating_count}` : "")
      );
    }
    if (work.javranking?.score != null) {
      parts.push(
        `JR ${work.javranking.score}` +
          (work.javranking.rank != null ? ` · #${work.javranking.rank}` : "")
      );
    }
    return parts.length ? parts.join(" · ") : null;
  }, [work]);

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

  const displayTags = work.display_tags ?? work.tags ?? [];

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
            <div className="work-page-badge-row">
              <StatusBadges
                wantList={work.want_list}
                hasLocalMedia={work.has_local_media}
                inCatalog
              />
              {work.javranking?.compact_badge && (
                <a
                  className="javranking-badge compact"
                  href={work.javranking.detail_url}
                  target="_blank"
                  rel="noopener noreferrer"
                >{work.javranking.compact_badge}</a>
              )}
            </div>

            <dl className="work-page-meta-table work-page-meta-table--labeled">
              <MetaRow label="番号" empty={!work.primary_code}>
                {work.primary_code ? (
                  <span className="work-page-code-row">
                    <span className="work-page-code-inline">{work.primary_code}</span>
                    <button
                      type="button"
                      className="ghost work-page-code-copy"
                      title="复制番号"
                      onClick={() => void copyText(work.primary_code!, "番号", props.report)}
                    >复制</button>
                  </span>
                ) : "—"}
              </MetaRow>

              <div className="work-page-meta-row work-page-meta-row--title">
                <dt>标题</dt>
                <dd>
                  <div className="meta-title-head">
                    <LangToggle
                      mode={titleMode}
                      onChange={setTitleMode}
                      hasOriginal={hasOriginalTitle}
                      label="标题语言"
                    />
                  </div>
                  <h1 className="work-page-title">{displayTitle}</h1>
                  {hasOriginalTitle && titleMode === "translated" && (
                    <p className="original-title muted-hint">原文可切换查看</p>
                  )}
                </dd>
              </div>

              <MetaRow label="演员" empty={work.actors.length === 0}>
                {work.actors.length === 0 ? "—" : (
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
                )}
              </MetaRow>

              <MetaRow label="日期" empty={!work.release_date}>
                {work.release_date ?? "—"}
              </MetaRow>

              <MetaRow label="时长" empty={!runtimeLabel}>
                {runtimeLabel ?? "—"}
              </MetaRow>

              <MetaRow label="导演" empty={work.directors.length === 0}>
                {work.directors.length > 0 ? work.directors.join("、") : "—"}
              </MetaRow>

              <MetaRow label="片商" empty={!work.studio}>
                {work.studio ?? "—"}
                {work.label && work.label !== work.studio ? (
                  <span className="muted-hint"> · 厂牌 {work.label}</span>
                ) : null}
              </MetaRow>

              <MetaRow label="系列" empty={!work.series}>
                {work.series ?? "—"}
              </MetaRow>

              <MetaRow label="评分" empty={!ratingLabel}>
                {ratingLabel ?? "—"}
              </MetaRow>

              <MetaRow label="分类" empty={!work.category}>
                {work.category ? <span className="pill">{work.category}</span> : "—"}
              </MetaRow>

              <MetaRow label="标签" empty={displayTags.length === 0}>
                {displayTags.length === 0 ? "—" : (
                  <div className="tags display-tags" aria-label="作品标签">
                    {displayTags.map((tag) => (
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
              </MetaRow>
            </dl>

            {work.javranking && (work.javranking.honors ?? []).length > 0 && (
              <section className="javranking-honors">
                <div className="javranking-honors-head">
                  <h3>荣誉徽章</h3>
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
                </div>
              </section>
            )}
          </div>
        </header>

        <div className="work-page-body">
          <section className="work-page-section plot-bilingual">
            <div className="section-head-row">
              <h2>简介</h2>
              <LangToggle
                mode={plotMode}
                onChange={setPlotMode}
                hasOriginal={hasOriginalPlot}
                label="简介语言"
              />
            </div>
            <div className="work-page-plot">
              {displayPlot
                ? <p>{displayPlot}</p>
                : <p className="muted">{plotMode === "original" ? "暂无原文简介" : "暂无简介译文"}</p>}
              {hasOriginalPlot && (
                <p className="muted-hint plot-lang-hint">
                  当前显示{plotMode === "original" ? "原文" : "译文"}
                  {plotMode === "translated" ? ` · 来源 ${sourceOf(work, "plot")}` : ` · 来源 ${sourceOf(work, "original_plot")}`}
                </p>
              )}
            </div>
          </section>

          <section className="work-page-section sample-gallery">
            <h2>片段图片 {(work.sample_urls ?? []).length > 0 ? `(${(work.sample_urls ?? []).length})` : ""}</h2>
            {(work.sample_urls ?? []).length === 0 ? (
              <p className="muted">暂无样本帧。可点上方「生成样本帧」尝试抓取。</p>
            ) : (
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
            )}
          </section>

          <section className="work-page-section review-highlights">
            <h2>精选评价 {(work.reviews ?? []).length > 0 ? `(${(work.reviews ?? []).length})` : ""}</h2>
            {(work.reviews ?? []).length === 0 ? (
              <p className="muted">暂无短评摘录</p>
            ) : (
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
            )}
          </section>

          <section className="work-page-section magnet-panel">
            <div className="magnet-panel-head">
              <h2>磁力链接 ({(work.magnets ?? []).length})</h2>
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
            <p className="muted">一对多保存在本地；可复制，或「推到 115 离线」（需在设置中完成 115 登录并选择目录）。可在「榜单 → 多源搜索」勾选后保存到作品。</p>
            {(work.magnets ?? []).length === 0
              ? <p className="muted">暂无已保存磁力。可在「榜单 → 多源番号搜索」勾选后保存到作品。</p>
              : (
                <div className="magnet-list">
                  {(work.magnets ?? []).map((magnet) => {
                    const magnets = work.magnets ?? [];
                    const bestHash = [...magnets].sort((a, b) => {
                      const score = (m: typeof a) =>
                        (m.has_subtitle ? 4 : 0) + (m.hd ? 2 : 0) + Math.min((m.size_bytes ?? 0) / 1e12, 1);
                      return score(b) - score(a);
                    })[0]?.info_hash;
                    const offline = offlineTasks.find((task) =>
                      task.magnet_id === magnet.id
                      || (task.info_hash && magnet.info_hash
                        && task.info_hash.toLowerCase() === magnet.info_hash.toLowerCase())
                    );
                    const offlineLabel = offline
                      ? (offline.strm_path
                        ? "STRM 已生成"
                        : offline.status === "completed" || offline.status === "done"
                          ? "离线完成"
                          : offline.status === "failed" || offline.status === "error"
                            ? `离线失败${offline.error ? `：${offline.error}` : ""}`
                            : `离线中 ${Math.round(offline.progress || 0)}%`)
                      : null;
                    return (
                    <div className="magnet-row" key={magnet.id}>
                      <span>
                        {(magnet.name || magnet.info_hash.slice(0, 12))
                          + (magnet.has_subtitle ? " · 字幕" : "")
                          + (magnet.hd ? " · HD" : "")
                          + (magnet.size_bytes
                            ? ` · ${(magnet.size_bytes / (1024 ** 3)).toFixed(2)} GiB`
                            : "")
                          + (bestHash && magnet.info_hash === bestHash ? " · 推荐" : "")}
                      </span>
                      <small className="muted">{magnet.provider}</small>
                      {offlineLabel && (
                        <span className={`offline-chip status-${(offline?.status || "running").toLowerCase()}`} title={offline?.strm_path ?? offline?.error ?? undefined}>
                          {offlineLabel}
                        </span>
                      )}
                      <button
                        type="button"
                        className="ghost"
                        onClick={() => void navigator.clipboard.writeText(magnet.uri)}
                      >复制</button>
                      <button
                        type="button"
                        className="secondary"
                        disabled={props.busy === `offline-${magnet.id}` || Boolean(offline && !offline.error && offline.status !== "failed")}
                        onClick={() => {
                          void (async () => {
                            try {
                              const task = await api.submitWorkOffline(work.id, { magnet_id: magnet.id });
                              setOfflineTasks((current) => {
                                const without = current.filter((item) => item.id !== task.id && item.info_hash !== task.info_hash);
                                return [task, ...without];
                              });
                              props.report?.(`已提交 115 离线：${task.info_hash.slice(0, 12)}… (${task.status})`);
                            } catch (error) {
                              props.report?.(error instanceof Error ? error.message : String(error));
                            }
                          })();
                        }}
                      >{offline && !offline.error && offline.status !== "failed" ? "已推送" : "推到 115 离线"}</button>
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
                    );
                  })}
                </div>
              )}
          </section>

          {offlineTasks.length > 0 && (
            <section className="work-page-section work-offline-strip">
              <div className="work-related-head">
                <h2>115 离线任务</h2>
                <span className="muted">{offlineTasks.length}</span>
              </div>
              <div className="magnet-list">
                {offlineTasks.slice(0, 8).map((task) => (
                  <div className="magnet-row" key={task.id}>
                    <span>{task.remote_name || task.info_hash.slice(0, 16)}</span>
                    <small className="muted">{task.status} · {Math.round(task.progress || 0)}%</small>
                    {task.strm_path && <small className="muted">STRM: {task.strm_path}</small>}
                    {task.error && <small className="danger-text">{task.error}</small>}
                  </div>
                ))}
              </div>
            </section>
          )}

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

          <section className="work-page-section work-page-aux">
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

          <section className="work-page-section work-page-aux">
            <h2>合集</h2>
            <div className="tags">
              {(work.collections ?? []).map((item) => (
                <span key={item.id}>{item.kind} · {item.name}</span>
              ))}
              {(work.collections ?? []).length === 0 && <p className="muted">尚未关联系列/片商/平台合集</p>}
            </div>
          </section>

          <section className="work-page-section work-page-aux">
            <h2>资料站 / 身份</h2>
            {work.identities.length === 0 ? (
              <p className="muted">暂无外部身份</p>
            ) : (
              <ul className="identity-chip-list">
                {visibleIdentities(work.identities).map((identity) => {
                  const href = identity.source_url && /^https?:/i.test(identity.source_url)
                    ? identity.source_url
                    : null;
                  return (
                    <li key={`${identity.provider}-${identity.kind}-${identity.value}`} className="identity-chip">
                      <span className="identity-chip-provider">{identity.provider}</span>
                      {href ? (
                        <a href={href} target="_blank" rel="noopener noreferrer" title={href}>
                          {identity.value}
                        </a>
                      ) : (
                        <span className="identity-chip-value">{identity.value}</span>
                      )}
                      <button
                        type="button"
                        className="ghost"
                        title="复制"
                        onClick={() => void copyText(identity.value, identity.provider, props.report)}
                      >复制</button>
                    </li>
                  );
                })}
              </ul>
            )}
          </section>
        </div>
      </div>
    </section>
  );
}
